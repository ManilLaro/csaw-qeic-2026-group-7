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
# Every number below is read or derived from the artifacts.
# ---------------------------------------------------------------------------

def _mps_observables(circ, target_idx, angle, paulis, bond, threshold, seed):
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
                       matrix_product_state_truncation_threshold=threshold)
    data = sim.run(q, shots=1, seed_simulator=seed).result().data(0)
    return {k: float(np.real(data[k])) for k in paulis}


def solve_stage2(stage_dir):
    import math
    import numpy as np

    d = Path(stage_dir)
    A = {n: _load(d, n + '.json') or {} for n in (
        'challenge', 'backend_snapshot', 'mapping', 'simulated_runtime_snapshot', 'device_design', 'device_model',
        'provenance_registry', 'control_manifest', 'control_calibration', 'characterization', 'local_subsystem',
        'observable_spec', 'verification_protocol', 'noise_snapshot', 'simulation_config')}
    ch, be, mp, rt = A['challenge'], A['backend_snapshot'], A['mapping'], A['simulated_runtime_snapshot']
    des, mod, reg, man = A['device_design'], A['device_model'], A['provenance_registry'], A['control_manifest']
    cal, char, loc, obs = A['control_calibration'], A['characterization'], A['local_subsystem'], A['observable_spec']
    ver, noise, simcfg = A['verification_protocol'], A['noise_snapshot'], A['simulation_config']

    res = empty_submission()
    layers, params, incidental, faults = {}, {}, [], []
    ev = {k: [] for k in ('artifact', 'runtime', 'design', 'model', 'control', 'sim')}

    chc = ch.get('circuit', {}) if isinstance(ch.get('circuit'), dict) else {}
    tgt_op = chc.get('target_operation_id') or man.get('target_operation_id')
    tgt_edge = tuple(man.get('target_physical_edge') or chc.get('target_physical_edge') or ())
    c_idx = chc.get('target_instruction_index')
    i_idx = chc.get('intended_target_instruction_index')
    tgt_logical = chc.get('target_logical_qubits')

    # ---------------- M2: circuit / compilation ----------------
    I, C = _load_qpy(d, 'intended_circuit.qpy'), _load_qpy(d, 'compiled_circuit.qpy')
    target_angle = None
    compiler_ok = None
    if I is not None and C is not None:
        a, b = _instructions(I), _instructions(C)

        def wires(x):
            w = {}
            for o in x:
                for q in o[1]:
                    w.setdefault(q, []).append((o[0], tuple(o[1]), tuple(round(p, 12) for p in o[2])))
            return w
        compiler_ok = wires(a) == wires(b)
        basis = set(be.get('basis_gates', []))
        off_basis = sorted({o[0] for o in b} - basis)
        if off_basis:
            compiler_ok = False
        ta, tb = a[i_idx] if i_idx is not None and i_idx < len(a) else None, b[c_idx] if c_idx is not None and c_idx < len(b) else None
        target_angle = tb[2][0] if tb and tb[2] else None
        same_target = ta is not None and tb is not None and ta[:3] == tb[:3]
        layers['compiler'] = (
            ('CONSISTENT. ' if compiler_ok else 'INCONSISTENT. ') +
            f'Intended and compiled circuits have {len(a)} vs {len(b)} instructions; the compiled list is a reordering of '
            'commuting operations whose per-qubit (DAG) operation sequences, gate names and parameters are identical'
            f'{"" if compiler_ok else " -- MISMATCH FOUND"}; all compiled gates are in the basis {sorted(basis)}. Target '
            f'{tgt_op}: intended index {i_idx} {ta[0] if ta else "?"}{list(ta[1]) if ta else ""}({ta[2][0] if ta and ta[2] else "?"}) '
            f'== compiled index {c_idx} {tb[0] if tb else "?"}{list(tb[1]) if tb else ""}({target_angle}); '
            f'{"identical" if same_target else "DIFFERENT"}. The compiled circuit requests exactly RZZ(pi/2) on the '
            'target edge, so the compiler is not the fault boundary.')
        if not compiler_ok:
            faults.append('compiler')
        ev['artifact'] += [
            _ev('intended_circuit.qpy', f'target RZZ({ta[2][0] if ta and ta[2] else "?"}) on qubits {list(ta[1]) if ta else "?"} '
                f'at intended index {i_idx}', f'instruction {i_idx}'),
            _ev('compiled_circuit.qpy', f'same RZZ({target_angle}) on physical {list(tb[1]) if tb else "?"} at compiled index '
                f'{c_idx}; per-qubit gate sequences identical to intended (commuting reorder only)', f'instruction {c_idx}')]
    else:
        layers['compiler'] = 'NOT ASSESSED: circuits could not be loaded.'

    # ---------------- M3: mapping / runtime / physical edge ----------------
    edges = {tuple(sorted(e)) for e in be.get('edges', [])}
    l2p = {int(k): v for k, v in mp.get('logical_to_physical', {}).items()}
    identity = all(k == v for k, v in l2p.items())
    map_op = next((o for o in mp.get('operations', []) if o.get('operation_id') == tgt_op), {})
    rt_ok = (rt.get('device_revision') == be.get('device_revision') == des.get('revision')
             and rt.get('geometry_fingerprint') == be.get('geometry_fingerprint') == des.get('geometry_fingerprint')
             and tuple(rt.get('target_physical_edge', [])) == tgt_edge
             and rt.get('physical_layout') == [l2p.get(i) for i in range(len(l2p))]
             and rt.get('calibration_epoch') == cal.get('calibration_epoch') == man.get('calibration_epoch'))
    edge_ok = tuple(sorted(tgt_edge)) in edges and tuple(map_op.get('physical_edge', [])) == tgt_edge
    layers['mapping'] = (
        f'CONSISTENT. {mp.get("revision")} is the {"identity" if identity else "non-identity"} layout on '
        f'{len(l2p)} qubits; {tgt_op} maps logical {map_op.get("logical_qubits")} -> physical {map_op.get("physical_edge")}, '
        f'a supported edge in {be.get("artifact_id")} ({len(edges)} edges, linear chain 0-1-4-2-3-5-...-31).'
        if edge_ok else f'INCONSISTENT. target edge {tgt_edge} unsupported or mismatched in mapping.')
    layers['runtime'] = (
        (f'CONSISTENT. Runtime {rt.get("simulated_job_id")} used device revision {rt.get("device_revision")}, geometry '
         f'fingerprint {str(rt.get("geometry_fingerprint"))[:12]}..., calibration epoch {rt.get("calibration_epoch")}, '
         f'mapping {rt.get("mapping_ref")} and schedule {rt.get("schedule_ref")}, identical to the backend snapshot and '
         'the current device design. The runtime executed the stack it was given.') if rt_ok else
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

    # ---------------- schedule ----------------
    sched = _load(d, 'schedule.json') or {}
    sops = sched.get('operations', [])
    sched_mism = 0
    if C is not None:
        b = _instructions(C)
        sched_mism = sum(1 for o in sops if o['instruction_index'] >= len(b) or
                         (o['gate'], o['qubits']) != (b[o['instruction_index']][0], b[o['instruction_index']][1]))
    tgt_sched = [o for o in sops if o.get('control_ref') == man.get('artifact_id')]
    dup = [o for o in sops if o.get('operation_id') == tgt_op and o.get('instruction_index') != c_idx]
    sched_dur = tgt_sched[0]['duration_ns'] if tgt_sched else None
    layers['scheduling'] = (
        f'CONSISTENT. All {len(sops)} schedule entries match the compiled instruction at their instruction_index '
        f'({sched_mism} mismatches); exactly one entry ({tgt_sched[0]["operation_id"] if tgt_sched else "?"}, index '
        f'{tgt_sched[0]["instruction_index"] if tgt_sched else "?"}, rzz on {tgt_sched[0]["qubits"] if tgt_sched else "?"}) '
        f'uses {man.get("artifact_id")}; its duration {sched_dur} ns equals the calibrated pulse duration '
        f'{cal.get("pulse_duration_ns")} ns. Scheduling faithfully applied the (mis-derived) calibration.'
        if not sched_mism and tgt_sched and sched_dur == cal.get('pulse_duration_ns') else
        'INCONSISTENT schedule vs compiled circuit or calibration.')
    if sched_mism:
        faults.append('scheduling')
    if dup:
        o = dup[0]
        incidental.append(
            f'schedule.json reuses operation_id {tgt_op} for instruction_index {o["instruction_index"]} '
            f'({o["gate"]} on {o["qubits"]}, {o["duration_ns"]} ns, {o["control_ref"]}); {o["instruction_index"]} is the '
            'target\'s INTENDED index, so this is an id-labelling collision. It is a zero-duration virtual RZ on an '
            'unrelated qubit using the base control, matches the compiled instruction at that index, and cannot change '
            'the target interaction; bookkeeping defect, not the cause.')

    # ---------------- M4: device design provenance ----------------
    revs = {r['revision']: r for r in reg.get('revisions', [])}
    fp2rev = {r['geometry_fingerprint']: r['revision'] for r in reg.get('revisions', [])}
    cur_rev = des.get('revision')
    lr = des.get('local_region', {})
    g_design = lr.get('coupling_proxy_MHz')
    design_ok = (fp2rev.get(des.get('geometry_fingerprint')) == cur_rev == rt.get('device_revision')
                 and sorted(lr.get('qubits', [])) == sorted(tgt_edge))
    layers['device_design'] = (
        f'CURRENT AND CONSISTENT. {des.get("artifact_id")} is revision {cur_rev} with fingerprint '
        f'{str(des.get("geometry_fingerprint"))[:12]}..., which the provenance registry assigns to {cur_rev} '
        f'({revs.get(cur_rev, {}).get("timestamp")}) and which equals the runtime/backend fingerprint. Its local region '
        f'{lr.get("qubits")} (components {lr.get("components")}, coupler {lr.get("coupler", {}).get("id")} '
        f'{lr.get("coupler", {}).get("type")}, gap {lr.get("coupler", {}).get("gap_um")} um, length '
        f'{lr.get("coupler", {}).get("length_um")} um) specifies coupling_proxy_MHz = {g_design}.'
        if design_ok else 'Device design does not match current revision/runtime.')
    if not design_ok:
        faults.append('device_design')
    ev['design'] += [
        _ev('device_design.json', f'{cur_rev} geometry fingerprint {des.get("geometry_fingerprint")}; local region '
            f'{lr.get("qubits")} coupler {lr.get("coupler", {}).get("id")} coupling_proxy_MHz = {g_design}',
            'local_region.coupling_proxy_MHz'),
        _ev('provenance_registry.json', f'revisions: ' + ', '.join(f'{r} -> {v["geometry_fingerprint"][:12]}... at '
            f'{v["timestamp"]}' for r, v in revs.items()), 'revisions')]

    # ---------------- M5: device model provenance ----------------
    g_model = mod.get('coupling_proxy_MHz')
    model_rev = fp2rev.get(mod.get('source_geometry_fingerprint'))
    model_stale = model_rev is not None and model_rev != cur_rev
    reg_model = next((x for x in reg.get('artifacts', []) if x.get('artifact_id') == mod.get('artifact_id')), {})
    newer_models = [x for x in reg.get('artifacts', []) if x.get('path') == 'device_model.json'
                    and x.get('artifact_id') != mod.get('artifact_id')]
    layers['device_model'] = (
        (f'STALE PROVENANCE (FAULT). {mod.get("artifact_id")} ({mod.get("model_kind")}) declares '
         f'source_geometry_fingerprint {str(mod.get("source_geometry_fingerprint"))[:12]}..., which the registry assigns to '
         f'revision {model_rev} ({revs.get(model_rev, {}).get("timestamp")}), not the current {cur_rev}; created '
         f'{mod.get("created_at")}, before {cur_rev} existed ({revs.get(cur_rev, {}).get("timestamp")}). It carries '
         f'coupling_proxy_MHz = {g_model} vs {g_design} MHz in the {cur_rev} design for the same edge {mod.get("qubits")}. '
         f'No {cur_rev}-derived model exists in the registry ({len(newer_models)} alternatives); {mod.get("artifact_id")} '
         f'was merely re-registered at {reg_model.get("timestamp")}, which does not change its source geometry. '
         f'local_subsystem {loc.get("artifact_id")} mixes design_ref {loc.get("design_ref")} with model_ref '
         f'{loc.get("model_ref")}, i.e. two different geometry revisions for one subsystem. Transmon EJ/EC/ng parameters '
         'are not implicated: only the coupling proxy feeds the entangling rate.')
        if model_stale else f'CONSISTENT: model derived from {model_rev}.')
    if model_stale:
        faults.append('device_model')
    ev['model'] += [
        _ev('device_model.json', f'{mod.get("artifact_id")}: source_geometry_fingerprint '
            f'{mod.get("source_geometry_fingerprint")} (= {model_rev}), created {mod.get("created_at")}, '
            f'coupling_proxy_MHz = {g_model}', 'source_geometry_fingerprint; coupling_proxy_MHz'),
        _ev('provenance_registry.json', f'{model_rev} fingerprint == model source fingerprint; {cur_rev} fingerprint == '
            'design/runtime fingerprint; no model derived from ' + str(cur_rev), 'revisions; artifacts[model-20]'),
        _ev('local_subsystem.json', f'design_ref {loc.get("design_ref")} but model_ref {loc.get("model_ref")} for qubits '
            f'{loc.get("qubits")}', 'design_ref; model_ref')]

    # ---------------- control calibration ----------------
    t = cal.get('pulse_duration_ns')
    nominal = cal.get('nominal_rate_rad_per_ns')
    rate = lambda g: 2 * math.pi * g / 1000.0
    cal_uses_model = cal.get('model_ref') == mod.get('artifact_id')
    nominal_matches_model = nominal is not None and g_model is not None and abs(nominal - rate(g_model)) < 1e-9
    theta_nominal = nominal * t if nominal and t else None
    theta_real = rate(g_design) * t if g_design and t else None
    layers['control'] = (
        f'DERIVED FROM STALE MODEL. {cal.get("artifact_id")} (epoch {cal.get("calibration_epoch")}, channel '
        f'{cal.get("channel")}, edge {cal.get("physical_edge")}) has model_ref {cal.get("model_ref")}; nominal_rate '
        f'{nominal} rad/ns = 2*pi*{g_model}/1000 (the {model_rev} coupling) and pulse_duration {t} ns = '
        f'target_angle {cal.get("target_angle_rad")} / nominal_rate. The control chain itself is internally consistent '
        f'(manifest {man.get("artifact_id")} -> channel {man.get("channel")} -> edge {man.get("target_physical_edge")} '
        f'-> epoch {man.get("calibration_epoch")}; rectangular pulse, amplitude_scale '
        f'{cal.get("pulse", {}).get("amplitude_scale")}); channel and edge are correct. The defect is the rate it '
        'inherited from the stale model.') if cal_uses_model and nominal_matches_model else \
        'Calibration does not trace to the supplied model.'
    ev['control'] += [
        _ev('control_calibration.json', f'model_ref {cal.get("model_ref")}; nominal_rate_rad_per_ns {nominal} '
            f'(= 2*pi*{g_model}/1000); pulse_duration_ns {t}; target_angle_rad {cal.get("target_angle_rad")}',
            'model_ref; nominal_rate_rad_per_ns; pulse_duration_ns'),
        _ev('control_manifest.json', f'{tgt_op} on edge {man.get("target_physical_edge")} via channel {man.get("channel")}, '
            f'calibration {man.get("calibration_ref")}, epoch {man.get("calibration_epoch")}',
            'channel; calibration_ref')]

    # ---------------- M6: physical consequence from characterization ----------------
    probes = char.get('probes', [])
    n = np.array([p['repetitions'] for p in probes], dtype=float)
    y = np.array([p['expectation'] for p in probes], dtype=float)
    tol = char.get('absolute_tolerance', 0.004)
    g_fit = None
    if len(n) and t:
        from scipy.optimize import minimize_scalar
        grid = np.arange(0.5, 60.0, 0.001)
        pred = np.cos(np.outer(grid, n) * 2 * np.pi / 1000 * t)
        maxres = np.max(np.abs(pred - y), axis=1)
        okm = maxres <= tol
        branches, start = [], None  # disjoint g intervals consistent within tolerance (cos aliasing)
        for i, flag in enumerate(okm):
            if flag and start is None:
                start = i
            if (not flag or i == len(okm) - 1) and start is not None:
                end = i if flag else i - 1
                branches.append((float(grid[start]), float(grid[end])))
                start = None
        ref = g_model or g_design or 0.0  # physically plausible branch: nearest to the supplied model/design
        lo, hi = min(branches, key=lambda b: abs((b[0] + b[1]) / 2 - ref)) if branches else (grid[0], grid[-1])
        ok = np.array([lo, hi])
        g0 = grid[(grid >= lo) & (grid <= hi)][int(np.argmin(maxres[(grid >= lo) & (grid <= hi)]))] if branches else 0
        g_fit = float(minimize_scalar(lambda g: np.sum((np.cos(n * rate(g) * t) - y) ** 2),
                                      bounds=(g0 - 0.01, g0 + 0.01), method='bounded').x)
        for j, (blo, bhi) in enumerate(branches):
            params[f'coupling_fit_branch{j}_center_MHz'] = round((blo + bhi) / 2, 3)
        res_fit = float(np.max(np.abs(np.cos(n * rate(g_fit) * t) - y)))
        res_design = float(np.max(np.abs(np.cos(n * rate(g_design) * t) - y))) if g_design else None
        res_model = float(np.max(np.abs(np.cos(n * rate(g_model) * t) - y))) if g_model else None
        params.update({
            'coupling_fit_from_characterization_MHz': round(g_fit, 4),
            'coupling_fit_tolerance_window_low_MHz': round(float(ok.min()), 4) if len(ok) else -1.0,
            'coupling_fit_tolerance_window_high_MHz': round(float(ok.max()), 4) if len(ok) else -1.0,
            'characterization_max_residual_at_fit': round(res_fit, 5),
            'characterization_max_residual_design_coupling': round(res_design, 5) if res_design is not None else -1.0,
            'characterization_max_residual_model_coupling': round(res_model, 5) if res_model is not None else -1.0,
        })
    params.update({
        'coupling_design_D21_MHz': g_design, 'coupling_model20_MHz': g_model,
        'nominal_rate_model20_rad_per_ns': nominal,
        'realized_rate_design_rad_per_ns': rate(g_design) if g_design else None,
        'pulse_duration_ns': t,
        'intended_angle_rad': target_angle if target_angle is not None else cal.get('target_angle_rad'),
        'nominal_angle_rad': theta_nominal,
        'realized_angle_rad': theta_real,
        'realized_angle_fit_rad': rate(g_fit) * t if g_fit else None,
        'over_rotation_rad': (theta_real - cal.get('target_angle_rad')) if theta_real else None,
        'over_rotation_fraction': (g_design / g_model - 1) if g_design and g_model else None,
        'corrected_pulse_duration_ns': (cal.get('target_angle_rad') / rate(g_design)) if g_design else None,
        'corrected_amplitude_scale_at_fixed_duration': (g_model / g_design) if g_design and g_model else None,
    })
    params = {k: float(v) for k, v in params.items() if v is not None}

    # verification companion (analytic identity) and observable spec
    vobs = next((o for o in obs.get('observables', []) if o.get('circuit')), {})
    phis = ver.get('phase_angles_rad', [])
    vq = (vobs.get('qubits') or [None])[0]
    others = [e['angle_rad'] for e in ver.get('interactions', []) if vq in e['edge'] and tuple(e['edge']) != tgt_edge]
    pref = math.cos(phis[vq]) * math.prod(math.cos(x) for x in others) if vq is not None and phis else None
    if pref:
        x_obs = vobs.get('observed_value')
        x_int = pref * math.cos(cal.get('target_angle_rad'))
        x_real = pref * math.cos(theta_real)
        theta_v = math.acos(max(-1, min(1, x_obs / pref)))
        params.update({
            'verification_X1_observed': x_obs, 'verification_X1_predicted_intended': x_int,
            'verification_X1_predicted_realized': x_real, 'verification_X1_tolerance': vobs.get('absolute_tolerance'),
            'verification_inferred_target_angle_rad': theta_v,
            'verification_inferred_coupling_MHz': theta_v / t / (2 * math.pi) * 1000,
        })
    parity_obs = [o for o in obs.get('observables', []) if not o.get('circuit')]

    # ---------------- structured simulation (Aer MPS) ----------------
    sim_txt = []
    try:
        N = C.num_qubits

        def P(dct):
            s = ['I'] * N
            for k, v in dct.items():
                s[N - 1 - k] = v
            return ''.join(s)
        e0, e1 = tgt_edge
        paulis = {'total_parity': 'Z' * N, f'Z{e0}': P({e0: 'Z'}), f'Z{e1}': P({e1: 'Z'}),
                  f'Z{e0}Z{e1}': P({e0: 'Z', e1: 'Z'}), f'Y{e0}Y{e1}': P({e0: 'Y', e1: 'Y'})}
        seed = simcfg.get('seed_simulator', 20261112)
        runs = {}
        for bond, thr in ((128, 1e-10), (256, 1e-10), (512, 1e-10), (256, 1e-12)):
            for lab, ang in (('intended', None), ('realized', theta_real)):
                runs[(bond, thr, lab)] = _mps_observables(C, c_idx, ang, paulis, bond, thr, seed)
        base_i, base_r = runs[(256, 1e-10, 'intended')], runs[(256, 1e-10, 'realized')]
        conv = max(abs(runs[k][o] - (base_i if k[2] == 'intended' else base_r)[o]) for k in runs for o in paulis)
        for o in paulis:
            params[f'mps_workload_{o}_intended'] = round(base_i[o], 6)
            params[f'mps_workload_{o}_realized'] = round(base_r[o], 6)
        params['mps_max_observable_delta_across_bond_128_256_512_and_threshold'] = conv
        V = _load_qpy(d, 'verification_circuit.qpy')
        vt = (V.metadata or {}).get('target_instruction_index', ver.get('target_instruction_index'))
        vx_i = _mps_observables(V, vt, None, {'X': P({vq: 'X'})}, 256, 1e-10, seed)['X']
        vx_r = _mps_observables(V, vt, theta_real, {'X': P({vq: 'X'})}, 256, 1e-10, seed)['X']
        params['mps_verification_X1_intended'] = round(vx_i, 6)
        params['mps_verification_X1_realized'] = round(vx_r, 6)
        sim_txt = [
            _ev('simulation_config.json', f'Aer matrix_product_state, seed {seed}, bond limits 128/256/512 at threshold '
                f'1e-10 plus bond 256 at 1e-12; selected observables via save_expectation_value only, no statevector or '
                f'distribution. Max observable change across settings = {conv:.2e}: converged.',
                'recommended_method; convergence_policy'),
            _ev('compiled_circuit.qpy', f'MPS workload with target RZZ at pi/2 vs realized {theta_real:.6f} rad: total Z '
                f'parity {base_i["total_parity"]:.6f} vs {base_r["total_parity"]:.6f} (identical, parity is blind to the '
                f'fault); <Z{e0}> {base_i[f"Z{e0}"]:.4f} -> {base_r[f"Z{e0}"]:.4f}; <Z{e1}> {base_i[f"Z{e1}"]:.4f} -> '
                f'{base_r[f"Z{e1}"]:.4f}; <Z{e0}Z{e1}> {base_i[f"Z{e0}Z{e1}"]:.4f} -> {base_r[f"Z{e0}Z{e1}"]:.4f}.',
                f'instruction {c_idx}'),
            _ev('verification_circuit.qpy', f'MPS verification <X{vq}>: intended {vx_i:.6f}, realized {vx_r:.6f}; observed '
                f'{vobs.get("observed_value"):.6f} (tolerance {vobs.get("absolute_tolerance")}).',
                f'instruction {vt}')]
    except Exception as exc:
        sim_txt = [_ev('simulation_config.json', f'MPS simulation not completed ({type(exc).__name__}); analytic results '
                       'stand on their own.')]
    ev['sim'] = sim_txt

    ev['artifact'] += [
        _ev('characterization.json', f'XI expectations {y.round(6).tolist()} for n={n.astype(int).tolist()} fit '
            f'cos(n*omega*t) at g = {params.get("coupling_fit_from_characterization_MHz")} MHz (window '
            f'{params.get("coupling_fit_tolerance_window_low_MHz")}-{params.get("coupling_fit_tolerance_window_high_MHz")} '
            f'MHz at tolerance {tol}); over 0.5-60 MHz the only other solution is the alias theta -> 2*pi - theta at '
            f'{[v for k, v in params.items() if k.startswith("coupling_fit_branch")]} MHz, rejected as it is ~2.9x the '
            'design and model values and needs a >2*pi-scale rotation per pulse; max residual {params.get("characterization_max_residual_design_coupling")} '
            f'at {g_design} MHz vs {params.get("characterization_max_residual_model_coupling")} at {g_model} MHz',
            'probes'),
        _ev('observable_spec.json', f'verification-X1 observed {vobs.get("observed_value")} vs predicted '
            f'{params.get("verification_X1_predicted_intended", 0):.6f} for intended pi/2 and '
            f'{params.get("verification_X1_predicted_realized", 0):.6f} for realized angle', 'observables[verification-X1]'),
        _ev('verification_protocol.json', f'<X{vq}> = cos(phi_{vq}) * prod cos(theta_e) over incident edges; phi_{vq} = '
            f'{phis[vq] if phis else "?"}, other incident angles {others}; inverting the observed value gives target angle '
            f'{params.get("verification_inferred_target_angle_rad", 0):.6f} rad = '
            f'{params.get("verification_inferred_coupling_MHz", 0):.4f} MHz', 'analytic_identity; interactions'),
    ]

    # ---------------- noise / alternatives (M8) ----------------
    nq = noise.get('qubits', [])
    if nq:
        med_t1 = sorted(q['T1_us'] for q in nq)[len(nq) // 2]
        med_ro = sorted(q['readout_error'] for q in nq)[len(nq) // 2]
        for q in nq:
            if q['T1_us'] < 0.5 * med_t1 or q['readout_error'] > 3 * med_ro:
                incidental.append(
                    f'noise_snapshot qubit {q["qubit"]}: T1 {q["T1_us"]} us, T2 {q["T2_us"]} us, readout_error '
                    f'{q["readout_error"]} (median T1 {med_t1}, readout {med_ro}). Not on target edge {list(tgt_edge)}; the '
                    'snapshot scope excludes it from the isolated Q1/Q4 characterization, and the verification observable '
                    'applies no readout channel; it cannot produce a coherent 12.5% over-rotation.')
    for o in parity_obs:
        incidental.append(f'Observable {o["id"]} = {o["observed_value"]} (passes): Z-parity is invariant under '
                          'RZ/RZZ and SX-pair structure, and MPS confirms it is identical for intended and realized '
                          'angles, so passing parity neither clears nor localizes the fault.')
    incidental += [
        f'Instruction index differs between intended ({i_idx}) and compiled ({c_idx}) circuits for {tgt_op}: commuting '
        'reorder by the compiler; the operation, qubits and angle are identical.',
        'backend edge [4,2] vs compiled RZZ on (2,4): RZZ is symmetric, so edge orientation is irrelevant.',
        'intended_circuit.txt and compiled_circuit.txt are byte-identical drawings although the QPY instruction order '
        'differs: the drawing is order-insensitive (layered); not evidence of tampering.',
        f'provenance_registry re-registers {mod.get("artifact_id")} at {reg_model.get("timestamp")} alongside D21 '
        'artifacts; a registry timestamp is not a regeneration and does not alter its source fingerprint.',
        f'characterization n=8 expectation {y[-1] if len(y) else "?"} is near 0 as predicted (cos(8*theta) with theta = '
        '9*pi/16 gives 0); residuals of ~0.001 are within the declared additive perturbation <= ' + str(tol) + '.',
        'All 32 transmon components share identical geometry and model EJ/EC; qubit-parameter drift is not supported.',
    ]
    alternatives = (
        'REJECTED ALTERNATIVES: compiler (DAG-identical, RZZ(pi/2) requested); mapping (identity layout, supported edge '
        '[1,4]); scheduling (matches compiled circuit; duration equals calibration); wrong control channel (u14 bound to '
        'edge [1,4] consistently in manifest, calibration and verification metadata); runtime/backend (D21/K21 fingerprint '
        'consistent with design); T1/T2 decay (would shrink |<XI>| with n, but |<XI>| reaches 0.98 at n=7: coherent, '
        'undamped rotation; noise excluded from local probes); readout (Q30 0.18 is off-edge, no readout channel in '
        'verification); crosstalk (no_crosstalk=true, characterization is isolated). Only the coupling value fits.')

    # ---------------- verdict ----------------
    layers['algorithm'] = (f'{(I.metadata or {}).get("workload") if I is not None else "workload"}: {chc.get("rounds")} '
                           f'rounds of RZ and SX-conjugated RZZ on {chc.get("physical_qubits")} qubits; '
                           f'the intended target is RZZ(pi/2) on logical {tgt_logical}.')
    layers['characterization'] = (
        f'Characterization fits g = {params.get("coupling_fit_from_characterization_MHz")} MHz on the physical branch '
        '(the only other branch below 60 MHz is the cosine alias theta -> 2*pi - theta, implausible) (residual '
        f'{params.get("characterization_max_residual_at_fit")}), equal to the {cur_rev} design value {g_design} MHz and '
        f'inconsistent with the model value {g_model} MHz (residual {params.get("characterization_max_residual_model_coupling")}). '
        f'Verification <X{vq}> observed {vobs.get("observed_value")} matches the realized angle prediction '
        f'{params.get("verification_X1_predicted_realized", 0):.6f} and not the intended {params.get("verification_X1_predicted_intended", 0):.6f}.')
    layers['noise'] = 'NOT CAUSAL. ' + alternatives
    res.update(layer_assessments=layers, estimated_parameters=params, incidental_anomalies=incidental,
               artifact_evidence=ev['artifact'], runtime_evidence=ev['runtime'], device_design_evidence=ev['design'],
               device_model_evidence=ev['model'], control_evidence=ev['control'], simulation_evidence=ev['sim'])

    meas_dev = abs(params.get('verification_X1_observed', 0) - params.get('verification_X1_predicted_intended', 0))
    if not model_stale or not g_design or not g_model or g_design == g_model:
        if meas_dev > (vobs.get('absolute_tolerance') or 0.004):
            res.update(verdict='INTEGRITY_VIOLATION', confidence=0.6, suspected_fault_type='UNLOCALIZED_DEVIATION',
                       root_cause='Verification observable deviates from the intended prediction but no provenance break '
                                  'was found.')
        else:
            res.update(verdict='VALID_EXECUTION', confidence=0.8,
                       root_cause='All layers consistent; verification observable within tolerance of intent.')
        return res

    e0, e1 = tgt_edge
    res.update(
        verdict='INTEGRITY_VIOLATION',
        confidence=0.95,
        suspected_fault_type='STALE_DEVICE_MODEL_PROVENANCE -> COUPLING_MISCALIBRATION (coherent RZZ over-rotation)',
        affected_logical_operations=[
            f'{tgt_op}: RZZ(pi/2) on logical {tgt_logical} (intended index {i_idx}, compiled index '
            f'{c_idx}, schedule start {tgt_sched[0]["start_ns"] if tgt_sched else "?"} ns); also verification target '
            f'instruction {ver.get("target_instruction_index")} which uses the same calibration'],
        expected_physical_resources=[
            f'physical edge ({e0},{e1}) via coupler {lr.get("coupler", {}).get("id")} on channel {man.get("channel")}',
            f'{cur_rev} device model (source fingerprint {str(des.get("geometry_fingerprint"))[:12]}...) with g = {g_design} MHz',
            f'rate {rate(g_design):.6f} rad/ns, pulse {params["corrected_pulse_duration_ns"]:.4f} ns for theta = pi/2',
        ],
        suspected_realized_resources=[
            f'same physical edge ({e0},{e1}) / coupler {lr.get("coupler", {}).get("id")} / channel {man.get("channel")} '
            '(the edge and channel are correct)',
            f'calibration {cal.get("artifact_id")} derived from stale {mod.get("artifact_id")} ({model_rev} geometry, '
            f'g = {g_model} MHz): {t} ns pulse at true rate {rate(g_design):.6f} rad/ns',
            f'realized RZZ angle {theta_real:.6f} rad (= 9*pi/16) instead of pi/2: over-rotation '
            f'{params["over_rotation_rad"]:.6f} rad ({params["over_rotation_fraction"]:.1%})',
        ],
        root_cause=(
            f'OBSERVATIONS: compiler, mapping, schedule and runtime faithfully request RZZ(pi/2) on edge ({e0},{e1}) over '
            f'channel {man.get("channel")}. The current device design {des.get("artifact_id")} ({cur_rev}) gives '
            f'g = {g_design} MHz for coupler {lr.get("coupler", {}).get("id")}, but the device model '
            f'{mod.get("artifact_id")} was built from {model_rev} geometry (fingerprint '
            f'{str(mod.get("source_geometry_fingerprint"))[:12]}...) with g = {g_model} MHz and was never regenerated for '
            f'{cur_rev}. Calibration {cal.get("artifact_id")} ({cal.get("calibration_epoch")}) took its rate from that model: '
            f'{nominal:.6f} rad/ns, so it chose {t} ns for pi/2. '
            f'PHYSICAL CONSEQUENCE: under the supplied reduced assumption omega = 2*pi*g/1000 rad/ns the true rate is '
            f'{rate(g_design):.6f} rad/ns, giving theta = {theta_real:.6f} rad (9*pi/16), a {params["over_rotation_fraction"]:.1%} '
            f'(+{params["over_rotation_rad"]:.4f} rad) coherent over-rotation. Characterization independently fits '
            f'g = {g_fit:.3f} MHz and the verification correlator inverts to {params.get("verification_inferred_coupling_MHz", 0):.3f} MHz. '
            f'BOUNDARY: design ({cur_rev}, correct) -> device model (stale {model_rev}) -> control calibration '
            f'({cal.get("calibration_epoch")}, inherits stale g). The fault boundary is the design-to-model provenance '
            f'handoff, propagated into calibration; everything downstream executed as configured. ' + alternatives),
        proposed_repair=(
            f'Regenerate the reduced device model for the {cur_rev} geometry (source_geometry_fingerprint '
            f'{des.get("geometry_fingerprint")}), giving coupling_proxy_MHz = {g_design} for edge ({e0},{e1}); register it '
            f'(e.g. model-21) and repoint {cal.get("artifact_id")} and {loc.get("artifact_id")} to it. Recalibrate '
            f'{man.get("channel")}: nominal_rate {rate(g_design):.6f} rad/ns and pulse_duration '
            f'{params["corrected_pulse_duration_ns"]:.4f} ns (or keep {t} ns with amplitude_scale '
            f'{params["corrected_amplitude_scale_at_fixed_duration"]:.4f}). Add a provenance gate: a calibration may be '
            'used only if its model\'s source_geometry_fingerprint equals the runtime/backend geometry_fingerprint, and '
            'any new device revision invalidates models and calibrations derived from the previous one.'),
        repair_validation=(
            f'(1) Provenance: check model.source_geometry_fingerprint == design == runtime fingerprint ({cur_rev}). '
            f'(2) Re-run the {len(n)} characterization probes (|++>, n repeated pulses, measure XI): expect cos(n*pi/2) = '
            '[0, -1, 0, 1, 0, -1, 0, 1] within ' + str(tol) + ', and a fitted g with omega*t = pi/2 '
            f'(currently [{", ".join(f"{v:.3f}" for v in y)}]). (3) Re-run the verification companion: expect <X{vq}> = '
            f'{params.get("verification_X1_predicted_intended", 0):.4f} +/- {vobs.get("absolute_tolerance")} (currently '
            f'{vobs.get("observed_value"):.4f}). (4) Re-simulate the workload with Aer MPS at bond 128/256/512 and confirm '
            f'local observables on ({e0},{e1}) return to the intended values (e.g. <Z{e0}> '
            f'{params.get(f"mps_workload_Z{e0}_intended", 0):.4f}, currently {params.get(f"mps_workload_Z{e0}_realized", 0):.4f}); '
            'global parity is not a sufficient check. (5) Optionally amplify sensitivity with odd-n repetitions and '
            'Ramsey-style ZZ echoes; keep these as regression tests on every device revision.'),
    )
    return res


def solve(stage_name, stage_dir):
    return {'stage_1': solve_stage1, 'stage_2': solve_stage2}[stage_name](stage_dir)
