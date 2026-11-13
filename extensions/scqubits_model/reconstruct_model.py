"""B2 — scqubits reduced device-model reconstruction for Stage 2 (local subsystem Q1/Q4).

1. Rebuilds model-20's two transmons (EJ, EC, ng, ncut, truncated_dim from device_model.json) and
   reports f01, anharmonicity and charge matrix elements (with ncut convergence).
2. Builds the coupled pair with scqubits.HilbertSpace (charge-charge coupling G n1 n2, G set so the bare
   exchange equals the supplied proxy), cross-checked against an explicit eigenbasis construction.
3. Tests which coupling mechanism the supplied characterization supports: the supplied linear proxy
   (omega = 2*pi*g/1000) vs a static-ZZ mechanism (rate ~ g^2), and sweeps g.
4. Cross-checks model-20 against the D21 design (junction Lj -> EJ) and against the B1 electrostatic
   capacitance estimate (EC), when extensions/device_reconstruction/results.json is present.
5. Emits a D21-consistent replacement model record (model-21 candidate).

Reproduce from the repository root (~3 s):
    python extensions/scqubits_model/reconstruct_model.py
"""
import json
import math
import platform
import warnings
from pathlib import Path

import numpy as np
import scipy
import scqubits as scq

warnings.filterwarnings('ignore')
scq.settings.PROGRESSBAR_DISABLED = True
HERE = Path(__file__).resolve().parent
ART = HERE.parents[1] / 'stages' / 'stage_2' / 'artifacts'
B1_RESULTS = HERE.parent / 'device_reconstruction' / 'results.json'

PHI0_RED = 2.067833848e-15 / (2 * math.pi)   # Wb
H_PLANCK = 6.62607015e-34                    # J s
E_CHARGE = 1.602176634e-19                   # C


