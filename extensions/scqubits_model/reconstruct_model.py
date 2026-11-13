"""B2 — scqubits reduced device-model reconstruction for Stage 2 (local subsystem Q1/Q4).

1. Rebuilds model-20's two transmons (EJ, EC, ng, ncut, truncated_dim from device_model.json)
   and reports f01, anharmonicity and charge matrix elements.
2. Builds the coupled two-transmon HilbertSpace with a charge-charge interaction whose
   exchange strength equals the supplied coupling proxy (16 MHz from model-20, 18 MHz
   from design-21), and reports dressed spectra, exchange splitting and static ZZ.
3. Cross-checks model-20 against the D21 device design (junction Lj -> EJ, implied
   capacitances) and the supplied reduced rate assumption omega = 2*pi*g/1000 rad/ns.
4. Emits a proposed D21-consistent replacement model record (model-21 candidate).

Reproduce from the repository root:
    python extensions/scqubits_model/reconstruct_model.py
"""
import json
import math
from pathlib import Path

import numpy as np
import scqubits as scq

scq.settings.PROGRESSBAR_DISABLED = True
HERE = Path(__file__).resolve().parent
ART = HERE.parents[1] / 'stages' / 'stage_2' / 'artifacts'

PHI0_RED = 2.067833848e-15 / (2 * math.pi)   # reduced flux quantum, Wb
H_PLANCK = 6.62607015e-34
E_CHARGE = 1.602176634e-19


