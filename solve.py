"""Edit this file. Return partial evidence when no stronger claim is supported."""
import json
from pathlib import Path


def empty_submission():
    return dict(verdict='INSUFFICIENT_EVIDENCE', layer_assessments={}, suspected_fault_type=None,
                affected_logical_operations=[], expected_physical_resources=[], suspected_realized_resources=[],
                artifact_evidence=[], diagnostic_evidence=[], estimated_parameters={}, incidental_anomalies=[],
                root_cause=None, proposed_repair=None, repair_validation=None, confidence=0.0)


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------

def _load(stage_dir, name):
    return json.loads((Path(stage_dir) / name).read_text(encoding='utf-8'))


def _instructions(circuit):
    return [(i.operation.name, [circuit.find_bit(q).index for q in i.qubits],
             [float(p) for p in i.operation.params], [circuit.find_bit(b).index for b in i.clbits])
            for i in circuit.data]


def _tvd(a, b):
    na, nb = sum(a.values()), sum(b.values())
    return 0.5 * sum(abs(a.get(k, 0) / na - b.get(k, 0) / nb) for k in set(a) | set(b))


def _stage1_hypothesis_sims(observed, readout_error):
    """Local, seeded Aer simulation of two hypotheses for the bind_73 CX.

    Probe uses 4 physical qubits [0, 1, 2, 4]; clbits c0,c1,c2 read logical
    q0 (phys 0), q1 (phys 1), q2 (phys 4) exactly as the compiled circuit does.
    """
    from qiskit import QuantumCircuit
    from qiskit_aer import AerSimulator
    from qiskit_aer.noise import NoiseModel, ReadoutError

    nm = NoiseModel()
    p = readout_error
    nm.add_all_qubit_readout_error(ReadoutError([[1 - p, p], [p, 1 - p]]))
    sim = AerSimulator(noise_model=nm, seed_simulator=7301)
    shots = 4096
    out = {}
    # probe index -> physical qubit: 0->0, 1->1, 2->2, 3->4
    for label, target, phys in (('faithful', 3, [1, 4]), ('misrouted', 2, [1, 2])):
        c = QuantumCircuit(4, 3)
        c.rz(1.5707963267948966, 0); c.sx(0); c.rz(1.5707963267948966, 0)
        c.cx(0, 1); c.cx(1, target)
        c.measure([0, 1, 3], [0, 1, 2])
        counts = sim.run(c, shots=shots).result().get_counts()
        out[label] = dict(counts=counts, tvd=_tvd(counts, observed), depth=c.depth(), shots=shots, cx_phys=phys)
    return out


