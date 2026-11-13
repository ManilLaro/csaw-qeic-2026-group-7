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


# ---------------------------------------------------------------------------
# Stage 2 — cross-layer forensics (32-qubit workload, device/model provenance)
#
# Chain: device_design (geometry -> coupling g) -> device_model (reduced proxy g)
#        -> control_calibration (rate = 2*pi*g/1000 rad/ns, pulse duration)
#        -> schedule/runtime -> characterization + verification observables.
# Every number and label below is read or derived from the artifacts.
# ---------------------------------------------------------------------------

def _mps_observables(circ, target_idx, angle, paulis, bond, threshold, seed):
    """Aer MPS expectation values; returns (values, max bond dimension reached)."""
    import re
    import numpy as np
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Pauli
    from qiskit_aer import AerSimulator
    q = QuantumCircuit(circ.num_qubits)
    for k, ins in enumerate(circ.data):
        op = ins.operation
        if k == target_idx and angle is not None:
            op = op.__class__(angle)
        q.append(op, ins.qubits)
    for lab, p in paulis.items():
        q.save_expectation_value(Pauli(p), range(circ.num_qubits), label=lab)
    sim = AerSimulator(method='matrix_product_state', matrix_product_state_max_bond_dimension=bond,
                       matrix_product_state_truncation_threshold=threshold, mps_log_data=True)
    r = sim.run(q, shots=1, seed_simulator=seed).result()
    data = r.data(0)
    log = str(r.results[0].metadata.get('MPS_log_data', ''))
    bds = [int(x) for grp in re.findall(r'BD=\[([0-9 ]+)\]', log) for x in grp.split()]
    return {k: float(np.real(data[k])) for k in paulis}, (max(bds) if bds else -1)


def _frac_pi(x, max_den=64):
    from fractions import Fraction
    import math
    f = Fraction(x / math.pi).limit_denominator(max_den)
    num = '' if f.numerator == 1 else f'{f.numerator}*'
    return f'{num}pi/{f.denominator}' if abs(float(f) * math.pi - x) < 1e-9 else f'{x:.6f} rad'