def load(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


def ej_ghz_from_lj(lj_nh):
    return PHI0_RED ** 2 / (lj_nh * 1e-9) / H_PLANCK / 1e9


def c_ff_from_ec(ec_ghz):
    return E_CHARGE ** 2 / (2 * ec_ghz * 1e9 * H_PLANCK) * 1e15


def transmons(params):
    p = params
    return [scq.Transmon(EJ=p['EJ_GHz'][k], EC=p['EC_GHz'][k], ng=p['ng'][k], ncut=p['ncut'],
                         truncated_dim=p['truncated_dim'], id_str=f'T{k}') for k in range(2)]


def single(t):
    ev = t.eigenvals(evals_count=3)
    n = t.matrixelement_table('n_operator', evals_count=3)
    return {'f01_GHz': float(ev[1] - ev[0]), 'anharmonicity_MHz': float((ev[2] - ev[1]) - (ev[1] - ev[0])) * 1e3,
            'n01': float(abs(n[0, 1])), 'EJ_over_EC': t.EJ / t.EC}


def coupled(tq, g_mhz, levels=None):
    """Charge coupling G n1 n2 with G chosen so the bare exchange g = G*|n01|*|n01'| equals g_mhz.

    The coupled Hamiltonian is assembled in the scqubits single-transmon eigenbasis (eigenvalues and
    n-operator matrix elements from scqubits). scqubits.HilbertSpace is not used because scqubits 4.3.1
    rejects scipy>=1.15 sparse arrays in identity_wrap; this is the same construction done explicitly.
    Dressed states are identified by maximum overlap with bare product states.
    """
    levels = levels or tq[0].truncated_dim
    ev = [t.eigenvals(evals_count=levels) for t in tq]
    nm = [t.matrixelement_table('n_operator', evals_count=levels) for t in tq]
    n01 = [abs(m[0, 1]) for m in nm]
    G = g_mhz / 1e3 / (n01[0] * n01[1])
    Id = np.eye(levels)
    H = np.kron(np.diag(ev[0] - ev[0][0]), Id) + np.kron(Id, np.diag(ev[1] - ev[1][0])) + G * np.kron(nm[0], nm[1])
    w, v = np.linalg.eigh(H)

    def E(a, b):
        k = a * levels + b
        return float(w[int(np.argmax(np.abs(v[k, :]) ** 2))])
    e00, e01, e10, e11 = E(0, 0), E(0, 1), E(1, 0), E(1, 1)
    f = [single(t)['f01_GHz'] for t in tq]
    det = f[1] - f[0]
    dressed_split = abs(e01 - e10)
    return {'G_charge_coupling_GHz': G, 'bare_detuning_MHz': det * 1e3,
            'dressed_01_10_splitting_MHz': dressed_split * 1e3,
            'exchange_g_from_splitting_MHz': 0.5 * math.sqrt(max(dressed_split ** 2 - det ** 2, 0.0)) * 1e3,
            'static_ZZ_kHz': (e11 - e10 - e01 + e00) * 1e6}


def main():
    mod, des, cal, loc = load('device_model.json'), load('device_design.json'), load('control_calibration.json'), \
        load('local_subsystem.json')
    reg = load('provenance_registry.json')
    fp2rev = {r['geometry_fingerprint']: r['revision'] for r in reg['revisions']}
    p = mod['parameters']
    g_model, g_design = mod['coupling_proxy_MHz'], des['local_region']['coupling_proxy_MHz']
    tq = transmons(p)
    R = {'model_id': mod['artifact_id'], 'model_source_revision': fp2rev.get(mod['source_geometry_fingerprint']),
         'design_revision': des['revision'], 'qubits': mod['qubits'],
         'single_transmon': {f'Q{q}': single(t) for q, t in zip(mod['qubits'], tq)}}

    # convergence in ncut
    R['ncut_convergence_f01_delta_MHz'] = max(
        abs(single(scq.Transmon(EJ=t.EJ, EC=t.EC, ng=t.ng, ncut=40, truncated_dim=3))['f01_GHz'] - single(t)['f01_GHz'])
        * 1e3 for t in tq)

    R['coupled'] = {'model20_g16': coupled(tq, g_model), 'design21_g18': coupled(tq, g_design),
                    'design21_g18_6levels': coupled(tq, g_design, levels=6)}

    # reduced-rate consequence (supplied assumption, not derived from the Hamiltonian)
    rate = lambda g: 2 * math.pi * g / 1000.0
    R['reduced_rate'] = {
        'assumption': mod['reduced_assumption'],
        'omega_model20_rad_per_ns': rate(g_model), 'omega_design21_rad_per_ns': rate(g_design),
        'calibration_nominal_rate_rad_per_ns': cal['nominal_rate_rad_per_ns'],
        'calibration_matches_model20': abs(cal['nominal_rate_rad_per_ns'] - rate(g_model)) < 1e-12,
        'realized_angle_rad': rate(g_design) * cal['pulse_duration_ns'],
        'intended_angle_rad': cal['target_angle_rad'],
    }

    # cross-check model-20 against D21 design junction/capacitance metadata
    comps = {c['id']: c for c in des['components']}
    lj = [comps[f'geom-Q{q}']['Lj_nH'] for q in mod['qubits']]
    cj = [comps[f'geom-Q{q}']['Cj_fF'] for q in mod['qubits']]
    ej_d21 = [ej_ghz_from_lj(x) for x in lj]
    R['design_cross_check'] = {
        'design21_Lj_nH': lj, 'design21_EJ_from_Lj_GHz': ej_d21,
        'model20_EJ_GHz': p['EJ_GHz'], 'model20_implied_Lj_nH': [PHI0_RED ** 2 / (e * 1e9 * H_PLANCK) * 1e9 for e in p['EJ_GHz']],
        'model20_implied_total_C_fF': [c_ff_from_ec(e) for e in p['EC_GHz']],
        'design21_junction_Cj_fF': cj,
        'note': 'EC depends on total island capacitance (cross + gap + junction), which needs an EM extraction not '
                'supplied; only the junction Cj is given, so EC is not cross-checked.',
    }
    t_d21 = [scq.Transmon(EJ=ej, EC=ec, ng=0.0, ncut=p['ncut'], truncated_dim=3) for ej, ec in zip(ej_d21, p['EC_GHz'])]
    R['design_cross_check']['f01_if_EJ_from_D21_Lj_GHz'] = [single(t)['f01_GHz'] for t in t_d21]

    # proposed replacement record (minimal repair: coupling proxy from D21 design)
    cand = dict(mod)
    cand.update(artifact_id='model-21-candidate', source_geometry_fingerprint=des['geometry_fingerprint'],
                coupling_proxy_MHz=g_design, created_by='extensions/scqubits_model/reconstruct_model.py',
                notes='Coupling proxy taken from design-21 local_region; transmon EJ/EC retained from model-20 pending '
                      'an EM capacitance extraction and junction characterization for D21 (see design_cross_check).')
    R['proposed_model_record'] = cand

    (HERE / 'results.json').write_text(json.dumps(R, indent=2) + '\n', encoding='utf-8')
    for q, s in R['single_transmon'].items():
        print(f"{q}: f01 {s['f01_GHz']:.4f} GHz, alpha {s['anharmonicity_MHz']:.1f} MHz, |n01| {s['n01']:.4f}, "
              f"EJ/EC {s['EJ_over_EC']:.1f}")
    print(f"ncut convergence: {R['ncut_convergence_f01_delta_MHz']:.2e} MHz")
    for k, v in R['coupled'].items():
        print(f"{k}: detuning {v['bare_detuning_MHz']:.2f} MHz, dressed splitting {v['dressed_01_10_splitting_MHz']:.3f}"
              f" MHz, g from splitting {v['exchange_g_from_splitting_MHz']:.3f} MHz, static ZZ {v['static_ZZ_kHz']:.1f} kHz")
    r = R['reduced_rate']
    print(f"omega model20 {r['omega_model20_rad_per_ns']:.6f}, design21 {r['omega_design21_rad_per_ns']:.6f} rad/ns; "
          f"realized angle {r['realized_angle_rad']:.6f} rad")
    dc = R['design_cross_check']
    print(f"EJ from D21 Lj {dc['design21_EJ_from_Lj_GHz']} vs model20 {dc['model20_EJ_GHz']}; "
          f"implied Lj {dc['model20_implied_Lj_nH']}; implied C {dc['model20_implied_total_C_fF']}; "
          f"f01 with D21 EJ {dc['f01_if_EJ_from_D21_Lj_GHz']}")


if __name__ == '__main__':
    main()
