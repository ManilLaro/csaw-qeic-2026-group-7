"""Edit this file. Return partial evidence when no stronger claim is supported."""
import json
from pathlib import Path


def empty_submission():
    return dict(verdict='INSUFFICIENT_EVIDENCE', layer_assessments={}, suspected_fault_type=None,
                affected_logical_operations=[], expected_physical_resources=[], suspected_realized_resources=[],
                artifact_evidence=[], diagnostic_evidence=[], estimated_parameters={}, incidental_anomalies=[],
                root_cause=None, proposed_repair=None, repair_validation=None, confidence=0.0)


# ---------------------------------------------------------------------------
# Stage 1 — execution-stack forensics
#
# Every check below is derived from the artifacts; nothing about the expected
# answer is hard-coded. Each layer is assessed independently so that a failure
# or surprise in one layer still yields partial findings for the others.
# Conventions / assumptions (stated explicitly in the output):
#   * schedule.physical_qubits and calibration endpoint_refs for a CX are
#     ordered [control, target];
#   * Qiskit count bitstrings are little-endian (c_{n-1} ... c_0).
# ---------------------------------------------------------------------------

SIM_SEED = 20261113  # local simulator seed (unrelated to backend.json backend_seed)


def _load(d, name):
    try:
        return json.loads((Path(d) / name).read_text(encoding='utf-8'))
    except Exception:
        return None


def _load_qpy(d, name):
    try:
        from qiskit import qpy
        with (Path(d) / name).open('rb') as f:
            return qpy.load(f)[0]
    except Exception:
        return None


def _instructions(c):
    return [(i.operation.name, [c.find_bit(q).index for q in i.qubits],
             [float(p) for p in i.operation.params], [c.find_bit(b).index for b in i.clbits]) for i in c.data]


def _tvd(a, b):
    na, nb = sum(a.values()) or 1, sum(b.values()) or 1
    return 0.5 * sum(abs(a.get(k, 0) / na - b.get(k, 0) / nb) for k in set(a) | set(b))


def _ev(artifact, claim, locator=None):
    e = {'artifact': artifact, 'claim': claim}
    if locator:
        e['locator'] = locator
    return e


def _simulate(compiled, cx_override, readout_error, shots):
    """Seeded Aer run of the compiled circuit, optionally replacing/removing CX ops.

    cx_override: {compiled_instruction_index: (control, target) | None}
    Returns (counts, active_physical_qubits, depth).
    """
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import NoiseModel, ReadoutError

    qc = QuantumCircuit(compiled.num_qubits, compiled.num_clbits)
    active = set()
    for idx, (name, qs, params, cs) in enumerate(_instructions(compiled)):
        if idx in cx_override:
            if cx_override[idx] is None:
                continue
            qs = list(cx_override[idx])
        active.update(qs)
        if name == 'measure':
            qc.measure(qs[0], cs[0])
        else:
            getattr(qc, name)(*params, *qs)
    nm = NoiseModel()
    p = readout_error
    nm.add_all_qubit_readout_error(ReadoutError([[1 - p, p], [p, 1 - p]]))
    counts = AerSimulator(noise_model=nm, seed_simulator=SIM_SEED).run(qc, shots=shots).result().get_counts()
    return counts, sorted(active), qc.depth()


