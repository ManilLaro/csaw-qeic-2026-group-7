"""B3 — QuTiP local control-dynamics validation for Stage 2 (local subsystem Q1/Q4, <= 4 qubits).

What this adds beyond the closed-form records (stated honestly in README.md):
  1. Characterization replay with the calibrated pulse integrated by qutip.mesolve, with and without
     Lindblad T1/T2 from noise_snapshot.json; minimax fit of g with its tolerance window and the full
     alias set (the probes cannot distinguish g from k*1000/t +/- g).
  2. The ACTUAL compiled local block around the target (instruction 694 in compiled_circuit.qpy): the
     single-qubit gates on Q1 and Q4 between their neighbouring two-qubit gates, plus the target RZZ, built
     as a qutip-qip QubitCircuit with the target replaced by the pulse propagator; process fidelity of the
     realized vs intended block, and of the realized vs intended verification companion.
  3. Verification companion <X1> on a local subsystem chosen from the record (qubits on edges incident to
     the measured qubit, plus one non-incident neighbour to demonstrate it has no effect).
  4. Repair validation with sensitivity: residual angle error of the corrected pulse over the fit window,
     pass criteria per probe.

Reproduce from the repository root (~3 s):
    python extensions/qutip_validation/validate_dynamics.py
"""
import json
import math
import platform
from pathlib import Path

import numpy as np
import qutip as qt
import qutip_qip
from qutip_qip.circuit import QubitCircuit

HERE = Path(__file__).resolve().parent
ART = HERE.parents[1] / 'stages' / 'stage_2' / 'artifacts'
I2, X, Y, Z = qt.qeye(2), qt.sigmax(), qt.sigmay(), qt.sigmaz()
PLUS = (qt.basis(2, 0) + qt.basis(2, 1)).unit()
SOLVER_OPTS = {'atol': 1e-12, 'rtol': 1e-10}