def load(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


def ej_ghz_from_lj(lj_nh):
    return PHI0_RED ** 2 / (lj_nh * 1e-9) / H_PLANCK / 1e9


def c_ff_from_ec(ec_ghz):
    return E_CHARGE ** 2 / (2 * ec_ghz * 1e9 * H_PLANCK) * 1e15


def transmons(p, ncut=None):
    return [scq.Transmon(EJ=p['EJ_GHz'][k], EC=p['EC_GHz'][k], ng=p['ng'][k], ncut=ncut or p['ncut'],
                         truncated_dim=p['truncated_dim'], id_str=f'T{k}') for k in range(2)]


def single(t):
    ev = t.eigenvals(evals_count=3)
    n = t.matrixelement_table('n_operator', evals_count=3)
    return {'f01_GHz': float(ev[1] - ev[0]), 'anharmonicity_MHz': float((ev[2] - ev[1]) - (ev[1] - ev[0])) * 1e3,
            'n01': float(abs(n[0, 1])), 'EJ_over_EC': t.EJ / t.EC}


def charge_G(tq, g_mhz):
    n01 = [abs(t.matrixelement_table('n_operator', evals_count=2)[0, 1]) for t in tq]
    return g_mhz / 1e3 / (n01[0] * n01[1])


def coupled_hilbertspace(tq, g_mhz):
    """scqubits.HilbertSpace with the Quantum Metal compatibility shim (QuTiP 5 returns scipy sparse arrays,
    which scqubits 4.3.1 identity_wrap rejects; qiskit_metal ships the fix)."""
    from qiskit_metal.analyses.quantization import _scqubits_compat
    _scqubits_compat.apply()
    hs = scq.HilbertSpace(tq)
    hs.add_interaction(g=charge_G(tq, g_mhz), op1=tq[0].n_operator, op2=tq[1].n_operator, add_hc=False)
    hs.generate_lookup()
    E = lambda a, b: float(hs.energy_by_bare_index((a, b)))
    return E(0, 0), E(0, 1), E(1, 0), E(1, 1)


def coupled_explicit(tq, g_mhz, levels):
    """Same Hamiltonian assembled explicitly in the scqubits single-transmon eigenbasis (cross-check)."""
    ev = [t.eigenvals(evals_count=levels) for t in tq]
    nm = [t.matrixelement_table('n_operator', evals_count=levels) for t in tq]
    Id = np.eye(levels)
    H = np.kron(np.diag(ev[0]), Id) + np.kron(Id, np.diag(ev[1])) + charge_G(tq, g_mhz) * np.kron(nm[0], nm[1])
    w, v = np.linalg.eigh(H)
    E = lambda a, b: float(w[int(np.argmax(np.abs(v[a * levels + b, :]) ** 2))])
    overlaps = {f'{a}{b}': float(np.max(np.abs(v[a * levels + b, :]) ** 2)) for a, b in ((0, 1), (1, 0), (1, 1))}
    return (E(0, 0), E(0, 1), E(1, 0), E(1, 1)), overlaps


def summarize(e, det):
    e00, e01, e10, e11 = e
    return {'dressed_01_10_splitting_MHz': abs(e01 - e10) * 1e3, 'static_ZZ_MHz': (e11 - e10 - e01 + e00) * 1e3,
            'bare_detuning_MHz': det * 1e3}


def main():
    mod, des, cal, char = load('device_model.json'), load('device_design.json'), load('control_calibration.json'), \
        load('characterization.json')
    reg = load('provenance_registry.json')
    fp2rev = {r['geometry_fingerprint']: r['revision'] for r in reg['revisions']}
    p = mod['parameters']
    g_model, g_design = mod['coupling_proxy_MHz'], des['local_region']['coupling_proxy_MHz']
    t_ns, target = cal['pulse_duration_ns'], cal['target_angle_rad']
    tq = transmons(p)
    s = [single(t) for t in tq]
    det = s[1]['f01_GHz'] - s[0]['f01_GHz']
    R = {'versions': {'python': platform.python_version(), 'scqubits': scq.__version__, 'numpy': np.__version__,
                      'scipy': scipy.__version__},
         'model_id': mod['artifact_id'], 'model_source_revision': fp2rev.get(mod['source_geometry_fingerprint']),
         'design_revision': des['revision'], 'qubits': mod['qubits'],
         'single_transmon': {f'Q{q}': v for q, v in zip(mod['qubits'], s)},
         'ncut_convergence_f01_delta_MHz': max(abs(single(t2)['f01_GHz'] - v['f01_GHz']) * 1e3
                                               for t2, v in zip(transmons(p, ncut=40), s))}

    # ---- coupled pair ----
    coupled = {}
    for label, g in (('model20_g16', g_model), ('design21_g18', g_design)):
        hs = summarize(coupled_hilbertspace(tq, g), det)
        ex3, ov = coupled_explicit(tq, g, 3)
        ex6, _ = coupled_explicit(tq, g, 6)
        a1, a2 = s[0]['anharmonicity_MHz'] / 1e3, s[1]['anharmonicity_MHz'] / 1e3
        g_ghz = g / 1e3
        zz_pert = 2 * g_ghz ** 2 * (a1 + a2) / ((det + a1) * (det - a2)) * 1e3  # MHz, 2nd-order dispersive estimate
        coupled[label] = {'charge_coupling_G_GHz': charge_G(tq, g), 'hilbertspace': hs,
                          'explicit_3_levels': summarize(ex3, det), 'explicit_6_levels': summarize(ex6, det),
                          'dressed_state_max_overlaps': ov, 'static_ZZ_perturbative_MHz': zz_pert,
                          'g_over_detuning': g_ghz / abs(det)}
    R['coupled'] = coupled
    R['coupled_note'] = ('G is chosen so that the bare exchange G*|n01|*|n01| equals the proxy, so the dressed '
                         'splitting sqrt(det^2 + 4 g^2) reproduces the proxy by construction (consistency check, not '
                         'an independent extraction). Static ZZ of several MHz follows because g/detuning ~ 0.25; that '
                         'is outside the supplied reduced model, which states "Proxy g includes drive scaling" and '
                         'treats only the driven entangling rate.')

    # ---- which mechanism does the characterization support? ----
    n = np.array([q['repetitions'] for q in char['probes']]); y = np.array([q['expectation'] for q in char['probes']])
    tol = char['absolute_tolerance']
    rate = lambda g: 2 * math.pi * g / 1000.0
    lin = rate(g_design) * t_ns
    sq = target * (g_design / g_model) ** 2          # if the realized angle scaled like static ZZ (~g^2)
    # zeta = E11-E10-E01+E00 corresponds to H = (h*zeta/4) ZZ, i.e. exp(-i theta ZZ/2) with theta = pi*zeta*t
    zz_static_angle = math.pi * coupled['design21_g18']['hilbertspace']['static_ZZ_MHz'] / 1e3 * t_ns
    R['mechanism_test'] = {
        'linear_proxy_angle_rad': lin,
        'linear_proxy_max_residual': float(np.max(np.abs(np.cos(n * lin) - y))),
        'g_squared_scaling_angle_rad': sq,
        'g_squared_scaling_max_residual': float(np.max(np.abs(np.cos(n * sq) - y))),
        'g_squared_scaling_XI_n1': math.cos(sq),
        'static_ZZ_only_angle_per_pulse_rad': zz_static_angle,
        'static_ZZ_only_max_residual': float(np.max(np.abs(np.cos(n * zz_static_angle) - y))),
        'tolerance': tol,
        'conclusion': 'Only the supplied linear proxy (omega = 2*pi*g/1000 with g = 18 MHz) reproduces the probes '
                      'within tolerance; a rate scaling as g^2 or the bare static ZZ do not.',
    }
    sweep = np.round(np.arange(14.0, 20.01, 0.5), 2)
    R['g_sweep'] = [{'g_MHz': float(g), 'linear_angle_rad': rate(g) * t_ns,
                     'max_residual': float(np.max(np.abs(np.cos(n * rate(g) * t_ns) - y))),
                     'static_ZZ_MHz': summarize(coupled_explicit(tq, g, 3)[0], det)['static_ZZ_MHz']} for g in sweep]

    # ---- reduced-rate consequence (supplied assumption) ----
    R['reduced_rate'] = {
        'assumption': mod['reduced_assumption'],
        'omega_model20_rad_per_ns': rate(g_model), 'omega_design21_rad_per_ns': rate(g_design),
        'calibration_nominal_rate_rad_per_ns': cal['nominal_rate_rad_per_ns'],
        'calibration_matches_model20': abs(cal['nominal_rate_rad_per_ns'] - rate(g_model)) < 1e-12,
        'realized_angle_rad': lin, 'intended_angle_rad': target,
    }

    # ---- design cross-checks ----
    comps = {c['id']: c for c in des['components']}
    lj = [comps[f'geom-Q{q}']['Lj_nH'] for q in mod['qubits']]
    ej_d21 = [ej_ghz_from_lj(x) for x in lj]
    dc = {'design21_Lj_nH': lj, 'design21_EJ_from_Lj_GHz': ej_d21, 'model20_EJ_GHz': p['EJ_GHz'],
          'model20_implied_Lj_nH': [PHI0_RED ** 2 / (e * 1e9 * H_PLANCK) * 1e9 for e in p['EJ_GHz']],
          'model20_implied_total_C_fF': [c_ff_from_ec(e) for e in p['EC_GHz']],
          'f01_if_EJ_from_D21_Lj_GHz': [single(scq.Transmon(EJ=e, EC=c, ng=0.0, ncut=p['ncut'], truncated_dim=3))['f01_GHz']
                                         for e, c in zip(ej_d21, p['EC_GHz'])],
          'caveat': 'model-20 describes itself as "a supplied effective model, not a claimed ab initio transmon '
                    'extraction"; the D20 geometry is not supplied, and no frequency data exist to test either EJ. '
                    'The mismatch is consistent with model-20 not being derived from D21; it is not proof.'}
    if B1_RESULTS.exists():
        b1 = json.loads(B1_RESULTS.read_text(encoding='utf-8')).get('electrostatics', {})
        runs = b1.get('runs', [])
        if runs:
            ecs = [e for r in runs for e in r['EC_GHz']]
            dc['B1_electrostatic_EC_GHz_range'] = [min(ecs), max(ecs)]
            dc['B1_EC_consistent_with_model20_EC'] = min(ecs) - 0.02 <= max(p['EC_GHz']) and \
                min(p['EC_GHz']) <= max(ecs) + 0.02
    R['design_cross_check'] = dc

    cand = dict(mod)
    cand.update(artifact_id='model-21-candidate', source_geometry_fingerprint=des['geometry_fingerprint'],
                coupling_proxy_MHz=g_design, created_at=None, created_by='extensions/scqubits_model/reconstruct_model.py',
                notes='Coupling proxy from design-21 local_region. Transmon EJ/EC retained from model-20 pending D21 '
                      'junction characterization and EM capacitance extraction (see design_cross_check). created_at '
                      'left null: to be set when registered.')
    R['proposed_model_record'] = cand

    (HERE / 'results.json').write_text(json.dumps(R, indent=2) + '\n', encoding='utf-8')
    for q, v in R['single_transmon'].items():
        print(f"{q}: f01 {v['f01_GHz']:.4f} GHz, alpha {v['anharmonicity_MHz']:.1f} MHz, |n01| {v['n01']:.4f}, "
              f"EJ/EC {v['EJ_over_EC']:.1f}")
    for k, v in coupled.items():
        h, e3, e6 = v['hilbertspace'], v['explicit_3_levels'], v['explicit_6_levels']
        print(f"{k}: HilbertSpace split {h['dressed_01_10_splitting_MHz']:.4f} MHz ZZ {h['static_ZZ_MHz']:.4f} MHz | "
              f"explicit(3) ZZ {e3['static_ZZ_MHz']:.4f} | explicit(6) ZZ {e6['static_ZZ_MHz']:.4f} | "
              f"pert ZZ {v['static_ZZ_perturbative_MHz']:.3f} | g/det {v['g_over_detuning']:.3f} | overlaps {v['dressed_state_max_overlaps']}")
    m = R['mechanism_test']
    print(f"mechanism: linear resid {m['linear_proxy_max_residual']:.4f}, g^2 resid {m['g_squared_scaling_max_residual']:.3f} "
          f"(XI n=1 {m['g_squared_scaling_XI_n1']:.3f}), static-ZZ resid {m['static_ZZ_only_max_residual']:.3f}")
    print(f"design: EJ D21 {ej_d21} vs model {p['EJ_GHz']}; B1 EC range {dc.get('B1_electrostatic_EC_GHz_range')}")


if __name__ == '__main__':
    main()