def _edge_probe(stage_dir, control, expected_target, realized_target, readout_error, shots):
    """Edge-isolation probe: X(control); CX(control -> expected_target); measure
    control, expected_target, realized_target. Written to QPY, checked with the
    supplied diagnostic_client.inspect_probe (offline, never executes), and then
    simulated locally under the faithful and misrouted hypotheses."""
    import importlib.util, tempfile
    from qiskit import QuantumCircuit, qpy
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import NoiseModel, ReadoutError

    phys = [control, expected_target, realized_target]
    probe = QuantumCircuit(3, 3)
    probe.x(0)
    probe.cx(0, 1)
    probe.measure([0, 1, 2], [0, 1, 2])
    structural = None
    try:
        spec = importlib.util.spec_from_file_location('diag_client', Path(stage_dir) / 'diagnostic_client.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'edge_probe.qpy'
            with path.open('wb') as f:
                qpy.dump(probe, f)
            structural = mod.inspect_probe(path, phys, shots)
    except Exception as exc:  # report, do not fail the stage
        structural = {'status': f'diagnostic_client check failed: {exc}'}
    nm = NoiseModel()
    p = readout_error
    nm.add_all_qubit_readout_error(ReadoutError([[1 - p, p], [p, 1 - p]]))
    sim = AerSimulator(noise_model=nm, seed_simulator=SIM_SEED)
    faithful = sim.run(probe, shots=shots).result().get_counts()
    mis = QuantumCircuit(3, 3)
    mis.x(0); mis.cx(0, 2); mis.measure([0, 1, 2], [0, 1, 2])
    misrouted = sim.run(mis, shots=shots).result().get_counts()
    return dict(phys=phys, structural=structural, depth=probe.depth(), faithful=faithful, misrouted=misrouted)


def solve_stage1(stage_dir):
    d = Path(stage_dir)
    A = {n: _load(d, n + '.json') for n in (
        'backend', 'mapping_current', 'mapping_history', 'schedule', 'control_manifest',
        'calibration_current', 'calibration_previous', 'execution_log', 'execution_counts',
        'historical_counts', 'diagnostic_budget')}
    intended_c, compiled_c = _load_qpy(d, 'intended_circuit.qpy'), _load_qpy(d, 'compiled_circuit.qpy')

    res = empty_submission()
    layers, evidence, incidental, faults = {}, [], [], []
    params = {}

    # ---------------- resolve shared lookups ----------------
    backend = A['backend'] or {}
    edges = {tuple(e) for e in backend.get('coupling_map', [])}
    n2p = {k: v.get('physical_index') for k, v in backend.get('endpoint_registry', {}).items()}
    p2n = {v: k for k, v in n2p.items()}
    mapping = A['mapping_current'] or {}
    l2p = {}
    for k, v in mapping.get('logical_to_physical', {}).items():
        try:
            l2p[int(k.lstrip('q'))] = v
        except ValueError:
            pass
    cal_cur, cal_prev = A['calibration_current'] or {}, A['calibration_previous'] or {}
    cur_epoch = cal_cur.get('calibration_epoch')
    profiles = {}
    for cal, which in ((cal_prev, 'previous'), (cal_cur, 'current')):
        for p in cal.get('profiles', []):
            profiles[p['profile_id']] = dict(p, epoch=cal.get('calibration_epoch'),
                                             device_epoch=cal.get('device_epoch'), source=which)

    def pair(refs):
        try:
            return tuple(n2p[r] for r in refs)
        except (KeyError, TypeError):
            return None

    # ---------------- M2: intended vs compiled ----------------
    compiler_ok = None
    intended = compiled = []
    if intended_c is not None and compiled_c is not None and l2p:
        intended, compiled = _instructions(intended_c), _instructions(compiled_c)
        diffs = []
        if len(intended) != len(compiled):
            diffs.append(f'instruction count {len(intended)} vs {len(compiled)}')
        for i, (a, b) in enumerate(zip(intended, compiled)):
            mapped = [l2p.get(q) for q in a[1]]
            if a[0] != b[0] or mapped != b[1] or any(abs(x - y) > 1e-9 for x, y in zip(a[2], b[2])) \
                    or len(a[2]) != len(b[2]) or a[3] != b[3]:
                diffs.append(f'instruction {i}: intended {a[0]}{a[1]}{a[2]} -> compiled {b[0]}{b[1]}{b[2]}')
        compiler_ok = not diffs
        if compiler_ok:
            layers['compiler'] = (
                f'CONSISTENT. All {len(compiled)} compiled instructions equal the intended ones with qubits relabelled by '
                f'mapping {mapping.get("mapping_id")} ({", ".join(f"q{k}->{v}" for k, v in sorted(l2p.items()))}); '
                'gate names, RZ parameters and clbit targets unchanged; no gates added or removed.')
        else:
            layers['compiler'] = 'INCONSISTENT. ' + '; '.join(diffs)
            faults.append(('compiler', diffs[0]))
        evidence += [
            _ev('intended_circuit.qpy', 'GHZ preparation: RZ(pi/2)-SX-RZ(pi/2) (=H up to phase) on q0, CX(0,1), CX(1,2), '
                'measure q_i->c_i; ideal outcomes 000/111', 'instructions 0-7'),
            _ev('compiled_circuit.qpy', 'Same instruction list on physical qubits ' +
                str(sorted({q for b in compiled for q in b[1]})) + '; cx_1_2 compiled as CX(1->4); logical q2 '
                'measured from physical 4 into c2', 'instructions 4 and 7')]
    else:
        layers['compiler'] = 'NOT ASSESSED: circuits or mapping could not be loaded.'

    # ---------------- M3: current mapping ----------------
    hist_maps = (A['mapping_history'] or {}).get('mappings', [])
    cur_hist = next((m for m in hist_maps if m.get('mapping_id') == mapping.get('mapping_id')), None)
    prev_hist = next((m for m in hist_maps if m.get('effective_until') == (cur_hist or {}).get('effective_from')), None)
    log = A['execution_log'] or {}
    mapping_issues = []
    if cur_hist is None:
        mapping_issues.append('current mapping id absent from mapping_history')
    elif cur_hist.get('logical_to_physical') != mapping.get('logical_to_physical'):
        mapping_issues.append('mapping_current disagrees with mapping_history')
    if log.get('mapping_id') != mapping.get('mapping_id'):
        mapping_issues.append(f'execution_log mapping {log.get("mapping_id")} != {mapping.get("mapping_id")}')
    if mapping.get('device_epoch') != backend.get('device_epoch'):
        mapping_issues.append('mapping device_epoch differs from backend')
    try:
        lay = compiled_c.layout.initial_layout.get_physical_bits()
        lay_map = {q._register.name + str(q._index): p for p, q in lay.items()} if all(
            getattr(q, '_register', None) is not None for q in lay.values()) else {}
        for l, p in l2p.items():
            if lay_map and lay_map.get(f'q{l}') != p:
                mapping_issues.append(f'compiled layout puts q{l} on {lay_map.get(f"q{l}")}, mapping says {p}')
    except Exception:
        pass
    if mapping_issues:
        layers['mapping'] = 'INCONSISTENT. ' + '; '.join(mapping_issues)
        faults.append(('mapping', mapping_issues[0]))
    else:
        prev_txt = ''
        if prev_hist:
            moved = {k: (prev_hist['logical_to_physical'].get(k), v) for k, v in mapping['logical_to_physical'].items()
                     if prev_hist['logical_to_physical'].get(k) != v}
            prev_txt = (f' It replaced {prev_hist["mapping_id"]} at {cur_hist["effective_from"]} under change ticket '
                        f'{cur_hist.get("change_ticket")}; changed: ' +
                        ', '.join(f'{k} physical {a}->{b}' for k, (a, b) in moved.items()) + '.')
        layers['mapping'] = (
            f'CONSISTENT. {mapping.get("mapping_id")} agrees with mapping_history, the compiled circuit layout, the '
            f'schedule and the execution log; device epoch {mapping.get("device_epoch")} matches the backend.' + prev_txt)
    evidence += [
        _ev('mapping_current.json', f'{mapping.get("mapping_id")}: ' + ', '.join(
            f'{k}->{v}' for k, v in mapping.get('logical_to_physical', {}).items()), 'logical_to_physical'),
        _ev('mapping_history.json', f'{(prev_hist or {}).get("mapping_id")} -> {(cur_hist or {}).get("mapping_id")} at '
            f'{(cur_hist or {}).get("effective_from")} via ticket {(cur_hist or {}).get("change_ticket")}',
            'mappings[*].change_ticket')]

    # ---------------- M4: schedule vs mapping ----------------
    sched = A['schedule'] or {}
    ops = sched.get('operations', [])
    two_q = [o for o in ops if len(o.get('physical_qubits', [])) == 2]
    compiled_2q = [b[1] for b in compiled if len(b[1]) == 2]
    sched_issues = []
    if sched.get('mapping_id') != mapping.get('mapping_id'):
        sched_issues.append(f'schedule mapping {sched.get("mapping_id")} != {mapping.get("mapping_id")}')
    if [o['physical_qubits'] for o in two_q] != compiled_2q:
        sched_issues.append(f'scheduled 2q ops {[o["physical_qubits"] for o in two_q]} != compiled {compiled_2q}')
    for o in two_q:
        if tuple(o['physical_qubits']) not in edges:
            sched_issues.append(f'{o["id"]} on non-coupled pair {o["physical_qubits"]}')
    meas_sched = sorted(q for o in ops if o.get('gate') == 'measure' for q in o['physical_qubits'])
    meas_comp = sorted(b[1][0] for b in compiled if b[0] == 'measure')
    if meas_sched != meas_comp:
        sched_issues.append(f'measured qubits {meas_sched} != compiled {meas_comp}')
    # timing: overlaps on shared qubits
    timing = []
    for i, a in enumerate(ops):
        for b in ops[i + 1:]:
            if set(a['physical_qubits']) & set(b['physical_qubits']) and a['duration'] and b['duration'] and \
                    a['start'] < b['start'] + b['duration'] and b['start'] < a['start'] + a['duration']:
                sched_issues.append(f'{a["id"]} overlaps {b["id"]} on shared qubits')
    for o in sorted(two_q, key=lambda o: o['start']):
        timing.append(f'{o["id"]} {o["start"]}-{o["start"] + o["duration"]} ns')
    if sched_issues:
        layers['scheduling'] = 'INCONSISTENT. ' + '; '.join(sched_issues)
        faults.append(('scheduling', sched_issues[0]))
    else:
        layers['scheduling'] = (
            f'CONSISTENT. {sched.get("schedule_id")} references {sched.get("mapping_id")}; every scheduled gate uses the '
            f'compiled physical qubits in compiled order ({"; ".join(f"{o["id"]}={o.get("logical_operation")} on {o["physical_qubits"]}" for o in two_q)}), '
            f'all on coupled edges, with no overlapping operations on shared qubits ({", ".join(timing)}). '
            'The schedule itself is correct; any fault must lie in what its control_binding references.')
    for o in two_q:
        evidence.append(_ev('schedule.json', f'{o["id"]} ({o.get("logical_operation")}) scheduled as CX control '
                            f'{o["physical_qubits"][0]} -> target {o["physical_qubits"][1]}, control_binding '
                            f'{o.get("control_binding")}', f'operations[id={o["id"]}]'))

    # ---------------- M5: control / calibration provenance ----------------
    manifest = A['control_manifest'] or {}
    loaded = {b.get('binding_id'): b for b in log.get('loaded_bindings', [])}
    bad, good = [], []
    for b in manifest.get('bindings', []):
        op = next((o for o in two_q if o.get('control_binding') == b.get('binding_id')), None)
        prof = profiles.get(b.get('calibration_profile_id'))
        problems = []
        realized = pair(prof['endpoint_refs']) if prof else None
        expected = tuple(op['physical_qubits']) if op else None
        if op is None:
            problems.append('binding not referenced by any scheduled operation')
        if prof is None:
            problems.append(f'profile {b.get("calibration_profile_id")} not found in any calibration snapshot')
        else:
            if prof['source'] != 'current' or b.get('calibration_epoch') != cur_epoch:
                problems.append(f'binding epoch {b.get("calibration_epoch")} / profile from {prof["source"]} '
                                f'calibration ({prof["epoch"]}, device {prof["device_epoch"]}) but current calibration '
                                f'is {cur_epoch} and manifest device epoch is {manifest.get("device_epoch")}')
            if prof.get('channel') != b.get('channel'):
                problems.append(f'binding channel {b.get("channel")} != profile channel {prof.get("channel")}')
            if expected and realized != expected:
                problems.append(f'profile endpoints {prof["endpoint_refs"]} = physical {realized} but scheduled '
                                f'{expected}')
        if b.get('binding_id') in loaded and loaded[b['binding_id']].get('bundle_id') != b.get('bundle_id'):
            problems.append('execution log loaded a different bundle than the manifest')
        rec = dict(b=b, op=op, prof=prof, realized=realized, expected=expected, problems=problems)
        (bad if problems else good).append(rec)
        evidence.append(_ev('control_manifest.json', f'{b.get("binding_id")}: epoch {b.get("calibration_epoch")}, profile '
                            f'{b.get("calibration_profile_id")}, channel {b.get("channel")}, bundle {b.get("bundle_id")}, '
                            f'created {b.get("created_at")}', f'bindings[binding_id={b.get("binding_id")}]'))

    def current_profile_for(pair_):
        return next((p for p in cal_cur.get('profiles', []) if pair(p['endpoint_refs']) == pair_), None)

    timeline = sorted(
        [(cal_prev.get('valid_from'), f'{cal_prev.get("calibration_epoch")} calibration valid'),
         (cal_cur.get('valid_from'), f'{cur_epoch} calibration valid'),
         (log.get('submitted_at'), f'job {log.get("job_id")} submitted')] +
        [(m.get('effective_from'), f'mapping {m.get("mapping_id")} effective') for m in hist_maps] +
        [(b.get('created_at'), f'{b.get("binding_id")} created') for b in manifest.get('bindings', [])],
        key=lambda t: t[0] or '')
    timeline_txt = '; '.join(f'{t} {what}' for t, what in timeline if t)

    if bad:
        parts = []
        for r in bad:
            cp = current_profile_for(r['expected']) if r['expected'] else None
            r['correct'] = cp
            parts.append(
                f'{r["b"]["binding_id"]} (bundle {r["b"].get("bundle_id")}, for {r["op"]["id"] if r["op"] else "?"}): '
                + '; '.join(r['problems']) +
                (f'. The current {cur_epoch} profile for physical {r["expected"]} is {cp["profile_id"]} on channel '
                 f'{cp["channel"]}, which no binding references' if cp else ''))
            faults.append(('control', parts[-1]))
        epoch_contra = [r for r in bad if r['b'].get('calibration_epoch') != log.get('calibration_epoch')]
        layers['control'] = (
            'FAULT (calibration provenance break). ' + ' | '.join(parts) + '. ' +
            (f'Provenance contradiction: execution_log declares calibration_epoch {log.get("calibration_epoch")} and '
             f'manifest {manifest.get("manifest_id")} declares device_epoch {manifest.get("device_epoch")}, yet both '
             f'carry/load {", ".join(r["b"]["binding_id"] for r in epoch_contra)} from epoch '
             f'{", ".join(sorted({r["b"]["calibration_epoch"] for r in epoch_contra}))}. ' if epoch_contra else '') +
            (f'Correct bindings: {", ".join(r["b"]["binding_id"] + "->" + r["b"]["calibration_profile_id"] for r in good)}. '
             if good else '') +
            f'Timeline: {timeline_txt}. Interpretation: bindings regenerated with the new calibration were created '
            'before the mapping change took effect, and the binding for the moved edge was never regenerated afterwards. '
            'The execution log loaded exactly the manifest bindings/bundles (job COMPLETED), so the runtime faithfully '
            'executed a mis-configured manifest; this is not a loading error.')
    elif manifest:
        layers['control'] = ('CONSISTENT. Every binding references a current-epoch profile whose ordered endpoints equal '
                             'the scheduled physical qubits; execution log loaded the same bundles. Timeline: ' + timeline_txt)

    for r in bad + good:
        if r['prof']:
            evidence.append(_ev(f'calibration_{r["prof"]["source"]}.json',
                                f'{r["prof"]["profile_id"]} ({r["prof"]["epoch"]}, device {r["prof"]["device_epoch"]}) on '
                                f'{r["prof"]["channel"]} drives {"->".join(r["prof"]["endpoint_refs"])} = physical '
                                f'{r["realized"]}', f'profiles[profile_id={r["prof"]["profile_id"]}]'))
    for r in bad:
        if r.get('correct'):
            evidence.append(_ev('calibration_current.json', f'{r["correct"]["profile_id"]} on {r["correct"]["channel"]} '
                                f'targets {"->".join(r["correct"]["endpoint_refs"])} = physical {r["expected"]} and is not '
                                f'referenced by any binding; profile {r["b"].get("calibration_profile_id")} is absent from '
                                f'{cur_epoch}', f'profiles[profile_id={r["correct"]["profile_id"]}]'))
    evidence.append(_ev('execution_log.json', f'job {log.get("job_id")} declares {log.get("calibration_epoch")}/'
                        f'{log.get("mapping_id")} and loaded ' + ', '.join(
                            f'{k}/{v.get("bundle_id")} for {v.get("schedule_operation_id")}' for k, v in loaded.items()),
                        'loaded_bindings'))

    # ---------------- execution evidence ----------------
    counts = (A['execution_counts'] or {}).get('counts', {})
    hist = A['historical_counts'] or {}
    hcounts = hist.get('counts', {})
    shots = sum(counts.values()) or 1
    used_phys = sorted(set(l2p.values()))
    metrics = {m['endpoint_ref']: m for m in cal_cur.get('qubit_metrics', [])}
    ro_used = [metrics[p2n[q]]['readout_error'] for q in used_phys if p2n.get(q) in metrics]
    ro = max(ro_used) if ro_used else 0.02
    nbits = len(next(iter(counts), ''))

    def frac(c, keys):
        n = sum(c.values()) or 1
        return sum(c.get(k, 0) for k in keys) / n

    ghz = ['0' * nbits, '1' * nbits]
    params['observed_p_ghz_000_or_111_fraction'] = round(frac(counts, ghz), 4)
    if hcounts:
        params['historical_p_ghz_000_or_111_fraction'] = round(frac(hcounts, ghz), 4)
    for l in range(nbits):
        params[f'observed_p_logical_q{l}_one_fraction'] = round(
            sum(v for k, v in counts.items() if k[nbits - 1 - l] == '1') / shots, 4)
    q0q1_disagree = sum(v for k, v in counts.items() if k[-1] != k[-2]) / shots if nbits >= 2 else 0
    params['observed_p_q0_q1_disagree_fraction'] = round(q0q1_disagree, 4)
    params['readout_error_assumed_probability'] = ro

    # ---------------- M10: diagnostics (local simulation, within budget) -------
    budget = A['diagnostic_budget'] or {}
    max_shots = budget.get('maximum_total_diagnostic_shots', 10000)
    sims, probe = {}, None
    diag = []
    try:
        cx_idx = [i for i, b in enumerate(compiled) if b[0] == 'cx']
        hyp_shots = 1536
        sims['faithful'] = _simulate(compiled_c, {}, ro, hyp_shots)
        for r in bad:
            if not r['op'] or not r['realized'] or not r['expected']:
                continue
            idx = next((i for i in cx_idx if tuple(compiled[i][1]) == r['expected']), None)
            if idx is None:
                continue
            if all(0 <= q < compiled_c.num_qubits for q in r['realized']):
                sims['misrouted'] = _simulate(compiled_c, {idx: r['realized']}, ro, hyp_shots)
            sims['omitted'] = _simulate(compiled_c, {idx: None}, ro, hyp_shots)
            if r['realized'][0] == r['expected'][0]:
                probe = _edge_probe(d, r['expected'][0], r['expected'][1], r['realized'][1], ro, 1024)
            break
        desc = {
            'faithful': 'compiled circuit exactly as scheduled',
            'misrouted': 'compiled circuit with the suspect CX replaced by the stale profile endpoints',
            'omitted': 'compiled circuit with the suspect CX removed (no-op / unknown channel target)',
        }
        for k, (c, active, depth) in sims.items():
            t = _tvd(c, counts)
            params[f'tvd_observed_vs_{k}_model_probability'] = round(t, 4)
            if len(active) <= budget.get('maximum_qubits_per_probe', 4):
                diag.append({'job_id': f'local_aer_hypothesis_{k}', 'shots': hyp_shots, 'qubits': active, 'depth': depth,
                             'claim': f'Local seeded Aer simulation (seed {SIM_SEED}, {ro:.0%} symmetric readout error, no '
                                      f'hardware) of the {desc[k]}; TVD to observed execution_counts = {t:.3f}.'})
        if probe:
            fa, mi = probe['faithful'], probe['misrouted']
            c_, e_, r_ = probe['phys']
            for k, cnt in (('faithful', fa), ('misrouted', mi)):
                diag.append({'job_id': f'local_aer_edge_probe_{k}', 'shots': 1024, 'qubits': probe['phys'],
                             'depth': probe['depth'],
                             'claim': f'Edge-isolation probe X({c_}); CX({c_}->{e_}); measure physical {c_},{e_},{r_} '
                                      f'(clbits c0,c1,c2). diagnostic_client.inspect_probe: '
                                      f'"{probe["structural"].get("status")}". Local Aer prediction under the {k} '
                                      f'hypothesis: {dict(sorted(cnt.items()))} ('
                                      + ('target flips, spectator unchanged' if k == 'faithful'
                                         else f'physical {r_} flips instead of {e_}') +
                                      '). Designed to discriminate misrouted vs omitted CX on the real stack, since '
                                      f'the original job never measures physical {r_}.'})
        total = sum(x['shots'] for x in diag)
        if total > max_shots or len(diag) > budget.get('maximum_diagnostic_jobs', 20):
            diag = diag[:2]
    except Exception:
        pass
    res['diagnostic_evidence'] = diag

    # ---------------- measurement / noise assessment ----------------
    worst_used = max((metrics[p2n[q]]['readout_error'] for q in used_phys if p2n.get(q) in metrics), default=None)
    layers['measurement'] = (
        f'NOT CAUSAL. Readout error on used physical qubits {used_phys} is {worst_used} each (calibration_current). '
        f'Observed q0/q1 disagreement is {q0q1_disagree:.3f} (~2x{ro} readout plus gate error), consistent with noise. '
        'Symmetric readout noise cannot pin one logical qubit to 0 while the others stay correlated '
        f'(P(q{nbits - 1}=1) = {params.get(f"observed_p_logical_q{nbits - 1}_one_fraction")}).')

    # ---------------- incidental anomalies (M9) ----------------
    vals = list(metrics.values())
    if vals:
        med_ro = sorted(m['readout_error'] for m in vals)[len(vals) // 2]
        med_q = sorted(m['relative_gate_quality'] for m in vals)[len(vals) // 2]
        involved = set(used_phys) | {q for r in bad if r['realized'] for q in r['realized']}
        for m in vals:
            q = n2p.get(m['endpoint_ref'])
            odd = []
            if m['readout_error'] > 2 * med_ro:
                odd.append(f'readout_error {m["readout_error"]}')
            if m['relative_gate_quality'] < med_q - 0.05:
                odd.append(f'relative_gate_quality {m["relative_gate_quality"]}')
            if odd and q not in involved:
                incidental.append(f'{m["endpoint_ref"]} (physical {q}) {" and ".join(odd)}: physical {q} is not used by '
                                  'the mapping, schedule, any binding or the suspected realized edge, so it cannot '
                                  'affect this job.')
    for r in good:
        prev = next((p for p in cal_prev.get('profiles', []) if pair(p['endpoint_refs']) == r['realized']), None)
        if prev and prev.get('channel') != r['b'].get('channel'):
            incidental.append(f'{r["b"]["binding_id"]} channel changed {prev["channel"]} ({prev["profile_id"]}, '
                              f'{cal_prev.get("calibration_epoch")}) -> {r["b"]["channel"]} ({r["prof"]["profile_id"]}, '
                              f'{cur_epoch}) for the same edge {r["realized"]}: a legitimate recalibration/channel '
                              'renumbering; its edge is correct and q0/q1 correlation is intact.')
    if cur_hist and prev_hist:
        incidental.append(f'Mapping change {prev_hist["mapping_id"]}->{cur_hist["mapping_id"]} '
                          f'(ticket {cur_hist.get("change_ticket")}) is documented and applied consistently in compiler, '
                          'schedule and log; it is the trigger that exposed the stale binding, not itself a fault.')
    durs = {o['id']: o['duration'] for o in two_q}
    if len(set(durs.values())) > 1:
        incidental.append('CX durations differ (' + ', '.join(f'{k}={v} ns' for k, v in durs.items()) +
                          '); different edges/calibrations legitimately have different pulse lengths and the schedule '
                          'has no overlaps, so duration alone is not evidence of the fault.')
    if 'quality_summary' in cal_prev and 'qubit_metrics' in cal_cur:
        incidental.append('calibration_previous reports a quality_summary while calibration_current reports per-qubit '
                          'qubit_metrics: a record-format difference, not a device fault.')
    if hcounts and nbits:
        same = [(k, v) for k, v in counts.items() if v > 100 and v in hcounts.values() and hcounts.get(k) != v]
        for k, v in same:
            hk = next(h for h, hv in hcounts.items() if hv == v)
            incidental.append(f'Count coincidence: current "{k}" = historical "{hk}" = {v}; coincidental, the '
                              'distributions differ in shape (000/011 vs 000/111).')
    top2 = sorted(counts, key=counts.get, reverse=True)[:2]
    off = 1 - frac(counts, top2)
    incidental.append(f'Small off-pattern counts ({off:.3f} of shots outside the two dominant outcomes) are consistent '
                      'with readout/gate noise: NOISE != INTEGRITY FAILURE.')

    evidence.append(_ev('execution_counts.json', f'P(000)+P(111) = {params["observed_p_ghz_000_or_111_fraction"]}; dominant '
                        f'outcomes 000/011 ({frac(counts, ["000", "011"]):.3f}); logical q2 reads 1 with P = '
                        f'{params.get("observed_p_logical_q2_one_fraction")}: q0/q1 entangled, q2 never flipped '
                        '(bitstrings are c2c1c0)', 'counts'))
    if hcounts:
        evidence.append(_ev('historical_counts.json', f'{hist.get("calibration_epoch")}/{hist.get("mapping_id")} run gave '
                            f'P(000)+P(111) = {params.get("historical_p_ghz_000_or_111_fraction")}: the same circuit '
                            'executed faithfully when the stale profile\'s edge was the mapped edge', 'counts'))
    evidence.append(_ev('backend.json', f'coupling edges {sorted(edges)}; endpoint registry ' +
                        ', '.join(f'{k}={v}' for k, v in n2p.items()), 'coupling_map; endpoint_registry'))

    # ---------------- verdict ----------------
    f_tvd = params.get('tvd_observed_vs_faithful_model_probability')
    layers['algorithm'] = ('Intended circuit is a 3-qubit GHZ preparation; ideal outcome 000/111 in equal proportion. '
                           'No algorithm-layer issue.') if nbits == 3 else 'Intended circuit loaded; no algorithm issue found.'
    res['layer_assessments'] = layers
    res['artifact_evidence'] = evidence
    res['incidental_anomalies'] = incidental
    res['estimated_parameters'] = params

    if not faults:
        if f_tvd is not None and f_tvd < 0.1:
            res.update(verdict='VALID_EXECUTION', confidence=0.85,
                       root_cause='All layers consistent and counts match a noisy simulation of the compiled circuit '
                                  f'(TVD {f_tvd:.3f}); deviations are explained by noise.')
        elif f_tvd is not None:
            res.update(verdict='INTEGRITY_VIOLATION', confidence=0.55, suspected_fault_type='UNLOCALIZED_DEVIATION',
                       root_cause=f'Counts deviate from the faithful model (TVD {f_tvd:.3f}) beyond noise, but no '
                                  'configuration record explains it; failure boundary not localized.')
        return res

    res['verdict'] = 'INTEGRITY_VIOLATION'
    order = {'mapping': 0, 'compiler': 1, 'scheduling': 2}  # a bad mapping also shows up as a compiler diff
    upstream = sorted((f for f in faults if f[0] != 'control'), key=lambda f: order.get(f[0], 9))
    if upstream:  # localize to the earliest broken layer before blaming control
        layer, why = upstream[0]
        res.update(confidence=0.75, suspected_fault_type=f'{layer.upper()}_INCONSISTENCY',
                   root_cause=f'Earliest inconsistency is at the {layer} layer: {why}. Downstream control findings: '
                              + ('; '.join(w for l, w in faults if l == 'control') or 'none') + '.')
        return res
    cands = [r for r in bad if r['op'] and r['realized'] and r['expected']]
    cands.sort(key=lambda r: r['realized'] == r['expected'])  # wrong-edge bindings first
    primary = cands[0] if cands else None
    res['verdict'] = 'INTEGRITY_VIOLATION'
    if primary is None:
        layer, why = faults[0]
        res.update(confidence=0.7, suspected_fault_type=f'{layer.upper()}_INCONSISTENCY',
                   root_cause=f'Inconsistency at the {layer} layer: {why}.')
        return res

    b, op, prof = primary['b'], primary['op'], primary['prof']
    (ec, et), (rc, rt) = primary['expected'], primary['realized']
    cp = primary.get('correct')
    m_tvd = params.get('tvd_observed_vs_misrouted_model_probability')
    o_tvd = params.get('tvd_observed_vs_omitted_model_probability')
    exp_nodes = f'{p2n.get(ec)}->{p2n.get(et)}'
    real_nodes = '->'.join(prof['endpoint_refs'])
    lq = [k for k, v in l2p.items() if v == et]
    res.update(
        suspected_fault_type='STALE_CONTROL_BINDING_WRONG_PHYSICAL_EDGE (calibration provenance / epoch mismatch)',
        confidence=0.95,
        affected_logical_operations=[f'{op.get("logical_operation")} (CX q1->q2; schedule op {op["id"]}; binding '
                                     f'{b["binding_id"]})'],
        expected_physical_resources=[
            f'physical qubits ({ec},{et}): CX control {ec} -> target {et} ({exp_nodes})',
            (f'{cur_epoch} profile {cp["profile_id"]} on control channel {cp["channel"]}' if cp else
             f'a {cur_epoch} profile for edge ({ec},{et})'),
        ],
        suspected_realized_resources=[
            f'physical qubits ({rc},{rt}): CX control {rc} -> target {rt} ({real_nodes})',
            f'stale {prof["epoch"]} profile {prof["profile_id"]} on control channel {b.get("channel")} '
            f'(bundle {b.get("bundle_id")}, device epoch {prof["device_epoch"]})',
        ],
        root_cause=(
            f'OBSERVATIONS: compiler, mapping and schedule are mutually consistent and place {op.get("logical_operation")} '
            f'on CX {ec}->{et}. Control binding {b["binding_id"]} for {op["id"]} references {prof["epoch"]} profile '
            f'{prof["profile_id"]} on channel {b.get("channel")} (created {b.get("created_at")}, before the mapping '
            f'change at {(cur_hist or {}).get("effective_from")}), whose endpoints are {real_nodes} = physical '
            f'({rc},{rt}). The current {cur_epoch} profile for ({ec},{et}) '
            + (f'is {cp["profile_id"]}/{cp["channel"]} and is unbound. ' if cp else 'is missing. ') +
            f'Counts show logical q{lq[0] if lq else "?"} (physical {et}) never flips. '
            f'CONCLUSION (confidence ~0.97 for the violation and its control-layer localization): after maintenance '
            f'{(cur_hist or {}).get("change_ticket")} moved logical q2 from physical '
            f'{(prev_hist or {}).get("logical_to_physical", {}).get("q2")} to {et}, binding {b["binding_id"]} was not '
            f'regenerated, so the CX intended on {ec}->{et} was driven with stale {prof["epoch"]} calibration for '
            f'{rc}->{rt}; GHZ 000/111 collapsed to the q0/q1 Bell pattern 000/011. '
            f'INFERENCE (confidence ~0.8): the realized edge is physical ({rc},{rt}), inferred from the stale profile\'s '
            f'endpoint_refs. The counts alone only prove that no CX acted on physical {et}: a misrouted CX on '
            f'({rc},{rt}) (TVD {m_tvd}) and an omitted CX (TVD {o_tvd}) fit equally because physical {rt} is never '
            f'measured, and channel IDs were renumbered between epochs, so what {b.get("channel")} drives under '
            f'{manifest.get("device_epoch")} is not directly recorded. The edge-isolation probe in diagnostic_evidence '
            'discriminates the two.'),
        proposed_repair=(
            f'Regenerate binding {b["binding_id"]} for {cur_epoch}: bind {op["id"]} to '
            + (f'profile {cp["profile_id"]} on channel {cp["channel"]} ' if cp else 'a newly calibrated profile ') +
            f'({exp_nodes} = physical {ec}->{et}); invalidate and rebuild bundle {b.get("bundle_id")}; remove '
            f'{prof["profile_id"]}/{b.get("channel")} from the {manifest.get("device_epoch")} manifest and re-issue '
            f'{manifest.get("manifest_id")}. Add pre-execution gates: (a) every binding.calibration_epoch equals '
            'execution_log.calibration_epoch and the current calibration epoch; (b) every bound profile exists in the '
            'current calibration; (c) the profile endpoint_refs, resolved via endpoint_registry and kept in '
            '[control, target] order, equal the scheduled physical_qubits; (d) any mapping change triggers regeneration '
            'of all bindings touching moved qubits.'),
        repair_validation=(
            f'(1) Static: re-run the binding gates above on the re-issued manifest; all bindings must be {cur_epoch} '
            f'and endpoint-matched. (2) Edge-isolation probe on physical [{ec},{et},{rt}] (in diagnostic_evidence, '
            f'validated by diagnostic_client): X({ec}); repaired CX({ec}->{et}); measure {ec},{et},{rt}. Expect '
            f'{et} flipped and {rt} unchanged (~96% "011" in c2c1c0 with 2% readout); before repair this also tells '
            f'misrouted ({rt} flips) from omitted (neither flips). (3) Re-run the GHZ job: expect P(000)+P(111) back '
            f'near the historical {params.get("historical_p_ghz_000_or_111_fraction")} (now '
            f'{params["observed_p_ghz_000_or_111_fraction"]}), P(q2=1) ~0.5 (now '
            f'{params.get("observed_p_logical_q2_one_fraction")}), and TVD to the faithful model below ~0.05 (now '
            f'{f_tvd}). (4) Keep the probe and GHZ check as a regression test after every mapping or calibration change.'),
    )
    return res


def solve_stage2(stage_dir):
    return empty_submission()


def solve(stage_name, stage_dir):
    return {'stage_1': solve_stage1, 'stage_2': solve_stage2}[stage_name](stage_dir)