def load(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


def rate(g_mhz):
    """Supplied reduced assumption: omega = 2*pi*g_MHz/1000 rad/ns."""
    return 2 * math.pi * g_mhz / 1000.0


def pulse_propagator(g_mhz, t_ns):
    """Propagator of the calibrated rectangular pulse, H = omega ZZ/2, integrated with qutip.sesolve."""
    H = 0.5 * rate(g_mhz) * qt.tensor(Z, Z)
    U = qt.sesolve(H, qt.qeye([2, 2]), [0.0, t_ns], options=SOLVER_OPTS).states[-1]
    return qt.Qobj(U.full(), dims=[[2, 2], [2, 2]])


def probe_XI(g_mhz, t_ns, reps, t1_us=None, t2_us=None):
    """|++>, reps back-to-back pulses, <XI>; optional Lindblad T1 (sigma_-) and pure dephasing on both qubits."""
    H = 0.5 * rate(g_mhz) * qt.tensor(Z, Z)
    c_ops = []
    if t1_us:
        g1 = 1.0 / (t1_us * 1e3)
        gphi = max(0.0, 1.0 / (t2_us * 1e3) - g1 / 2)
        for k in range(2):
            ops = [I2, I2]; ops[k] = qt.destroy(2); c_ops.append(math.sqrt(g1) * qt.tensor(*ops))
            ops = [I2, I2]; ops[k] = Z; c_ops.append(math.sqrt(gphi / 2) * qt.tensor(*ops))
    out = qt.mesolve(H, qt.ket2dm(qt.tensor(PLUS, PLUS)), np.linspace(0, reps * t_ns, 200), c_ops=c_ops,
                     e_ops=[qt.tensor(X, I2)], options=SOLVER_OPTS)
    return float(out.expect[0][-1])


def process_fidelity(U, V):
    d = U.shape[0]
    return float(abs((U.dag() * V).tr()) ** 2 / d ** 2)


def compiled_local_block(compiled, idx, q_pair):
    """Single-qubit gates on each wire of q_pair contiguous with instruction idx (stopping at any two-qubit
    gate that involves another qubit), plus the target itself. Returns ordered [(name, local_targets, params)]."""
    ins = [(i.operation.name, [compiled.find_bit(q).index for q in i.qubits], [float(p) for p in i.operation.params])
           for i in compiled.data]
    block_idx = {idx}
    for q in q_pair:
        for step in (-1, 1):
            k = idx + step
            while 0 <= k < len(ins):
                name, qs, _ = ins[k]
                if q in qs:
                    if len(qs) == 1:
                        block_idx.add(k)
                    elif set(qs) == set(q_pair):
                        block_idx.add(k)
                    else:
                        break
                k += step
    loc = {q: i for i, q in enumerate(q_pair)}
    return [(ins[k][0], [loc[q] for q in ins[k][1]], ins[k][2], k) for k in sorted(block_idx)]


def block_unitary(block, target_idx, pulse_U):
    qc = QubitCircuit(2)
    qc.user_gates = {'RZZ': lambda arg_value: (-1j * arg_value / 2 * qt.tensor(Z, Z)).expm(),
                     'PULSE': lambda: pulse_U}
    for name, tg, params, k in block:
        if k == target_idx and pulse_U is not None:
            qc.add_gate('PULSE', targets=tg)
        elif name == 'rz':
            qc.add_gate('RZ', targets=tg, arg_value=params[0])
        elif name == 'sx':
            qc.add_gate('SQRTNOT', targets=tg)
        elif name == 'rzz':
            qc.add_gate('RZZ', targets=tg, arg_value=params[0])
        else:
            raise ValueError(name)
    return qc.compute_unitary(), [g.name for g in qc.gates]


def main():
    from qiskit import qpy
    char, cal, mod, des = load('characterization.json'), load('control_calibration.json'), load('device_model.json'), \
        load('device_design.json')
    ver, obs, noise, ch = load('verification_protocol.json'), load('observable_spec.json'), load('noise_snapshot.json'), \
        load('challenge.json')
    t = cal['pulse_duration_ns']; target = cal['target_angle_rad']
    g_model, g_design = mod['coupling_proxy_MHz'], des['local_region']['coupling_proxy_MHz']
    tol = char['absolute_tolerance']
    n = np.array([p['repetitions'] for p in char['probes']]); y = np.array([p['expectation'] for p in char['probes']])
    edge = tuple(char['physical_edge'])
    nq = {q['qubit']: q for q in noise['qubits']}
    t1 = min(nq[q]['T1_us'] for q in edge); t2 = min(nq[q]['T2_us'] for q in edge)
    R = {'versions': {'python': platform.python_version(), 'qutip': qt.__version__, 'qutip_qip': qutip_qip.__version__,
                      'numpy': np.__version__},
         'inputs': {'pulse_duration_ns': t, 'target_angle_rad': target, 'g_model20_MHz': g_model,
                    'g_design21_MHz': g_design, 'T1_us': t1, 'T2_us': t2, 'tolerance': tol},
         'scope_note': 'H = omega ZZ/2 commutes with itself, so mesolve reproduces cos(n*omega*t), the same closed form '
                       'characterization.json declares it was generated from. Agreement therefore validates the '
                       'numbers and assumptions under the supplied reduced model; it is not an independent physical '
                       'derivation of g.'}

    # ---- 1. characterization replay, fit window and aliases ----
    sims = {}
    for label, g in (('model20_g16', g_model), ('design21_g18', g_design)):
        v = np.array([probe_XI(g, t, k) for k in n])
        sims[label] = {'XI': v.round(6).tolist(), 'max_abs_residual': float(np.max(np.abs(v - y))),
                       'within_tolerance': bool(np.max(np.abs(v - y)) <= tol)}
    coh = np.array(sims['design21_g18']['XI'])
    vn = np.array([probe_XI(g_design, t, k, t1, t2) for k in n])
    sims['design21_g18_with_T1T2'] = {'XI': vn.round(6).tolist(), 'max_abs_residual': float(np.max(np.abs(vn - y))),
                                      'max_shift_from_coherent': float(np.max(np.abs(vn - coh)))}
    period = 1000.0 / t
    grid = np.arange(0.0, 3 * period, 0.0005)
    cost = np.max(np.abs(np.cos(np.outer(grid, n) * 2 * np.pi / 1000 * t) - y), axis=1)
    ok = cost <= tol
    branches, start = [], None
    for i, f in enumerate(ok):
        if f and start is None:
            start = i
        if (not f or i == len(ok) - 1) and start is not None:
            branches.append([float(grid[start]), float(grid[i if f else i - 1])]); start = None
    phys = min(branches, key=lambda b: abs(sum(b) / 2 - g_model))
    fine = np.arange(phys[0], phys[1] + 1e-9, 0.0005)
    costf = [max(abs(probe_XI(g, t, k) - yy) for k, yy in zip(n, y)) for g in fine]
    g_fit = float(fine[int(np.argmin(costf))])
    R['characterization'] = {
        'supplied_XI': y.tolist(), 'repetitions': n.tolist(), 'simulations': sims,
        'qutip_minimax_fit_g_MHz': g_fit, 'qutip_minimax_fit_max_residual': float(min(costf)),
        'fit_window_within_tolerance_MHz': phys,
        'alias_branches_within_tolerance_MHz_0_to_3_periods': branches,
        'alias_period_MHz': period,
        'branch_choice': 'the branch nearest the supplied model/design proxies (16/18 MHz) is taken; the others '
                         '(theta -> 2*pi - theta and +k*1000/t) fit equally well and are excluded only as physically '
                         'implausible (2.5x or more the proxies).',
        'realized_angle_per_pulse_rad': rate(g_fit) * t,
        'realized_angle_window_rad': [rate(phys[0]) * t, rate(phys[1]) * t],
    }

    # ---- 2. actual compiled local block from compiled_circuit.qpy ----
    with (ART / 'compiled_circuit.qpy').open('rb') as f:
        C = qpy.load(f)[0]
    cidx = ch['circuit']['target_instruction_index']
    block = compiled_local_block(C, cidx, list(edge))
    U_int, names = block_unitary(block, cidx, None)
    U_real, _ = block_unitary(block, cidx, pulse_propagator(g_design, t))
    U_m20, _ = block_unitary(block, cidx, pulse_propagator(g_model, t))
    R['compiled_block'] = {
        'instructions': [{'index': k, 'gate': nme, 'local_targets': tg, 'params': pr} for nme, tg, pr, k in block],
        'qutip_qip_gates': names,
        'process_fidelity_realized_vs_intended': process_fidelity(U_int, U_real),
        'process_fidelity_model20_pulse_vs_intended': process_fidelity(U_int, U_m20),
        'analytic_cos2_half_overrotation': math.cos((rate(g_design) * t - target) / 2) ** 2,
        'note': 'Single-qubit gates on Q1/Q4 adjacent to the target, as compiled; target replaced by the integrated '
                'pulse propagator. With the model-20 rate the block equals the intended one; with the D21 rate it '
                'does not.',
    }
    # SX-conjugation identity (both orientations hold, since (+/-Y)x(+/-Y) = YY; reported as such)
    th = rate(g_design) * t
    sx = qt.Qobj(0.5 * np.array([[1 + 1j, 1 - 1j], [1 - 1j, 1 + 1j]]))
    S2 = qt.tensor(sx, sx)
    rzz = (-1j * th / 2 * qt.tensor(Z, Z)).expm(); yy = (-1j * th / 2 * qt.tensor(Y, Y)).expm()
    R['sx_conjugation'] = {
        'SXdag_RZZ_SX_eq_YY_rotation': process_fidelity(S2.dag() * rzz * S2, yy) > 1 - 1e-12,
        'SX_RZZ_SXdag_eq_YY_rotation': process_fidelity(S2 * rzz * S2.dag(), yy) > 1 - 1e-12,
        'commutes_with_ZZ_parity': bool(((S2.dag() * rzz * S2) * qt.tensor(Z, Z) - qt.tensor(Z, Z) * (S2.dag() * rzz * S2)).norm() < 1e-10),
        'note': 'Holds for either orientation, so it explains parity blindness but does not validate gate order.'}

    # ---- 3. verification companion on a record-derived local subsystem ----
    vq = next(o for o in obs['observables'] if o.get('circuit'))
    q0 = vq['qubits'][0]
    phis = ver['phase_angles_rad']
    incident = sorted({x for e in ver['interactions'] if q0 in e['edge'] for x in e['edge']})
    extra = next((x for e in ver['interactions'] for x in e['edge']
                  if x not in incident and set(e['edge']) & set(incident)), None)

    def verification_expect(g, sub):
        idx = {p: i for i, p in enumerate(sub)}
        op_on = lambda p, o: qt.tensor(*[o if i == idx[p] else I2 for i in range(len(sub))])
        psi = qt.tensor(*[PLUS] * len(sub))
        for p in sub:
            psi = (-1j * phis[p] / 2 * op_on(p, Z)).expm() * psi
        for e in ver['interactions']:
            a, b = e['edge']
            if a in idx and b in idx:
                if tuple(e['edge']) == edge:
                    H = 0.5 * rate(g) * op_on(a, Z) * op_on(b, Z)
                    psi = qt.sesolve(H, psi, [0, t], options=SOLVER_OPTS).states[-1]
                else:
                    psi = (-1j * e['angle_rad'] / 2 * op_on(a, Z) * op_on(b, Z)).expm() * psi
        return float(qt.expect(op_on(q0, X), psi))
    sub_min, sub_ext = incident, sorted(incident + ([extra] if extra is not None else []))
    R['verification'] = {
        'subsystem_incident': sub_min, 'subsystem_with_non_incident_neighbour': sub_ext,
        'observed': vq['observed_value'], 'tolerance': vq['absolute_tolerance'],
        'X_g16_incident': verification_expect(g_model, sub_min),
        'X_g18_incident': verification_expect(g_design, sub_min),
        'X_g18_with_neighbour': verification_expect(g_design, sub_ext),
    }
    v = R['verification']
    v['neighbour_effect'] = abs(v['X_g18_with_neighbour'] - v['X_g18_incident'])
    v['g18_within_tolerance'] = abs(v['X_g18_incident'] - v['observed']) <= v['tolerance']
    v['g16_deviation_over_tolerance'] = abs(v['X_g16_incident'] - v['observed']) / v['tolerance']

    # ---- 4. repair validation with sensitivity ----
    t_fix = target / rate(g_design)
    worst = max(abs(rate(g) * t_fix - target) for g in phys)
    xi_fix = [probe_XI(g_design, t_fix, k) for k in n]
    worst_probe = max(abs(math.cos(k * (target + s * worst)) - math.cos(k * target)) for k in n for s in (-1, 1))
    R['repair'] = {
        'corrected_pulse_ns': t_fix,
        'XI_after_repair_at_g_fit': [round(x, 6) for x in xi_fix],
        'pass_criteria': {f'n={int(k)}': [round(math.cos(k * target), 6), tol] for k in n},
        'fault_signature_vs_repair': {f'n={int(k)}': {'fault': round(float(c), 6), 'repaired': round(math.cos(k * target), 6)}
                                      for k, c in zip(n, coh)},
        'residual_angle_error_over_fit_window_rad': worst,
        'worst_probe_deviation_over_fit_window': worst_probe,
        'passes_within_tolerance_over_fit_window': worst_probe <= tol,
        'amplitude_repair_note': f'keeping {t} ns and scaling the drive by {g_model / g_design:.4f} gives omega*t = pi/2 '
                                 'by the same algebra (it equals the g16 case), so it is not simulated separately.',
        'verification_X_after_repair': verification_expect(g_design * (t_fix / t), sub_min),
        'compiled_block_fidelity_after_repair': process_fidelity(U_int, block_unitary(block, cidx, pulse_propagator(g_design, t_fix))[0]),
    }

    (HERE / 'results.json').write_text(json.dumps(R, indent=2) + '\n', encoding='utf-8')
    c = R['characterization']
    print(f"probes: g16 resid {c['simulations']['model20_g16']['max_abs_residual']:.4f}, g18 resid "
          f"{c['simulations']['design21_g18']['max_abs_residual']:.4f}, T1/T2 shift "
          f"{c['simulations']['design21_g18_with_T1T2']['max_shift_from_coherent']:.2e}; fit g {g_fit:.4f} MHz "
          f"window {phys}; aliases {[round(sum(b) / 2, 2) for b in branches]}")
    b = R['compiled_block']
    print(f"compiled block {[i['index'] for i in b['instructions']]} gates {b['qutip_qip_gates']}: F(real,int) "
          f"{b['process_fidelity_realized_vs_intended']:.6f} (analytic {b['analytic_cos2_half_overrotation']:.6f}), "
          f"F(m20,int) {b['process_fidelity_model20_pulse_vs_intended']:.6f}")
    print(f"verification {v['subsystem_incident']}+{extra}: obs {v['observed']:.6f} g18 {v['X_g18_incident']:.6f} "
          f"g16 {v['X_g16_incident']:.6f} ({v['g16_deviation_over_tolerance']:.0f}x tol), neighbour effect "
          f"{v['neighbour_effect']:.1e}")
    r = R['repair']
    print(f"repair {t_fix:.4f} ns: XI {r['XI_after_repair_at_g_fit']}; worst probe dev over window "
          f"{r['worst_probe_deviation_over_fit_window']:.4f} (pass {r['passes_within_tolerance_over_fit_window']}); "
          f"block F {r['compiled_block_fidelity_after_repair']:.6f}; <X> {r['verification_X_after_repair']:.1e}")


if __name__ == '__main__':
    main()
