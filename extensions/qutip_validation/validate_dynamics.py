"""B3 — QuTiP local control-dynamics validation for Stage 2.

Reproduces, from first-principles local dynamics, the supplied Stage-2 evidence:
  1. characterization.json  (|++>, n repeated ZZ pulses, measure XI)
  2. the verification companion correlator <X1> (observable_spec.json)
  3. the "SX basis changes realize YY pair blocks" statement (device_model.json)
  4. the proposed repair (corrected pulse) restores the intended pi/2 rotation
and quantifies decoherence using noise_snapshot.json T1/T2 for Q1/Q4.

Only the supplied 2-4 qubit local subsystem is simulated. Artifacts are read-only.
Reproduce from the repository root:
    python extensions/qutip_validation/validate_dynamics.py
"""
import json
import math
from pathlib import Path

import numpy as np
import qutip as qt
from qutip_qip.operations import Gate, expand_operator  # noqa: F401  (qutip-qip used for gate algebra)

HERE = Path(__file__).resolve().parent
ART = HERE.parents[1] / 'stages' / 'stage_2' / 'artifacts'


def load(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


I2, X, Y, Z = qt.qeye(2), qt.sigmax(), qt.sigmay(), qt.sigmaz()
PLUS = (qt.basis(2, 0) + qt.basis(2, 1)).unit()


def rate(g_mhz):
    """Supplied reduced assumption: omega = 2*pi*g_MHz/1000 rad/ns."""
    return 2 * math.pi * g_mhz / 1000.0


def zz_pulse_dynamics(g_mhz, t_ns, reps, t1_us=None, t2_us=None, steps=400):
    """Evolve |++> under H = omega ZZ/2 for reps back-to-back rectangular pulses
    of duration t_ns (time-dependent solver, not a closed formula); return <XI>.
    Optional Lindblad T1/T2 on both qubits (times in us, converted to ns)."""
    H = 0.5 * rate(g_mhz) * qt.tensor(Z, Z)
    rho0 = qt.ket2dm(qt.tensor(PLUS, PLUS))
    c_ops = []
    if t1_us:
        g1 = 1.0 / (t1_us * 1e3)
        gphi = max(0.0, 1.0 / (t2_us * 1e3) - g1 / 2) if t2_us else 0.0
        for k in range(2):
            ops = [I2, I2]
            ops[k] = qt.destroy(2)
            c_ops.append(math.sqrt(g1) * qt.tensor(*ops))
            if gphi:
                ops[k] = Z
                c_ops.append(math.sqrt(gphi / 2) * qt.tensor(*ops))
    tlist = np.linspace(0.0, reps * t_ns, steps)
    out = qt.mesolve(H, rho0, tlist, c_ops=c_ops, e_ops=[qt.tensor(X, I2)],
                     options={'atol': 1e-12, 'rtol': 1e-10})
    return float(out.expect[0][-1])


def sx():
    return qt.Qobj(0.5 * np.array([[1 + 1j, 1 - 1j], [1 - 1j, 1 + 1j]]))


def rz(phi):
    return qt.Qobj(np.diag([np.exp(-1j * phi / 2), np.exp(1j * phi / 2)]))


def rzz(theta):
    return (-1j * theta / 2 * qt.tensor(Z, Z)).expm()


def same_up_to_phase(a, b):
    ov = (a.dag() * b).tr() / a.shape[0]
    return abs(abs(ov) - 1) < 1e-10


def main():
    char, cal, mod, des = load('characterization.json'), load('control_calibration.json'), load('device_model.json'), \
        load('device_design.json')
    ver, obs, noise = load('verification_protocol.json'), load('observable_spec.json'), load('noise_snapshot.json')
    t = cal['pulse_duration_ns']
    target = cal['target_angle_rad']
    g_model, g_design = mod['coupling_proxy_MHz'], des['local_region']['coupling_proxy_MHz']
    tol = char['absolute_tolerance']
    n = [p['repetitions'] for p in char['probes']]
    y = np.array([p['expectation'] for p in char['probes']])
    nq = {q['qubit']: q for q in noise['qubits']}
    e0, e1 = char['physical_edge']
    t1 = min(nq[e0]['T1_us'], nq[e1]['T1_us'])
    t2 = min(nq[e0]['T2_us'], nq[e1]['T2_us'])
    R = {'inputs': {'pulse_duration_ns': t, 'target_angle_rad': target, 'g_model20_MHz': g_model,
                    'g_design21_MHz': g_design, 'T1_us': t1, 'T2_us': t2, 'tolerance': tol}}

    # 1. characterization replay
    sims = {}
    for label, g in (('model20_g16', g_model), ('design21_g18', g_design)):
        v = np.array([zz_pulse_dynamics(g, t, k) for k in n])
        sims[label] = {'XI': v.round(6).tolist(), 'max_abs_residual': float(np.max(np.abs(v - y))),
                       'within_tolerance': bool(np.max(np.abs(v - y)) <= tol)}
    vn = np.array([zz_pulse_dynamics(g_design, t, k, t1, t2) for k in n])
    sims['design21_g18_with_T1T2'] = {'XI': vn.round(6).tolist(), 'max_abs_residual': float(np.max(np.abs(vn - y))),
                                      'max_shift_from_coherent': float(np.max(np.abs(vn - np.array(sims['design21_g18']['XI']))))}
    # refit g from QuTiP dynamics (minimax over the physical branch near the supplied proxies)
    grid = np.arange(14.0, 20.0, 0.01)
    cost = [np.max(np.abs(np.cos(np.array(n) * rate(g) * t) - y)) for g in grid]
    g0 = float(grid[int(np.argmin(cost))])
    fine = np.arange(g0 - 0.02, g0 + 0.02, 0.0005)
    costf = [max(abs(zz_pulse_dynamics(g, t, k, steps=60) - yy) for k, yy in zip(n, y)) for g in fine]
    g_fit = float(fine[int(np.argmin(costf))])
    R['characterization'] = {'supplied_XI': y.tolist(), 'repetitions': n, 'simulations': sims,
                             'qutip_minimax_fit_g_MHz': g_fit, 'qutip_minimax_fit_max_residual': float(min(costf)),
                             'realized_angle_per_pulse_rad': rate(g_fit) * t}

    # 2. verification companion on the local subsystem {neighbors of Q1, Q1, Q4, Q2}
    vq = next(o for o in obs['observables'] if o.get('circuit'))
    q = vq['qubits'][0]
    phis = ver['phase_angles_rad']
    sub = sorted({q} | {x for e in ver['interactions'] if q in e['edge'] for x in e['edge']} | {2})[:4]
    idx = {p: i for i, p in enumerate(sub)}

    def op_on(p, o):
        ops = [I2] * len(sub)
        ops[idx[p]] = o
        return qt.tensor(*ops)

    def verification_expect(g):
        psi = qt.tensor(*[PLUS] * len(sub))
        for p in sub:
            psi = op_on(p, rz(phis[p])) * psi
        for e in ver['interactions']:
            a, b = e['edge']
            if a in idx and b in idx:
                if tuple(e['edge']) == tuple(cal['physical_edge']):
                    H = 0.5 * rate(g) * op_on(a, Z) * op_on(b, Z)  # target edge: calibrated pulse dynamics
                    psi = qt.sesolve(H, psi, [0, t], options={'atol': 1e-12, 'rtol': 1e-10}).states[-1]
                else:
                    psi = (-1j * e['angle_rad'] / 2 * op_on(a, Z) * op_on(b, Z)).expm() * psi
        return float(qt.expect(op_on(q, X), psi))
    R['verification'] = {'local_subsystem_qubits': sub, 'observed': vq['observed_value'],
                         'tolerance': vq['absolute_tolerance'],
                         'qutip_X_model20_g16': verification_expect(g_model),
                         'qutip_X_design21_g18': verification_expect(g_design)}
    R['verification']['design21_within_tolerance'] = abs(R['verification']['qutip_X_design21_g18'] - vq['observed_value']) \
        <= vq['absolute_tolerance']

    # 3. SX-conjugated RZZ realizes a YY-type pair block
    th = rate(g_design) * t
    S2 = qt.tensor(sx(), sx())
    blk = S2.dag() * rzz(th) * S2
    yy = (-1j * th / 2 * qt.tensor(Y, Y)).expm()
    R['sx_conjugation'] = {'theta_rad': th, 'SXdag_RZZ_SX_equals_exp(-i theta YY/2)': same_up_to_phase(blk, yy),
                           'block_ZZparity_commutes': bool((blk * qt.tensor(Z, Z) - qt.tensor(Z, Z) * blk).norm() < 1e-10)}

    # 4. repair validation
    t_fix = target / rate(g_design)
    amp_fix = g_model / g_design
    R['repair'] = {
        'corrected_pulse_ns': t_fix,
        'XI_corrected_duration': [round(zz_pulse_dynamics(g_design, t_fix, k), 6) for k in n],
        'XI_corrected_amplitude': [round(zz_pulse_dynamics(g_design * amp_fix, t, k), 6) for k in n],
        'expected_cos_n_pi_over_2': [round(math.cos(k * target), 6) for k in n],
        'verification_X_after_repair': verification_expect(g_design * amp_fix),
    }
    R['repair']['max_deviation_from_intended'] = float(max(
        abs(a - b) for a, b in zip(R['repair']['XI_corrected_duration'], R['repair']['expected_cos_n_pi_over_2'])))

    (HERE / 'results.json').write_text(json.dumps(R, indent=2) + '\n', encoding='utf-8')
    c = R['characterization']['simulations']
    print(f"characterization max residual: g=16 -> {c['model20_g16']['max_abs_residual']:.4f}, "
          f"g=18 -> {c['design21_g18']['max_abs_residual']:.4f} (tol {tol}); "
          f"T1/T2 shift {c['design21_g18_with_T1T2']['max_shift_from_coherent']:.2e}; fit g = {g_fit:.4f} MHz")
    v = R['verification']
    print(f"verification <X{q}>: observed {v['observed']:.6f}, g=16 {v['qutip_X_model20_g16']:.6f}, "
          f"g=18 {v['qutip_X_design21_g18']:.6f}")
    print(f"SX-conjugation YY identity: {R['sx_conjugation']}")
    print(f"repair: pulse {t_fix:.4f} ns -> XI {R['repair']['XI_corrected_duration']}; "
          f"<X> after repair {R['repair']['verification_X_after_repair']:.2e}")


if __name__ == '__main__':
    main()