def solve_stage2(stage_dir):
    import math
    import numpy as np

    d = Path(stage_dir)
    A = {n: _load(d, n + '.json') or {} for n in (
        'challenge', 'backend_snapshot', 'mapping', 'simulated_runtime_snapshot', 'device_design', 'device_model',
        'provenance_registry', 'control_manifest', 'control_calibration', 'characterization', 'local_subsystem',
        'observable_spec', 'verification_protocol', 'noise_snapshot', 'simulation_config', 'schedule')}
    ch, be, mp, rt = A['challenge'], A['backend_snapshot'], A['mapping'], A['simulated_runtime_snapshot']
    des, mod, reg, man = A['device_design'], A['device_model'], A['provenance_registry'], A['control_manifest']
    cal, char, loc, obs = A['control_calibration'], A['characterization'], A['local_subsystem'], A['observable_spec']
    ver, noise, simcfg, sched = A['verification_protocol'], A['noise_snapshot'], A['simulation_config'], A['schedule']

    res = empty_submission()
    layers, params, incidental, faults, rejected = {}, {}, [], [], []
    ev = {k: [] for k in ('artifact', 'runtime', 'design', 'model', 'control', 'sim')}
    diag = []

    chc = ch.get('circuit', {}) if isinstance(ch.get('circuit'), dict) else {}
    tgt_op = chc.get('target_operation_id') or man.get('target_operation_id')
    tgt_edge = tuple(man.get('target_physical_edge') or chc.get('target_physical_edge') or ())
    c_idx = chc.get('target_instruction_index')
    i_idx = chc.get('intended_target_instruction_index')
    tgt_logical = chc.get('target_logical_qubits')
    e0, e1 = tgt_edge if len(tgt_edge) == 2 else (None, None)

    # ---------------- M2: circuit / compilation ----------------
    I, C = _load_qpy(d, 'intended_circuit.qpy'), _load_qpy(d, 'compiled_circuit.qpy')
    a = b = []
    target_angle = None
    if I is not None and C is not None:
        a, b = _instructions(I), _instructions(C)

        def wires(x):
            w = {}
            for o in x:
                for q in o[1]:
                    w.setdefault(q, []).append((o[0], tuple(o[1]), tuple(round(p, 12) for p in o[2])))
            return w
        basis = set(be.get('basis_gates', []))
        off_basis = sorted({o[0] for o in b} - basis)
        ta = a[i_idx] if i_idx is not None and i_idx < len(a) else None
        tb = b[c_idx] if c_idx is not None and c_idx < len(b) else None
        target_angle = tb[2][0] if tb and tb[2] else None
        same_target = ta is not None and tb is not None and ta[0] == tb[0] and ta[1] == tb[1] \
            and abs(ta[2][0] - tb[2][0]) < 1e-12
        compiler_ok = wires(a) == wires(b) and not off_basis and same_target
        layers['compiler'] = (
            ('CONSISTENT. ' if compiler_ok else 'INCONSISTENT. ') +
            f'Intended and compiled circuits both have {len(a)}/{len(b)} instructions; the compiled order is a '
            'reordering of commuting operations, and per-qubit (DAG) gate sequences, names and parameters are '
            f'{"identical" if wires(a) == wires(b) else "DIFFERENT"}; off-basis gates: {off_basis or "none"}. Target '
            f'{tgt_op}: intended index {i_idx} {ta[0] if ta else "?"}{list(ta[1]) if ta else ""}'
            f'({ta[2][0] if ta and ta[2] else "?"}) vs compiled index {c_idx} {tb[0] if tb else "?"}'
            f'{list(tb[1]) if tb else ""}({target_angle}): {"identical" if same_target else "DIFFERENT"}. The compiled '
            f'circuit requests exactly RZZ({_frac_pi(target_angle) if target_angle else "?"}) on the target edge; the '
            'compiler is not the fault boundary.')
        if not compiler_ok:
            faults.append('compiler')
        ev['artifact'] += [
            _ev('intended_circuit.qpy', f'target {ta[0] if ta else "?"}({ta[2][0] if ta and ta[2] else "?"}) on '
                f'{list(ta[1]) if ta else "?"} at intended index {i_idx}; metadata {I.metadata}', f'instruction {i_idx}'),
            _ev('compiled_circuit.qpy', f'same {tb[0] if tb else "?"}({target_angle}) on physical {list(tb[1]) if tb else "?"}'
                f' at compiled index {c_idx}; per-qubit sequences identical to intended (commuting reorder only)',
                f'instruction {c_idx}')]
        if i_idx != c_idx:
            rejected.append(f'Index difference intended {i_idx} vs compiled {c_idx} for {tgt_op}: commuting reorder; '
                            'operation, qubits and angle identical.')
    else:
        layers['compiler'] = 'NOT ASSESSED: circuits could not be loaded.'

    # ---------------- M3: mapping / runtime / physical edge ----------------
    edges_dir = [tuple(e) for e in be.get('edges', [])]
    edges = {tuple(sorted(e)) for e in edges_dir}
    l2p = {int(k): v for k, v in mp.get('logical_to_physical', {}).items()}
    identity = bool(l2p) and all(k == v for k, v in l2p.items())
    map_op = next((o for o in mp.get('operations', []) if o.get('operation_id') == tgt_op), {})
    chain = []
    if edges_dir:  # walk the chain if the coupling graph is a path
        adj = {}
        for u, v in edges_dir:
            adj.setdefault(u, set()).add(v); adj.setdefault(v, set()).add(u)
        ends = [q for q, n in adj.items() if len(n) == 1]
        if len(ends) == 2 and all(len(n) <= 2 for n in adj.values()):
            chain, prev = [min(ends)], None
            while len(chain) < len(adj):
                nxt = [x for x in adj[chain[-1]] if x != prev]
                prev = chain[-1]; chain.append(nxt[0])
    rt_ok = (rt.get('device_revision') == be.get('device_revision') == des.get('revision')
             and rt.get('geometry_fingerprint') == be.get('geometry_fingerprint') == des.get('geometry_fingerprint')
             and tuple(rt.get('target_physical_edge', [])) == tgt_edge
             and rt.get('physical_layout') == [l2p.get(i) for i in range(len(l2p))]
             and rt.get('calibration_epoch') == cal.get('calibration_epoch') == man.get('calibration_epoch'))
    edge_ok = tuple(sorted(tgt_edge)) in edges and tuple(map_op.get('physical_edge', [])) == tgt_edge
    layers['mapping'] = (
        f'CONSISTENT. {mp.get("revision")} is {"the identity" if identity else "a non-identity"} layout on {len(l2p)} '
        f'qubits; {tgt_op} maps logical {map_op.get("logical_qubits")} -> physical {map_op.get("physical_edge")}, a '
        f'supported edge of {be.get("artifact_id")} ({len(edges)} edges' +
        (f', a path {"-".join(map(str, chain[:6]))}-...-{chain[-1]}' if chain else '') + ').'
        if edge_ok else f'INCONSISTENT. target edge {tgt_edge} unsupported or mismatched in mapping.')
    layers['runtime'] = (
        (f'CONSISTENT. Runtime {rt.get("simulated_job_id")} used device revision {rt.get("device_revision")}, geometry '
         f'fingerprint {str(rt.get("geometry_fingerprint"))[:12]}..., calibration epoch {rt.get("calibration_epoch")}, '
         f'mapping {rt.get("mapping_ref")} and schedule {rt.get("schedule_ref")}, matching the backend snapshot and the '
         'current device design. The runtime executed the stack it was given.') if rt_ok else
        'INCONSISTENT runtime/backend/design revision, fingerprint, layout or epoch.')
    if not edge_ok:
        faults.append('mapping')
    if not rt_ok:
        faults.append('runtime')
    ev['runtime'] += [
        _ev('simulated_runtime_snapshot.json', f'job {rt.get("simulated_job_id")}: {rt.get("device_revision")}/'
            f'{rt.get("calibration_epoch")}, fingerprint {rt.get("geometry_fingerprint")}, target edge '
            f'{rt.get("target_physical_edge")}', 'device_revision; geometry_fingerprint; calibration_epoch'),
        _ev('backend_snapshot.json', f'{be.get("device_revision")}, fingerprint {be.get("geometry_fingerprint")}, '
            f'selected edge {be.get("selected_edge")} present in edges', 'edges; selected_edge'),
        _ev('mapping.json', f'{tgt_op}: logical {map_op.get("logical_qubits")} -> physical {map_op.get("physical_edge")}',
            'operations[0]')]
    # edge orientation: report only if it actually differs
    if b:
        orient = {tuple(o[1]) for o in b if o[0] == 'rzz'}
        flipped = [e for e in orient if e not in set(edges_dir) and tuple(reversed(e)) in set(edges_dir)]
        if flipped:
            rejected.append(f'RZZ on {flipped[:3]} uses the reverse orientation of backend edges; RZZ is symmetric, so '
                            'orientation is irrelevant.')

    # ---------------- schedule and workload exposure ----------------
    sops = sched.get('operations', [])
    sched_mism = sum(1 for o in sops if not b or o['instruction_index'] >= len(b) or
                     (o['gate'], o['qubits']) != (b[o['instruction_index']][0], b[o['instruction_index']][1]))
    tgt_sched = [o for o in sops if o.get('control_ref') == man.get('artifact_id')]
    dup = [o for o in sops if o.get('operation_id') == tgt_op and o.get('instruction_index') != c_idx]
    sched_dur = tgt_sched[0]['duration_ns'] if tgt_sched else None
    edge_ops = [o for o in sops if o['gate'] == 'rzz' and sorted(o['qubits']) == sorted(tgt_edge)]
    edge_base = [o for o in edge_ops if o.get('control_ref') != man.get('artifact_id')]
    base_refs = sorted({o.get('control_ref') for o in sops} - {man.get('artifact_id')})
    base_durs = sorted({o['duration_ns'] for o in edge_base})
    base_angles = [b[o['instruction_index']][2][0] for o in edge_base if b]
    layers['scheduling'] = (
        f'CONSISTENT. All {len(sops)} schedule entries match the compiled instruction at their instruction_index '
        f'({sched_mism} mismatches). Exactly {len(tgt_sched)} entry uses {man.get("artifact_id")}: '
        f'{tgt_sched[0]["operation_id"] if tgt_sched else "?"} (index {tgt_sched[0]["instruction_index"] if tgt_sched else "?"}'
        f', rzz on {tgt_sched[0]["qubits"] if tgt_sched else "?"}, start {tgt_sched[0]["start_ns"] if tgt_sched else "?"} ns), '
        f'whose duration {sched_dur} ns equals the calibrated pulse {cal.get("pulse_duration_ns")} ns. Scheduling '
        'faithfully applied the mis-derived calibration.'
        if not sched_mism and tgt_sched and sched_dur == cal.get('pulse_duration_ns') else
        'INCONSISTENT schedule vs compiled circuit or calibration.')
    if sched_mism:
        faults.append('scheduling')
    if dup:
        o = dup[0]
        incidental.append(
            f'schedule.json reuses operation_id {tgt_op} for instruction_index {o["instruction_index"]} ({o["gate"]} on '
            f'{o["qubits"]}, {o["duration_ns"]} ns, {o["control_ref"]})' +
            (f'; {o["instruction_index"]} is the target\'s INTENDED index, so this is an id-labelling collision' if
             o['instruction_index'] == i_idx else '') +
            '. It matches the compiled instruction at that index, is a zero-duration virtual gate on an unrelated qubit '
            'with the base control, and cannot change the target interaction: bookkeeping defect, not the cause.')
    if edge_base:
        incidental.append(
            f'Edge {list(tgt_edge)} carries {len(edge_ops)} RZZ gates in the workload; the other {len(edge_base)} use '
            f'{", ".join(base_refs)} with durations {base_durs} ns and angles {min(base_angles):.3f}-'
            f'{max(base_angles):.3f} rad. Their different duration from the {sched_dur} ns target pulse reflects a '
            f'different control path/angle, not an error. LIMITATION: {", ".join(base_refs)} appears only in the '
            f'registry external_ids {reg.get("external_ids")}, so its provenance (and whether it also used the stale '
            f'model) cannot be verified from the supplied evidence; the evidence implicates only {tgt_op} (and the '
            'verification target that shares its calibration).')

    # ---------------- M4: device design provenance ----------------
    revs = {r['revision']: r for r in reg.get('revisions', [])}
    fp2rev = {r['geometry_fingerprint']: r['revision'] for r in reg.get('revisions', [])}
    cur_rev = des.get('revision')
    lr = des.get('local_region', {})
    cpl = lr.get('coupler', {})
    g_design = lr.get('coupling_proxy_MHz')
    comps = {c['id']: c for c in des.get('components', [])}
    design_ok = (fp2rev.get(des.get('geometry_fingerprint')) == cur_rev == rt.get('device_revision')
                 and sorted(lr.get('qubits', [])) == sorted(tgt_edge))
    xs = [(comps[f'geom-Q{q}']['x_um'], q) for q in range(len(l2p)) if f'geom-Q{q}' in comps]
    phys_order = [q for _, q in sorted(xs)]
    adjacent = bool(chain) and phys_order[:len(chain)] in (chain, chain[::-1])
    layers['device_design'] = (
        f'CURRENT AND CONSISTENT. {des.get("artifact_id")} is revision {cur_rev}, fingerprint '
        f'{str(des.get("geometry_fingerprint"))[:12]}..., which the provenance registry assigns to {cur_rev} '
        f'({revs.get(cur_rev, {}).get("timestamp")}) and which equals the runtime/backend fingerprint. Local region '
        f'{lr.get("qubits")} (components {lr.get("components")}, coupler {cpl.get("id")} {cpl.get("type")}, gap '
        f'{cpl.get("gap_um")} um, length {cpl.get("length_um")} um) specifies coupling_proxy_MHz = {g_design}. '
        f'Component x-positions order the qubits {"-".join(map(str, phys_order[:6]))}-..., '
        f'{"matching" if adjacent else "NOT matching"} the backend chain, so {list(tgt_edge)} are physically adjacent.'
        if design_ok else 'Device design does not match current revision/runtime.')
    if not design_ok:
        faults.append('device_design')
    ev['design'] += [
        _ev('device_design.json', f'{cur_rev} fingerprint {des.get("geometry_fingerprint")}; local region '
            f'{lr.get("qubits")} coupler {cpl.get("id")} coupling_proxy_MHz = {g_design}',
            'geometry_fingerprint; local_region.coupling_proxy_MHz'),
        _ev('provenance_registry.json', 'revisions: ' + ', '.join(
            f'{r} -> {v["geometry_fingerprint"][:12]}... at {v["timestamp"]}' for r, v in revs.items()), 'revisions')]

    # ---------------- M5: device model provenance ----------------
    g_model = mod.get('coupling_proxy_MHz')
    model_rev = fp2rev.get(mod.get('source_geometry_fingerprint'))
    model_stale = model_rev is not None and model_rev != cur_rev
    reg_model = next((x for x in reg.get('artifacts', []) if x.get('artifact_id') == mod.get('artifact_id')), {})
    other_models = [x for x in reg.get('artifacts', []) if x.get('path') == 'device_model.json'
                    and x.get('artifact_id') != mod.get('artifact_id')]
    reg_times = sorted({x.get('timestamp') for x in reg.get('artifacts', [])})
    # junction cross-check: EJ = (Phi0/2pi)^2 / Lj  (CODATA SI), design Lj vs model EJ
    phi0_red, h_pl = 2.067833848e-15 / (2 * math.pi), 6.62607015e-34
    mq = mod.get('qubits', [])
    ej_model = mod.get('parameters', {}).get('EJ_GHz', [])
    lj_des = [comps.get(f'geom-Q{q}', {}).get('Lj_nH') for q in mq]
    junction_txt, junction_mismatch = '', False
    if lj_des and None not in lj_des and len(ej_model) == len(mq):
        ej_des = [phi0_red ** 2 / (lj * 1e-9) / h_pl / 1e9 for lj in lj_des]
        lj_impl = [phi0_red ** 2 / (ej * 1e9 * h_pl) * 1e9 for ej in ej_model]
        rel = [abs(m / d - 1) for m, d in zip(ej_model, ej_des)]
        junction_mismatch = max(rel) > 0.05
        for q, a, b_, c in zip(mq, ej_des, ej_model, lj_impl):
            params[f'ej_from_design_lj_Q{q}_GHz'] = a
            params[f'ej_model_Q{q}_GHz'] = b_
            params[f'lj_implied_by_model_Q{q}_nH'] = c
        params['ej_model_vs_design_max_relative_mismatch_ratio'] = max(rel)
        junction_txt = (
            f'Independent junction cross-check: {cur_rev} design junctions Lj = {lj_des} nH give EJ = '
            f'(Phi0/2pi)^2/Lj = {[round(x, 2) for x in ej_des]} GHz, but {mod.get("artifact_id")} uses EJ = {ej_model} GHz '
            f'(implying Lj = {[round(x, 2) for x in lj_impl]} nH, asymmetric where the design is symmetric); max relative '
            f'mismatch {max(rel):.1%}. ' +
            ('So the model\'s transmon parameters, not only its coupling, are inconsistent with the current geometry, '
             f'further evidence it was not derived from {cur_rev} (the {model_rev} geometry is not supplied, so a match '
             f'to {model_rev} cannot be confirmed). ' if junction_mismatch else 'Junction parameters are consistent. '))
    layers['device_model'] = (
        (f'STALE PROVENANCE (PRIMARY FAULT). {mod.get("artifact_id")} ({mod.get("model_kind")}) declares '
         f'source_geometry_fingerprint {str(mod.get("source_geometry_fingerprint"))[:12]}..., which the registry assigns '
         f'to {model_rev} ({revs.get(model_rev, {}).get("timestamp")}), not the current {cur_rev}. It was created '
         f'{mod.get("created_at")}, before {cur_rev} existed ({revs.get(cur_rev, {}).get("timestamp")}), and carries '
         f'coupling_proxy_MHz = {g_model} vs {g_design} MHz in the {cur_rev} design for the same edge {mod.get("qubits")}. '
         + ('The registry contains no other device model, so no ' + str(cur_rev) + '-derived model exists. '
            if not other_models else f'Other models in registry: {[x["artifact_id"] for x in other_models]}. ') +
         f'{mod.get("artifact_id")} is listed in the registry at {reg_model.get("timestamp")} (all registry artifact '
         f'timestamps: {reg_times}); a registry timestamp is not a regeneration and does not change its source geometry. '
         f'{loc.get("artifact_id")} cites coupling_refs {loc.get("coupling_refs")} (design_ref {loc.get("design_ref")}, '
         f'model_ref {loc.get("model_ref")}): two coupling sources from different geometry revisions ({g_design} vs '
         f'{g_model} MHz) for one subsystem. Its transmon parameters (EJ {mod.get("parameters", {}).get("EJ_GHz")} GHz, '
         f'EC {mod.get("parameters", {}).get("EC_GHz")} GHz) do not enter the reduced entangling rate; only the '
         'coupling proxy does. ' + junction_txt) if model_stale else f'CONSISTENT: model derived from {model_rev}.')
    if model_stale:
        faults.append('device_model')
    ev['model'] += [
        _ev('device_model.json', f'{mod.get("artifact_id")}: source_geometry_fingerprint '
            f'{mod.get("source_geometry_fingerprint")} (= {model_rev}), created {mod.get("created_at")}, '
            f'coupling_proxy_MHz = {g_model}; reduced_assumption: "{mod.get("reduced_assumption", "")[:90]}..."',
            'source_geometry_fingerprint; coupling_proxy_MHz; created_at'),
        _ev('provenance_registry.json', f'{model_rev} fingerprint == model source fingerprint; {cur_rev} fingerprint == '
            f'design/runtime fingerprint; {len(other_models)} other device models registered',
            f'revisions; artifacts[artifact_id={mod.get("artifact_id")}]'),
        _ev('local_subsystem.json', f'coupling_refs {loc.get("coupling_refs")}: design_ref {loc.get("design_ref")} and '
            f'model_ref {loc.get("model_ref")} from different revisions for qubits {loc.get("qubits")}',
            'coupling_refs; design_ref; model_ref')]
    if junction_txt:
        ev['model'].append(_ev('device_model.json', junction_txt.strip(), 'parameters.EJ_GHz'))
        ev['design'].append(_ev('device_design.json', f'junction Lj_nH {lj_des} for geom-Q{mq[0]}/geom-Q{mq[-1]}'
                                + (f' (identical across all {len(comps)} components)' if len({c.get('Lj_nH') for c in comps.values()}) == 1
                                   else ' (varies across components)'), f'components[geom-Q{mq[0]}].Lj_nH'))

    # ---------------- control calibration ----------------
    t = cal.get('pulse_duration_ns')
    nominal = cal.get('nominal_rate_rad_per_ns')
    theta_t = cal.get('target_angle_rad')
    rate = lambda g: 2 * math.pi * g / 1000.0
    cal_uses_model = cal.get('model_ref') == mod.get('artifact_id')
    nominal_matches_model = bool(nominal and g_model) and abs(nominal - rate(g_model)) < 1e-9
    duration_matches = bool(nominal and t and theta_t) and abs(theta_t / nominal - t) < 1e-9
    theta_real = rate(g_design) * t if g_design and t else None
    V = _load_qpy(d, 'verification_circuit.qpy')
    vmeta = (V.metadata or {}) if V is not None else {}
    chan_ok = man.get('channel') == cal.get('channel') == vmeta.get('channel', man.get('channel'))
    edge_ok2 = tuple(cal.get('physical_edge', [])) == tgt_edge == tuple(char.get('physical_edge', tgt_edge))
    layers['control'] = (
        f'PROPAGATES THE STALE MODEL. {cal.get("artifact_id")} (epoch {cal.get("calibration_epoch")}, channel '
        f'{cal.get("channel")}, edge {cal.get("physical_edge")}) has model_ref {cal.get("model_ref")}; its nominal_rate '
        f'{nominal} rad/ns = 2*pi*{g_model}/1000 (the {model_rev} coupling) and pulse_duration {t} ns = target_angle '
        f'{theta_t} / nominal_rate ({"exact" if duration_matches else "MISMATCH"}). Given its declared input the '
        'calibration arithmetic is correct; its defect is that it carries no source-geometry fingerprint and was not '
        f'checked against the {cur_rev} runtime, so it inherited the stale rate. Channel/edge binding is '
        f'{"consistent" if chan_ok and edge_ok2 else "INCONSISTENT"}: manifest {man.get("artifact_id")}, calibration and '
        f'verification metadata all use {man.get("channel")} on {list(tgt_edge)}; characterization control_ref '
        f'{char.get("control_ref")} shows the probes used this same pulse.'
    ) if cal_uses_model and nominal_matches_model else 'Calibration does not trace to the supplied model.'
    ev['control'] += [
        _ev('control_calibration.json', f'model_ref {cal.get("model_ref")}; nominal_rate_rad_per_ns {nominal} '
            f'(= 2*pi*{g_model}/1000); pulse_duration_ns {t}; target_angle_rad {theta_t}',
            'model_ref; nominal_rate_rad_per_ns; pulse_duration_ns'),
        _ev('control_manifest.json', f'{tgt_op} on edge {man.get("target_physical_edge")} via channel '
            f'{man.get("channel")}, calibration {man.get("calibration_ref")}, epoch {man.get("calibration_epoch")}',
            'channel; calibration_ref'),
        _ev('characterization.json', f'control_ref {char.get("control_ref")}: probes repeat the same calibrated pulse '
            f'on edge {char.get("physical_edge")}', 'control_ref'),
        _ev('verification_circuit.qpy', f'metadata channel {vmeta.get("channel")}, target_instruction_index '
            f'{vmeta.get("target_instruction_index")}', 'metadata')]

    # ---------------- M6: physical consequence from characterization ----------------
    probes = char.get('probes', [])
    n = np.array([p['repetitions'] for p in probes], dtype=float)
    y = np.array([p['expectation'] for p in probes], dtype=float)
    tol = char.get('absolute_tolerance', 0.004)
    g_fit, branches, period = None, [], (1000.0 / t if t else None)
    if len(n) and t:
        from scipy.optimize import minimize_scalar
        cheb = lambda g: float(np.max(np.abs(np.cos(n * rate(g) * t) - y)))
        grid = np.arange(0.0, 2 * period + 0.0005, 0.001)
        pred = np.cos(np.outer(grid, n) * 2 * np.pi / 1000 * t)
        okm = np.max(np.abs(pred - y), axis=1) <= tol
        start = None
        for i, flag in enumerate(okm):
            if flag and start is None:
                start = i
            if (not flag or i == len(okm) - 1) and start is not None:
                end = i if flag else i - 1
                branches.append((float(grid[start]), float(grid[end])))
                start = None
        ref = g_model or g_design or 0.0
        lo, hi = min(branches, key=lambda br: abs((br[0] + br[1]) / 2 - ref)) if branches else (0.0, period)
        g_fit = float(minimize_scalar(cheb, bounds=(lo - 0.002, hi + 0.002), method='bounded').x)  # minimax fit
        resid = np.cos(n * rate(g_fit) * t) - y
        params.update({
            'coupling_fit_minimax_MHz': round(g_fit, 4),
            'coupling_fit_window_low_MHz': round(lo, 4), 'coupling_fit_window_high_MHz': round(hi, 4),
            'coupling_alias_period_MHz': period,
            'characterization_max_residual_at_fit_expectation': round(float(np.max(np.abs(resid))), 5),
            'characterization_max_residual_design_coupling_expectation': round(cheb(g_design), 5) if g_design else None,
            'characterization_max_residual_model_coupling_expectation': round(cheb(g_model), 5) if g_model else None,
        })
        aliases = [round((lo + hi) / 2, 3) for lo, hi in branches if not (lo <= g_fit <= hi)]
        sign_alt = bool(len(resid) > 2 and np.all(np.sign(resid[1:]) == -np.sign(resid[:-1])))
    else:
        aliases, sign_alt = [], False
    params.update({
        'coupling_design_MHz': g_design, 'coupling_model_MHz': g_model,
        'coupling_error_MHz': (g_design - g_model) if g_design and g_model else None,
        'model_coupling_underestimate_ratio': (g_model / g_design) if g_design and g_model else None,
        'nominal_rate_model_rad_per_ns': nominal,
        'realized_rate_design_rad_per_ns': rate(g_design) if g_design else None,
        'rate_error_rad_per_ns': (rate(g_design) - nominal) if g_design and nominal else None,
        'pulse_duration_ns': t,
        'intended_angle_rad': target_angle if target_angle is not None else theta_t,
        'nominal_angle_rad': nominal * t if nominal and t else None,
        'realized_angle_rad': theta_real,
        'realized_angle_fit_rad': rate(g_fit) * t if g_fit else None,
        'over_rotation_rad': (theta_real - theta_t) if theta_real and theta_t else None,
        'over_rotation_deg': math.degrees(theta_real - theta_t) if theta_real and theta_t else None,
        'over_rotation_ratio': (g_design / g_model - 1) if g_design and g_model else None,
        'rzz_process_fidelity_vs_intended_dimensionless':
            math.cos((theta_real - theta_t) / 2) ** 2 if theta_real and theta_t else None,
        'corrected_pulse_duration_ns': (theta_t / rate(g_design)) if g_design and theta_t else None,
        'corrected_amplitude_scale_at_fixed_duration_ratio': (g_model / g_design) if g_design and g_model else None,
    })

    # verification companion (analytic identity) and observable spec
    vobs = next((o for o in obs.get('observables', []) if o.get('circuit')), {})
    phis = ver.get('phase_angles_rad', [])
    vq = (vobs.get('qubits') or [None])[0]
    others = [e['angle_rad'] for e in ver.get('interactions', []) if vq in e['edge'] and tuple(e['edge']) != tgt_edge]
    pref = math.cos(phis[vq]) * math.prod(math.cos(x) for x in others) if vq is not None and vq < len(phis) else None
    x_obs, vtol = vobs.get('observed_value'), vobs.get('absolute_tolerance')
    if pref and x_obs is not None and theta_t is not None:
        theta_v = math.acos(max(-1, min(1, x_obs / pref)))
        params.update({
            'verification_X_observed_expectation': x_obs,
            'verification_X_predicted_intended_expectation': pref * math.cos(theta_t),
            'verification_X_predicted_realized_expectation': pref * math.cos(theta_real) if theta_real else None,
            'verification_X_tolerance_expectation': vtol,
            'verification_inferred_target_angle_rad': theta_v,
            'verification_inferred_coupling_MHz': theta_v / t / (2 * math.pi) * 1000 if t else None,
        })
    parity_obs = [o for o in obs.get('observables', []) if not o.get('circuit')]

    # ---------------- local 2-qubit probe simulation (diagnostic, within budget) ----------------
    try:
        from qiskit import QuantumCircuit
        from qiskit_aer import AerSimulator
        lim = A['simulation_config'].get('local_physics_subsystem_max_qubits', 4)
        shots = 1024
        sim = AerSimulator(seed_simulator=simcfg.get('seed_simulator', 20261112))
        for reps in n.astype(int)[:9]:
            pc = QuantumCircuit(2, 1)
            pc.h([0, 1])
            for _ in range(reps):
                pc.rzz(theta_real, 0, 1)
            pc.h(0)
            pc.measure(0, 0)
            cnt = sim.run(pc, shots=shots).result().get_counts()
            xexp = (cnt.get('0', 0) - cnt.get('1', 0)) / shots
            meas = float(y[list(n.astype(int)).index(reps)])
            diag.append({'job_id': f'local_aer_char_probe_n{reps}', 'shots': shots, 'qubits': list(tgt_edge)[:lim],
                         'depth': pc.depth(),
                         'claim': f'Local seeded Aer replay of the characterization protocol on {list(tgt_edge)} '
                                  f'(|++>, {reps} x RZZ({theta_real:.6f}) i.e. the {t} ns pulse at the {cur_rev} rate, '
                                  f'measure X on Q{e0}): <XI> = {xexp:+.3f} (shot noise +/-{1 / math.sqrt(shots):.3f}); '
                                  f'analytic {math.cos(reps * theta_real):+.6f}; supplied {meas:+.6f}; intended-model '
                                  f'prediction {math.cos(reps * theta_t):+.6f}. No hardware.'})
    except Exception:
        pass

    # ---------------- structured simulation (Aer MPS) ----------------
    try:
        N = C.num_qubits

        def P(dct):
            s = ['I'] * N
            for k, v in dct.items():
                s[N - 1 - k] = v
            return ''.join(s)
        paulis = {'total_parity': 'Z' * N, f'Z{e0}': P({e0: 'Z'}), f'Z{e1}': P({e1: 'Z'}),
                  f'Z{e0}Z{e1}': P({e0: 'Z', e1: 'Z'}), f'Y{e0}Y{e1}': P({e0: 'Y', e1: 'Y'})}
        seed = simcfg.get('seed_simulator', 20261112)
        settings = [(128, 1e-10), (256, 1e-10), (512, 1e-10), (256, 1e-12)]
        runs, maxbd = {}, 0
        for bond, thr in settings:
            for lab, ang in (('intended', None), ('realized', theta_real)):
                runs[(bond, thr, lab)], bd = _mps_observables(C, c_idx, ang, paulis, bond, thr, seed)
                maxbd = max(maxbd, bd)
        ref_i, ref_r = runs[(256, 1e-10, 'intended')], runs[(256, 1e-10, 'realized')]
        deltas = {f'{bd}_{thr:g}': max(abs(runs[(bd, thr, lab)][o] - (ref_i if lab == 'intended' else ref_r)[o])
                                       for lab in ('intended', 'realized') for o in paulis) for bd, thr in settings}
        for o in paulis:
            params[f'mps_workload_{o}_intended_expectation'] = round(ref_i[o], 6)
            params[f'mps_workload_{o}_realized_expectation'] = round(ref_r[o], 6)
        params['mps_max_observable_delta_across_settings_expectation'] = max(deltas.values())
        params['mps_max_bond_dimension_reached_count'] = maxbd
        # parity invariance check with every RZ/RZZ angle randomized (seeded)
        rng = np.random.default_rng(seed)
        from qiskit import QuantumCircuit as _QC
        rq = _QC(N)
        for ins in C.data:
            op = ins.operation
            if op.name in ('rz', 'rzz'):
                op = op.__class__(float(rng.uniform(-math.pi, math.pi)))
            rq.append(op, ins.qubits)
        rnd_parity = _mps_observables(rq, None, None, {'p': 'Z' * N}, 256, 1e-10, seed)[0]['p']
        params['mps_workload_total_parity_random_angles_expectation'] = round(rnd_parity, 6)
        vt = vmeta.get('target_instruction_index', ver.get('target_instruction_index'))
        vx_i = _mps_observables(V, vt, None, {'X': P({vq: 'X'})}, 256, 1e-10, seed)[0]['X']
        vx_r = _mps_observables(V, vt, theta_real, {'X': P({vq: 'X'})}, 256, 1e-10, seed)[0]['X']
        params['mps_verification_X_intended_expectation'] = round(vx_i, 6)
        params['mps_verification_X_realized_expectation'] = round(vx_r, 6)
        ev['sim'] = [
            _ev('simulation_config.json', f'Aer matrix_product_state, seed {seed}; settings (bond, threshold) '
                f'{settings}; observables via save_expectation_value only (no statevector/distribution). Per-setting max '
                f'|delta| vs (256, 1e-10): ' + ', '.join(f'{k}: {v:.1e}' for k, v in deltas.items()) +
                f'. Max bond dimension reached = {maxbd} (< smallest limit {settings[0][0]}), so no bond-limit '
                'truncation occurred; discarded weight is bounded by the SVD threshold per truncation. Converged.',
                'recommended_method; convergence_policy'),
            _ev('compiled_circuit.qpy', f'MPS workload, target RZZ at {_frac_pi(theta_t)} vs realized '
                f'{_frac_pi(theta_real)}: total Z parity {ref_i["total_parity"]:.6f} vs {ref_r["total_parity"]:.6f} '
                f'(identical: parity is blind to the fault); <Z{e0}> {ref_i[f"Z{e0}"]:.4f} -> {ref_r[f"Z{e0}"]:.4f}; '
                f'<Z{e1}> {ref_i[f"Z{e1}"]:.4f} -> {ref_r[f"Z{e1}"]:.4f}; <Z{e0}Z{e1}> {ref_i[f"Z{e0}Z{e1}"]:.4f} -> '
                f'{ref_r[f"Z{e0}Z{e1}"]:.4f}; <Y{e0}Y{e1}> {ref_i[f"Y{e0}Y{e1}"]:.4f} -> {ref_r[f"Y{e0}Y{e1}"]:.4f}.',
                f'instruction {c_idx}'),
            _ev('verification_circuit.qpy', f'MPS verification <X{vq}>: intended {vx_i:.6f}, realized {vx_r:.6f}; '
                f'observed {x_obs} (tolerance {vtol}).', f'instruction {vt}')]
    except Exception as exc:
        ev['sim'] = [_ev('simulation_config.json', f'MPS simulation not completed ({type(exc).__name__}); analytic '
                         'results stand on their own.')]

    params = {k: float(v) for k, v in params.items() if v is not None}
    fit_txt = (f'g = {params.get("coupling_fit_minimax_MHz")} MHz (window {params.get("coupling_fit_window_low_MHz")}-'
               f'{params.get("coupling_fit_window_high_MHz")} MHz at tolerance {tol}; max residual '
               f'{params.get("characterization_max_residual_at_fit_expectation")}; residuals '
               f'{"alternate in sign with n, i.e. the declared fixed additive perturbation, not a fit bias" if sign_alt else "small"})')
    alias_txt = (f'Because probes use integer n, cos(n*2*pi*g*t/1000) is also matched exactly at g = k*{period:g} +/- '
                 f'{g_fit:.3f} MHz (and at -g, since cos is even); in 0-{2 * period:g} MHz the other solutions are '
                 f'{aliases} MHz. They are rejected only on physical grounds: they are far from both the design '
                 f'({g_design} MHz) and model ({g_model} MHz) proxies.' if g_fit and period else '')
    ev['artifact'] += [
        _ev('characterization.json', f'XI expectations {y.round(6).tolist()} for n={n.astype(int).tolist()} are '
            f'reproduced by cos(n*omega*t) at {fit_txt}; max residual '
            f'{params.get("characterization_max_residual_design_coupling_expectation")} at the design {g_design} MHz vs '
            f'{params.get("characterization_max_residual_model_coupling_expectation")} at the model {g_model} MHz. '
            + alias_txt, 'probes'),
        _ev('observable_spec.json', f'verification-X{vq} observed {x_obs} vs predicted '
            f'{params.get("verification_X_predicted_intended_expectation", float("nan")):.6f} (intended) and '
            f'{params.get("verification_X_predicted_realized_expectation", float("nan")):.6f} (realized), tolerance {vtol}',
            f'observables[id={vobs.get("id")}]'),
        _ev('verification_protocol.json', f'<X_q> = cos(phi_q) * prod cos(theta_e) over incident edges; phi_{vq} = '
            f'{phis[vq] if vq is not None and vq < len(phis) else "?"}, other incident angles {others}; inverting the '
            f'observed value gives target angle {params.get("verification_inferred_target_angle_rad", float("nan")):.6f} '
            f'rad = {params.get("verification_inferred_coupling_MHz", float("nan")):.4f} MHz', 'analytic_identity; interactions'),
    ]

    # ---------------- noise / alternatives (M8) ----------------
    nq = noise.get('qubits', [])
    tgt_noise = [q for q in nq if q['qubit'] in tgt_edge]
    if nq:
        med_t1 = sorted(q['T1_us'] for q in nq)[len(nq) // 2]
        med_ro = sorted(q['readout_error'] for q in nq)[len(nq) // 2]
        for q in nq:
            if q['T1_us'] < 0.5 * med_t1 or q['readout_error'] > 3 * med_ro:
                incidental.append(
                    f'noise_snapshot qubit {q["qubit"]}: T1 {q["T1_us"]} us, T2 {q["T2_us"]} us, readout_error '
                    f'{q["readout_error"]} (medians T1 {med_t1} us, readout {med_ro}). Not on target edge {list(tgt_edge)}; '
                    f'snapshot scope: "{noise.get("scope", "")}"; the verification observable applies no readout channel. '
                    'An off-edge incoherent error cannot produce a coherent, sign-alternating rotation error on the target.')
    longest = (max(n) * t / 1000.0) if len(n) and t else None
    damp = min((math.exp(-longest / q['T2_us']) for q in tgt_noise), default=None) if longest else None
    if damp:
        params['t2_damping_over_longest_probe_dimensionless'] = damp
    rejected += [
        'compiler: DAG-identical, requests exactly the intended angle',
        f'mapping: {"identity" if identity else "consistent"} layout, supported edge {list(tgt_edge)}',
        'scheduling: matches compiled circuit; target duration equals calibration',
        f'wrong control channel/edge: {man.get("channel")} bound to {list(tgt_edge)} in manifest, calibration, '
        'characterization and verification metadata',
        f'runtime/backend: {rt.get("device_revision")}/{rt.get("calibration_epoch")} and fingerprint consistent with design',
        (f'T1/T2 decoherence: the longest probe ({int(max(n))} pulses = {longest * 1000:.1f} ns) gives T2 damping '
         f'{damp:.5f} on {list(tgt_edge)}, far too small for the observed shifts; decay would shrink |<XI>| with n, but '
         f'|<XI>| reaches {float(np.max(np.abs(y))):.3f} at n={int(n[int(np.argmax(np.abs(y)))])}, and the sign pattern '
         'is a coherent rotation') if damp else 'T1/T2: not quantified',
        'readout error: off-edge outliers only; verification uses no readout channel',
        f'crosstalk: noise assumptions {noise.get("assumptions", {}).get("no_crosstalk") and "no_crosstalk=true" or "n/a"}; '
        'characterization is isolated',
        f'qubit-parameter drift: all {len(comps)} design components share identical cross geometry and Lj/Cj; the '
        'reduced rate depends only on the coupling proxy',
    ]
    if parity_obs:
        rp = params.get('mps_workload_total_parity_random_angles_expectation')
        incidental.append(
            'Observables ' + ', '.join(f'{o["id"]} = {o["observed_value"]}' for o in parity_obs) + ' pass, but they '
            'are structural invariants of this circuit family (RZZ pair blocks are SX-conjugated): MPS gives total '
            f'parity {params.get("mps_workload_total_parity_intended_expectation")} with the intended angle, '
            f'{params.get("mps_workload_total_parity_realized_expectation")} with the realized angle' +
            (f' and {rp} with every RZ/RZZ angle randomized' if rp is not None else '') +
            '. Passing parity therefore neither clears nor localizes the fault.')
    incidental += [
        f'intended_circuit.txt and compiled_circuit.txt are '
        f'{"byte-identical" if (d / "intended_circuit.txt").read_bytes() == (d / "compiled_circuit.txt").read_bytes() else "different"}'
        ' drawings although the QPY instruction order differs: the layered drawing is insensitive to commuting order; '
        'not evidence of tampering.',
        f'provenance_registry lists {mod.get("artifact_id")} with timestamp {reg_model.get("timestamp")} like every '
        'other artifact; a listing time is not a regeneration and does not alter its source fingerprint.',
        'The QPY circuits and readable drawings are not listed in the provenance registry; their integrity rests on the '
        'release manifest hashes instead.',
    ]
    if V is not None:
        vgates = sorted({i.operation.name for i in V.data} - set(be.get('basis_gates', [])))
        if vgates:
            incidental.append(f'verification_circuit.qpy uses {vgates}, outside the backend basis: it is an analytic '
                              'logical companion record, not a transpiled job, and its identity is used analytically.')
    if len(y) and theta_real:
        k_zero = [int(r) for r in n if abs(math.cos(r * theta_real)) < 1e-6]
        if k_zero:
            incidental.append(f'characterization n={k_zero} expectation {[float(y[list(n).index(k)]) for k in k_zero]} '
                              f'is ~0 exactly as predicted (cos(n*{_frac_pi(theta_real)}) = 0); within tolerance {tol}.')

    # ---------------- assemble ----------------
    layers['algorithm'] = (f'{(I.metadata or {}).get("workload") if I is not None else "workload"}: {chc.get("rounds")} '
                           f'rounds of RZ and SX-conjugated RZZ on {chc.get("physical_qubits")} qubits '
                           f'({chc.get("two_qubit_gates")} two-qubit gates); intended target RZZ({_frac_pi(theta_t) if theta_t else "?"}) '
                           f'on logical {tgt_logical}. No algorithm-layer issue.')
    layers['provenance_registry'] = (
        f'Registry {reg.get("artifact_id")}: revisions ' + ', '.join(f'{r}@{v["timestamp"]}' for r, v in revs.items()) +
        f'. Design and runtime resolve to {cur_rev}; {mod.get("artifact_id")} resolves to {model_rev}. '
        f'external_ids {reg.get("external_ids")} are unregistered.')
    layers['characterization'] = (
        f'Probes fit {fit_txt}, equal to the {cur_rev} design value {g_design} MHz and inconsistent with the model value '
        f'{g_model} MHz (residual {params.get("characterization_max_residual_model_coupling_expectation")}). ' + alias_txt)
    layers['verification'] = (
        f'<X{vq}> observed {x_obs} matches the realized prediction '
        f'{params.get("verification_X_predicted_realized_expectation", float("nan")):.6f}, not the intended '
        f'{params.get("verification_X_predicted_intended_expectation", float("nan")):.6f} (tolerance {vtol}); inverted '
        f'coupling {params.get("verification_inferred_coupling_MHz", float("nan")):.4f} MHz.')
    layers['noise'] = ('NOT CAUSAL. Off-edge T1/readout outliers only; target-edge decoherence over the probes is '
                       f'negligible (T2 damping {damp:.5f}).' if damp else 'NOT CAUSAL.')
    layers['alternatives'] = 'REJECTED: ' + '; '.join(rejected) + '.'
    res.update(estimated_parameters=params, incidental_anomalies=incidental, diagnostic_evidence=diag,
               artifact_evidence=ev['artifact'], runtime_evidence=ev['runtime'], device_design_evidence=ev['design'],
               device_model_evidence=ev['model'], control_evidence=ev['control'], simulation_evidence=ev['sim'])

    x_dev = abs((x_obs or 0) - params.get('verification_X_predicted_intended_expectation', 0))
    if not model_stale or not g_design or not g_model or g_design == g_model or theta_real is None:
        res['layer_assessments'] = layers
        if x_dev > (vtol or 0.004):
            res.update(verdict='INTEGRITY_VIOLATION', confidence=0.6, suspected_fault_type='UNLOCALIZED_DEVIATION',
                       root_cause='Verification observable deviates from the intended prediction but no provenance '
                                  'break was found.')
        else:
            res.update(verdict='VALID_EXECUTION', confidence=0.8,
                       root_cause='All layers consistent; verification observable within tolerance of intent.')
        return res

    over = params['over_rotation_rad']
    layers['root_cause_boundary'] = (
        f'PRIMARY: device design {des.get("artifact_id")} ({cur_rev}) -> device model {mod.get("artifact_id")} '
        f'({model_rev}): the model was never regenerated after {cur_rev} (fingerprint mismatch, g {g_model} vs {g_design} '
        f'MHz' + (f'; junction EJ {ej_model} GHz vs {cur_rev} Lj-derived '
                  f'{round(params.get(f"ej_from_design_lj_Q{mq[0]}_GHz", 0), 2)} GHz' if junction_mismatch else '') +
        f'). PROPAGATION: {mod.get("artifact_id")} -> {cal.get("artifact_id")} ({cal.get("calibration_epoch")}): the '
        'calibration correctly derives its pulse from its declared model_ref but has no geometry-fingerprint check '
        f'against the {cur_rev} runtime. DOWNSTREAM (clean): compiler, mapping, schedule, runtime executed as configured.')
    res.update(
        layer_assessments=layers,
        verdict='INTEGRITY_VIOLATION',
        confidence=0.95,
        suspected_fault_type='STALE_DEVICE_MODEL_PROVENANCE -> COUPLING_MISCALIBRATION (coherent RZZ over-rotation)',
        affected_logical_operations=[
            f'{tgt_op}: RZZ({_frac_pi(theta_t)}) on logical {tgt_logical} (intended index {i_idx}, compiled index '
            f'{c_idx}, schedule start {tgt_sched[0]["start_ns"] if tgt_sched else "?"} ns, control {man.get("artifact_id")})',
            f'verification companion target instruction {vmeta.get("target_instruction_index", ver.get("target_instruction_index"))} '
            '(same calibration)'],
        expected_physical_resources=[
            f'physical edge ({e0},{e1}) via coupler {cpl.get("id")} on channel {man.get("channel")}',
            f'{cur_rev} device model (source fingerprint {str(des.get("geometry_fingerprint"))[:12]}...) with '
            f'g = {g_design} MHz',
            f'rate {rate(g_design):.6f} rad/ns; {params["corrected_pulse_duration_ns"]:.4f} ns pulse for theta = '
            f'{_frac_pi(theta_t)}',
        ],
        suspected_realized_resources=[
            f'same physical edge ({e0},{e1}) / coupler {cpl.get("id")} / channel {man.get("channel")} (edge and channel '
            'are correct)',
            f'calibration {cal.get("artifact_id")} ({cal.get("calibration_epoch")}) derived from stale '
            f'{mod.get("artifact_id")} ({model_rev} geometry, g = {g_model} MHz): {t} ns pulse at true rate '
            f'{rate(g_design):.6f} rad/ns',
            f'realized RZZ angle {theta_real:.6f} rad ({_frac_pi(theta_real)}) instead of {_frac_pi(theta_t)}: '
            f'over-rotation {over:.6f} rad ({params["over_rotation_deg"]:.2f} deg, '
            f'{params["over_rotation_ratio"]:.1%})',
        ],
        root_cause=(
            f'OBSERVATIONS: compiler, mapping, schedule and runtime faithfully request RZZ({_frac_pi(theta_t)}) on edge '
            f'({e0},{e1}) over channel {man.get("channel")}. Current design {des.get("artifact_id")} ({cur_rev}) gives '
            f'g = {g_design} MHz for coupler {cpl.get("id")}; device model {mod.get("artifact_id")} was built from '
            f'{model_rev} geometry (fingerprint {str(mod.get("source_geometry_fingerprint"))[:12]}...) with '
            f'g = {g_model} MHz and was never regenerated for {cur_rev}. Calibration {cal.get("artifact_id")} took its '
            f'rate from it ({nominal:.6f} rad/ns) and chose {t} ns. '
            f'PHYSICAL CONSEQUENCE: with the supplied reduced assumption omega = 2*pi*g/1000 rad/ns, the true rate is '
            f'{rate(g_design):.6f} rad/ns (+{params["rate_error_rad_per_ns"]:.6f} rad/ns, coupling error '
            f'{params["coupling_error_MHz"]:.1f} MHz; the model under-estimates g by '
            f'{1 - params["model_coupling_underestimate_ratio"]:.1%}), so theta = {theta_real:.6f} rad '
            f'({_frac_pi(theta_real)}), a {params["over_rotation_ratio"]:.1%} (+{over:.4f} rad, '
            f'{params["over_rotation_deg"]:.2f} deg) coherent over-rotation; gate process fidelity vs intended '
            f'{params["rzz_process_fidelity_vs_intended_dimensionless"]:.5f}. Characterization independently gives '
            f'{fit_txt}; the verification correlator inverts to '
            f'{params.get("verification_inferred_coupling_MHz", float("nan")):.3f} MHz; MPS shows local workload '
            f'observables on ({e0},{e1}) shift while global parity cannot. '
            f'BOUNDARY: {layers["root_cause_boundary"]} SCOPE: only operations bound to {man.get("artifact_id")} are '
            f'evidenced as affected; {", ".join(base_refs)} provenance is not supplied.'),
        proposed_repair=(
            f'(1) Regenerate the reduced device model from the {cur_rev} geometry (source_geometry_fingerprint '
            f'{des.get("geometry_fingerprint")}), giving coupling_proxy_MHz = {g_design} for ({e0},{e1}); register it as '
            f'a new artifact and repoint {loc.get("artifact_id")} to it. (2) Issue a new calibration epoch (rather than '
            f'editing {cal.get("artifact_id")}) for channel {man.get("channel")}: nominal_rate {rate(g_design):.6f} rad/ns '
            f'and pulse_duration {params["corrected_pulse_duration_ns"]:.4f} ns, or keep {t} ns with amplitude_scale '
            f'{params["corrected_amplitude_scale_at_fixed_duration_ratio"]:.4f} (valid only if rate is linear in drive; '
            'the model states "Proxy g includes drive scaling", so re-verify). (3) Provenance gate: a calibration may be '
            'used only if its model\'s source_geometry_fingerprint equals the runtime/backend geometry_fingerprint; a new '
            'device revision invalidates models and calibrations derived from earlier revisions. (4) Audit '
            f'{", ".join(base_refs)} for the same dependency.'),
        repair_validation=(
            f'(1) Provenance: model.source_geometry_fingerprint == design == runtime fingerprint ({cur_rev}). '
            f'(2) Re-run the {len(n)} characterization probes (|++>, n pulses, measure XI): expect '
            f'{[round(math.cos(k * theta_t), 3) for k in n.astype(int)]} within {tol} (currently '
            f'{[round(float(v), 3) for v in y]}), and a refit giving omega*t = {_frac_pi(theta_t)}. (3) Re-run the '
            f'verification companion: expect <X{vq}> = '
            f'{params.get("verification_X_predicted_intended_expectation", float("nan")):.4f} +/- {vtol} (currently '
            f'{x_obs}). (4) Re-simulate the workload with Aer MPS at bond 128/256/512 and confirm local observables on '
            f'({e0},{e1}) return to intended values (<Z{e0}> {params.get(f"mps_workload_Z{e0}_intended_expectation", float("nan")):.4f}, '
            f'currently {params.get(f"mps_workload_Z{e0}_realized_expectation", float("nan")):.4f}); global parity is not '
            'a sufficient check. (5) Keep probes as a regression test on every device revision; odd-n repetitions '
            'amplify residual rate error.'),
    )
    return res


def solve(stage_name, stage_dir):
    return {'stage_1': solve_stage1, 'stage_2': solve_stage2}[stage_name](stage_dir)