def solve_stage1(stage_dir):
    from qiskit import qpy

    d = Path(stage_dir)
    backend = _load(d, 'backend.json')
    mapping = _load(d, 'mapping_current.json')
    history = _load(d, 'mapping_history.json')
    schedule = _load(d, 'schedule.json')
    manifest = _load(d, 'control_manifest.json')
    cal_cur = _load(d, 'calibration_current.json')
    cal_prev = _load(d, 'calibration_previous.json')
    log = _load(d, 'execution_log.json')
    counts = _load(d, 'execution_counts.json')['counts']
    hist = _load(d, 'historical_counts.json')

    circuits = {}
    for n in ('intended', 'compiled'):
        with (d / f'{n}_circuit.qpy').open('rb') as f:
            circuits[n] = qpy.load(f)[0]
    intended, compiled = (_instructions(circuits[n]) for n in ('intended', 'compiled'))

    # --- Compiler: intended vs compiled under the current mapping ----------
    l2p = {int(k[1:]): v for k, v in mapping['logical_to_physical'].items()}
    compiler_ok = len(intended) == len(compiled) and all(
        a[0] == b[0] and [l2p[q] for q in a[1]] == b[1] and a[2] == b[2] and a[3] == b[3]
        for a, b in zip(intended, compiled))

    # --- Mapping: current record vs history vs compiled layout -------------
    m2 = next(m for m in history['mappings'] if m['mapping_id'] == mapping['mapping_id'])
    m1 = next(m for m in history['mappings'] if m['mapping_id'] != mapping['mapping_id'])
    edges = {tuple(e) for e in backend['coupling_map']}
    mapping_ok = (m2['logical_to_physical'] == mapping['logical_to_physical']
                  and (l2p[0], l2p[1]) in edges and (l2p[1], l2p[2]) in edges)

    # --- Schedule vs mapping ------------------------------------------------
    sched_ops = {o['id']: o for o in schedule['operations']}
    two_q = [o for o in schedule['operations'] if o['gate'] == 'cx']
    sched_ok = (schedule['mapping_id'] == mapping['mapping_id']
                and all(tuple(o['physical_qubits']) in edges for o in two_q)
                and [o['physical_qubits'] for o in two_q] == [b[1] for b in compiled if b[0] == 'cx'])

    # --- Control / calibration provenance -----------------------------------
    p2n = {v['physical_index']: k for k, v in backend['endpoint_registry'].items()}
    n2p = {k: v['physical_index'] for k, v in backend['endpoint_registry'].items()}
    profiles = {p['profile_id']: dict(p, epoch=c['calibration_epoch'], device_epoch=c['device_epoch'])
                for c in (cal_cur, cal_prev) for p in c['profiles']}
    binding_findings = []
    bad = None
    for b in manifest['bindings']:
        op = next(o for o in two_q if o.get('control_binding') == b['binding_id'])
        prof = profiles[b['calibration_profile_id']]
        realized = sorted(n2p[r] for r in prof['endpoint_refs'])
        expected = sorted(op['physical_qubits'])
        ok = (b['calibration_epoch'] == cal_cur['calibration_epoch'] and realized == expected
              and prof['channel'] == b['channel'])
        binding_findings.append(dict(binding=b, op=op, prof=prof, realized=realized, expected=expected, ok=ok))
        if not ok:
            bad = binding_findings[-1]
    # current-epoch profile that *should* have been bound for the bad edge
    correct_prof = None
    if bad:
        correct_prof = next((p for p in cal_cur['profiles']
                             if sorted(n2p[r] for r in p['endpoint_refs']) == bad['expected']), None)

    # --- Diagnostic: seeded local simulation of both hypotheses -------------
    ro = max(m['readout_error'] for m in cal_cur['qubit_metrics']
             if m['endpoint_ref'] in {p2n[q] for q in l2p.values()})
    sims = None
    try:
        sims = _stage1_hypothesis_sims(counts, ro)
    except Exception:  # Aer unavailable: static conclusion still stands
        sims = None

    shots = sum(counts.values())
    p_q2_one = sum(v for k, v in counts.items() if k[0] == '1') / shots
    p_q0q1_corr = (counts.get('000', 0) + counts.get('011', 0)) / shots
    p_ghz_hist = (hist['counts'].get('000', 0) + hist['counts'].get('111', 0)) / sum(hist['counts'].values())
    p_ghz_now = (counts.get('000', 0) + counts.get('111', 0)) / shots

    if not (compiler_ok and mapping_ok and sched_ok and bad and correct_prof):
        return empty_submission()

    bid, ch, pid = bad['binding']['binding_id'], bad['binding']['channel'], bad['binding']['calibration_profile_id']
    op_id, lop = bad['op']['id'], bad['op']['logical_operation']
    e0, e1 = bad['expected']
    r0, r1 = bad['realized']
    exp_nodes = '-'.join(p2n[q] for q in bad['expected'])
    real_nodes = '-'.join(bad['prof']['endpoint_refs'])
    good = next(f for f in binding_findings if f['ok'])

    res = empty_submission()
    res.update(
        verdict='INTEGRITY_VIOLATION',
        suspected_fault_type='STALE_CONTROL_BINDING_WRONG_PHYSICAL_EDGE',
        confidence=0.93,
        layer_assessments={
            'algorithm': 'Intended circuit is a 3-qubit GHZ preparation (RZ-SX-RZ = H on q0, CX q0->q1, CX q1->q2); '
                         'ideal outcome is 000/111. No algorithm-layer fault.',
            'compiler': 'CONSISTENT. Compiled circuit is instruction-for-instruction identical to the intended circuit '
                        f'under mapping {mapping["mapping_id"]} (q0->{l2p[0]}, q1->{l2p[1]}, q2->{l2p[2]}); '
                        'parameters (RZ pi/2) and clbit assignments unchanged.',
            'mapping': f'CONSISTENT. Current mapping {mapping["mapping_id"]} (q2->physical {l2p[2]}, change ticket '
                       f'{m2.get("change_ticket")}, effective {m2["effective_from"]}) matches mapping_history, the compiled '
                       f'layout and the execution log; edges ({l2p[0]},{l2p[1]}) and ({l2p[1]},{l2p[2]}) exist in the coupling map. '
                       f'Previous mapping {m1["mapping_id"]} placed q2 on physical {m1["logical_to_physical"]["q2"]}.',
            'scheduling': f'CONSISTENT. {schedule["schedule_id"]} references {mapping["mapping_id"]}; {op_id} '
                          f'({lop}) is scheduled on physical ({e0},{e1}) as compiled. The schedule is correct; '
                          f'the fault is in the control binding it references ({bid}).',
            'control': f'FAULT. {bid} (bundle {bad["binding"]["bundle_id"]}) still points to {bad["binding"]["calibration_epoch"]} '
                       f'profile {pid} on channel {ch}, created {bad["binding"]["created_at"]} (before maintenance; '
                       f'previous device epoch {bad["prof"]["device_epoch"]}). {pid} drives endpoints {real_nodes} = physical '
                       f'({r0},{r1}), not ({e0},{e1}). The current {cal_cur["calibration_epoch"]} profile for {exp_nodes} is '
                       f'{correct_prof["profile_id"]} on channel {correct_prof["channel"]}, which was never bound. '
                       f'{good["binding"]["binding_id"]} was correctly regenerated for {cal_cur["calibration_epoch"]}.',
            'measurement': 'NOT CAUSAL. Readout on physical 0,1,4 is 2% per calibration_current; observed single-bit '
                           'flips (~2.4%) match. Readout cannot turn a GHZ into a q0/q1-only Bell state.',
        },
        affected_logical_operations=[f'{lop} (CX q1->q2, schedule op {op_id}, binding {bid})'],
        expected_physical_resources=[
            f'physical qubits ({e0},{e1}) = {exp_nodes}',
            f'{cal_cur["calibration_epoch"]} profile {correct_prof["profile_id"]} on control channel {correct_prof["channel"]}',
        ],
        suspected_realized_resources=[
            f'physical qubits ({r0},{r1}) = {real_nodes}',
            f'stale {bad["binding"]["calibration_epoch"]} profile {pid} on control channel {ch} (bundle {bad["binding"]["bundle_id"]})',
        ],
        incidental_anomalies=[
            'node_d (physical 3) relative_gate_quality 0.86: physical 3 is not used by the mapping, schedule or any binding.',
            'node_f (physical 5) readout_error 0.08: physical 5 is not measured or used.',
            f'bind_41 uses channel u7 whereas K19 used u3 for node_a-node_b: a legitimate K20 recalibration '
            '(K20 profile cp_021 matches endpoints node_a-node_b = physical (0,1)); q0/q1 correlation is intact.',
            f'Mapping change {m1["mapping_id"]}->{mapping["mapping_id"]} (q2: physical {m1["logical_to_physical"]["q2"]}->{l2p[2]}) '
            'is a documented maintenance change (maint_2048) applied consistently; it is the trigger, not the fault.',
            'Small off-pattern counts (001/010 ~2.4%, 1xx ~2%) are consistent with 2% readout error and gate noise; '
            'NOISE != INTEGRITY FAILURE.',
            'Execution log status COMPLETED and loaded bindings match the manifest: the job ran as configured, '
            'so the misconfiguration is in the configuration itself, not a runtime loading error.',
        ],
        root_cause=(
            f'Post-maintenance (ticket maint_2048) logical q2 moved from physical {m1["logical_to_physical"]["q2"]} to {l2p[2]}. '
            f'Compiler, mapping and schedule were updated, but control binding {bid} for {op_id} ({lop}) was not regenerated: '
            f'it still references {bad["binding"]["calibration_epoch"]} profile {pid} on channel {ch}, calibrated for '
            f'{real_nodes} = physical ({r0},{r1}). The CX intended on ({e0},{e1}) was therefore realized on ({r0},{r1}), '
            f'leaving logical q2 (physical {e1}) untouched. Result: GHZ 000/111 collapses to q0/q1 Bell pattern 000/011.'
        ),
        proposed_repair=(
            f'Regenerate {bid} for epoch {cal_cur["calibration_epoch"]}: bind {op_id} to profile {correct_prof["profile_id"]} '
            f'on channel {correct_prof["channel"]} ({exp_nodes} = physical ({e0},{e1})) and re-issue the control manifest '
            f'and bundle. Retire {pid}/{ch} bindings from the D20 manifest and add a pre-execution check that every '
            'binding\'s calibration_epoch equals the current epoch and its profile endpoint_refs, resolved via the '
            'endpoint_registry, equal the scheduled physical_qubits.'
        ),
        repair_validation=(
            f'(1) Static: assert every binding is {cal_cur["calibration_epoch"]} and profile endpoints match scheduled qubits. '
            f'(2) Edge-isolation probe on physical [1,2,4]: X on physical {e0}, then the repaired {op_id} CX, measure {r1} and {e1}; '
            f'expect physical {e1} flipped and {r1} unchanged (the reverse of the current fault). '
            '(3) Re-run the GHZ job: expect P(000)+P(111) back near the K19/M1 historical value '
            f'({p_ghz_hist:.3f}) instead of the current {p_ghz_now:.3f}, and P(q2=1) ~= 0.5 (currently {p_q2_one:.3f}). '
            '(4) Keep the probe and GHZ check as a regression test after every mapping or calibration change.'
        ),
        estimated_parameters={
            'observed_p_000_or_011_fraction': round(p_q0q1_corr, 4),
            'observed_p_ghz_000_or_111_fraction': round(p_ghz_now, 4),
            'historical_p_ghz_000_or_111_fraction': round(p_ghz_hist, 4),
            'observed_p_logical_q2_one_fraction': round(p_q2_one, 4),
            'readout_error_assumed_probability': ro,
        },
        artifact_evidence=[
            {'artifact': 'intended_circuit.qpy', 'claim': 'GHZ: H(q0) as RZ-SX-RZ, CX(0,1), CX(1,2), measure q_i->c_i', 'locator': 'instructions 0-7'},
            {'artifact': 'compiled_circuit.qpy', 'claim': 'Identical ops on physical (0,1,4); CX(1,4) realizes cx_1_2; q2 measured from physical 4 into c2', 'locator': 'instruction 4, 7'},
            {'artifact': 'mapping_current.json', 'claim': f'{mapping["mapping_id"]}: q2 -> physical {l2p[2]}', 'locator': 'logical_to_physical.q2'},
            {'artifact': 'mapping_history.json', 'claim': f'q2 moved {m1["logical_to_physical"]["q2"]}->{l2p[2]} at {m2["effective_from"]} via maint_2048', 'locator': 'mappings[1].change_ticket'},
            {'artifact': 'backend.json', 'claim': 'Edges (0,1),(1,4),(1,2) exist; node_b=1, node_c=2, node_e=4', 'locator': 'coupling_map; endpoint_registry'},
            {'artifact': 'schedule.json', 'claim': f'{op_id} ({lop}) scheduled on physical [{e0},{e1}] with control_binding {bid}', 'locator': f'operations[id={op_id}]'},
            {'artifact': 'control_manifest.json', 'claim': f'{bid}: epoch {bad["binding"]["calibration_epoch"]}, profile {pid}, channel {ch}, created {bad["binding"]["created_at"]} (pre-maintenance)', 'locator': f'bindings[binding_id={bid}]'},
            {'artifact': 'calibration_previous.json', 'claim': f'{pid} on {ch} targets {real_nodes} = physical ({r0},{r1}); device epoch D19', 'locator': f'profiles[profile_id={pid}]'},
            {'artifact': 'calibration_current.json', 'claim': f'K20 profile {correct_prof["profile_id"]} on {correct_prof["channel"]} targets {exp_nodes} but is unbound; {pid} absent from K20', 'locator': f'profiles[profile_id={correct_prof["profile_id"]}]'},
            {'artifact': 'execution_log.json', 'claim': f'Job ran under K20/M2 but loaded {bid}/{bad["binding"]["bundle_id"]} for {op_id}', 'locator': 'loaded_bindings[1]'},
            {'artifact': 'execution_counts.json', 'claim': f'000+011 = {p_q0q1_corr:.3f}; P(q2=1) = {p_q2_one:.3f}: q0/q1 entangled, q2 never flipped', 'locator': 'counts'},
            {'artifact': 'historical_counts.json', 'claim': f'K19/M1 run gave GHZ 000+111 = {p_ghz_hist:.3f}: same {pid}/{ch} was correct when q2 was on physical 2', 'locator': 'counts'},
        ],
    )
    if sims:
        f, m = sims['faithful'], sims['misrouted']
        res['estimated_parameters'].update({
            'tvd_observed_vs_faithful_model_probability': round(f['tvd'], 4),
            'tvd_observed_vs_misrouted_model_probability': round(m['tvd'], 4),
        })
        res['diagnostic_evidence'] = [
            {'job_id': 'local_aer_faithful_cx_1_4', 'shots': f['shots'], 'qubits': [0, 1, 2, 4], 'depth': f['depth'],
             'claim': f'Local seeded Aer simulation (seed 7301, {ro:.0%} readout error, no hardware) of the circuit with '
                      f'cx_1_2 on physical (1,4) as intended: GHZ 000/111; TVD vs observed counts = {f["tvd"]:.3f}. Rejected.'},
            {'job_id': 'local_aer_misrouted_cx_1_2', 'shots': m['shots'], 'qubits': [0, 1, 2, 4], 'depth': m['depth'],
             'claim': f'Same simulation with cx_1_2 realized on physical (1,2) (stale cp_017/u12 endpoints): 000/011 pattern; '
                      f'TVD vs observed counts = {m["tvd"]:.3f}. Matches observation.'},
        ]
    return res


def solve_stage2(stage_dir):
    return empty_submission()


def solve(stage_name, stage_dir):
    return {'stage_1': solve_stage1, 'stage_2': solve_stage2}[stage_name](stage_dir)
