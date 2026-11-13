"""B1 — Quantum Metal (formerly Qiskit Metal) device/geometry reconstruction for Stage 2.

Rebuilds the D21 device from device_design.json as a Quantum Metal DesignPlanar:
  * 32 TransmonCross qubits at the supplied positions and cross dimensions;
  * the local Q1-Q4 capacitive coupler geom-C14 (gap and length from local_region).
Then uses the rendered Metal qgeometry (shapely polygons) to check the reconstruction
against the rest of the Stage-2 evidence:
  * geometric nearest-neighbour graph == backend_snapshot edges (physical ordering 0-1-4-2-...);
  * the coupler bridges exactly Q1 and Q4 (the target edge) and touches no other qubit;
  * every geometry id in provenance_registry.geometry_ids / local_subsystem.geometry_refs exists;
  * junction metadata (Lj) -> EJ, compared with device_model.json (model-20);
and exports the qgeometry tables, a local-region plot and a reconstruction-recipe digest.

Limitation: no EM solver (Ansys/pyEPR, gmsh+Elmer) is available, so the coupling capacitance
and hence g are NOT extracted here; the supplied coupling_proxy_MHz is used as authoritative.

Reproduce from the repository root (headless):
    QT_QPA_PLATFORM=offscreen python extensions/device_reconstruction/reconstruct_geometry.py
"""
import hashlib
import json
import math
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from shapely.geometry import box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from qiskit_metal import designs, draw  # noqa: E402
from qiskit_metal.qlibrary.core import QComponent  # noqa: E402
from qiskit_metal.qlibrary.qubits.transmon_cross import TransmonCross  # noqa: E402

HERE = Path(__file__).resolve().parent
ART = HERE.parents[1] / 'stages' / 'stage_2' / 'artifacts'
PHI0_RED = 2.067833848e-15 / (2 * math.pi)
H_PLANCK = 6.62607015e-34


def load(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


class GapCoupler(QComponent):
    """Series gap capacitor of total length `length` centred at (pos_x, pos_y) along x, made of two
    pads of width `width` separated by `gap`. Assumption (documented in README): the coupler spans the
    space between the facing cross arms and overlaps each arm, so each pad is galvanically joined to one
    qubit and the only series element is the capacitive gap."""
    default_options = dict(pos_x='0um', pos_y='0um', length='200um', gap='18um', width='20um')

    def make(self):
        p = self.p
        half = p.length / 2
        left = box(p.pos_x - half, p.pos_y - p.width / 2, p.pos_x - p.gap / 2, p.pos_y + p.width / 2)
        right = box(p.pos_x + p.gap / 2, p.pos_y - p.width / 2, p.pos_x + half, p.pos_y + p.width / 2)
        self.add_qgeometry('poly', {'pad_left': left, 'pad_right': right})


def main():
    des, be, reg, loc, mod = load('device_design.json'), load('backend_snapshot.json'), \
        load('provenance_registry.json'), load('local_subsystem.json'), load('device_model.json')
    design = designs.DesignPlanar()
    design.overwrite_enabled = True
    design.chips.main.size['size_x'] = '17mm'

    comps = {c['id']: c for c in des['components']}
    qubits = {}
    for cid, c in comps.items():
        if c['type'] != 'transmon_cross':
            continue
        q = int(cid.split('Q')[-1])
        qubits[q] = TransmonCross(design, f'Q{q}', options=dict(
            pos_x=f'{c["x_um"]}um', pos_y=f'{c["y_um"]}um', cross_length=f'{c["cross_length_um"]}um',
            cross_width=f'{c["cross_width_um"]}um', cross_gap=f'{c["gap_um"]}um'))
    lr = des['local_region']
    qa, qb = lr['qubits']
    cpl = lr['coupler']
    ca, cb = comps[f'geom-Q{qa}'], comps[f'geom-Q{qb}']
    coupler = GapCoupler(design, cpl['id'].replace('geom-', ''), options=dict(
        pos_x=f'{(ca["x_um"] + cb["x_um"]) / 2}um', pos_y=f'{(ca["y_um"] + cb["y_um"]) / 2}um',
        length=f'{cpl["length_um"]}um', gap=f'{cpl["gap_um"]}um', width=f'{ca["cross_width_um"]}um'))
    design.rebuild()

    # ---- extract rendered geometry (Metal qgeometry, units mm) ----
    poly = design.qgeometry.tables['poly']
    id2name = {v.id: k for k, v in design.components.items()}
    metal = {}
    for _, row in poly.iterrows():
        if row['subtract']:
            continue
        name = id2name[row['component']]
        metal.setdefault(name, []).append(row['geometry'])
    metal = {k: unary_union(v) for k, v in metal.items()}
    cname = coupler.name

    # nearest-neighbour graph from geometry (arm-tip separation)
    names = sorted(qubits)
    sep = {}
    for i in names:
        for j in names:
            if i < j:
                sep[(i, j)] = metal[f'Q{i}'].distance(metal[f'Q{j}']) * 1e3  # um
    dmin = min(sep.values())
    geo_edges = {e for e, s in sep.items() if abs(s - dmin) < 1e-6}
    be_edges = {tuple(sorted(e)) for e in be['edges']}

    # coupler connectivity
    pads = [g for _, r in poly.iterrows() if id2name[r['component']] == cname for g in [r['geometry']]]
    touches = {q for q in names if any(p.intersects(metal[f'Q{q}']) for p in pads)}
    pad_owner = [[q for q in names if p.intersects(metal[f'Q{q}'])] for p in pads]
    pad_gap_um = pads[0].distance(pads[1]) * 1e3

    ids_needed = set(reg.get('geometry_ids', [])) | set(loc.get('geometry_refs', []))
    ids_built = {f'geom-Q{q}' for q in qubits} | {cpl['id']}
    order_x = [q for q in sorted(qubits, key=lambda q: comps[f'geom-Q{q}']['x_um'])]

    lj = [comps[f'geom-Q{q}']['Lj_nH'] for q in mod['qubits']]
    ej_design = [PHI0_RED ** 2 / (x * 1e-9) / H_PLANCK / 1e9 for x in lj]

    recipe = json.dumps({'components': des['components'], 'local_region': lr}, sort_keys=True).encode()
    R = {
        'metal_version': __import__('qiskit_metal').__version__,
        'design_revision': des['revision'],
        'components_built': len(design.components),
        'qubits_built': len(qubits),
        'geometry_ids_required': sorted(ids_needed), 'geometry_ids_missing': sorted(ids_needed - ids_built),
        'physical_order_by_x': order_x,
        'nearest_neighbour_arm_separation_um': dmin,
        'geometric_edges_equal_backend_edges': geo_edges == be_edges,
        'geometric_edges_not_in_backend': sorted(geo_edges - be_edges),
        'backend_edges_not_geometric': sorted(be_edges - geo_edges),
        'target_edge': sorted(be['selected_edge']),
        'coupler': {'id': cpl['id'], 'type': cpl['type'], 'length_um': cpl['length_um'], 'gap_um_supplied': cpl['gap_um'],
                    'gap_um_rendered': pad_gap_um, 'touches_qubits': sorted(touches), 'pad_owners': pad_owner,
                    'bridges_exactly_target_edge': sorted(touches) == sorted(be['selected_edge'])
                    and all(len(o) == 1 for o in pad_owner),
                    'arm_tip_separation_um': sep[tuple(sorted((qa, qb)))]},
        'coupling_proxy_MHz_design21': lr['coupling_proxy_MHz'],
        'coupling_proxy_MHz_model20': mod['coupling_proxy_MHz'],
        'junction_check': {'design21_Lj_nH': lj, 'design21_EJ_GHz': ej_design, 'model20_EJ_GHz': mod['parameters']['EJ_GHz']},
        'fingerprint_note': 'design geometry_fingerprint is an opaque registry id ("not a digest of this JSON"); '
                            'the reconstruction recipe digest below identifies this rebuild, it is not expected to equal it.',
        'reconstruction_recipe_sha256': hashlib.sha256(recipe).hexdigest(),
        'design_geometry_fingerprint': des['geometry_fingerprint'],
        'em_extraction': 'not performed: no Ansys/pyEPR or gmsh+Elmer renderer available; g not re-derived.',
    }

    # exports
    tbl = poly[['component', 'name', 'subtract', 'layer', 'chip']].copy()
    tbl['component'] = [id2name[c] for c in tbl['component']]
    tbl['bounds_mm'] = [list(map(lambda v: round(v, 6), g.bounds)) for g in poly['geometry']]
    (HERE / 'qgeometry_poly.json').write_text(tbl.to_json(orient='records', indent=1), encoding='utf-8')
    fig, ax = plt.subplots(figsize=(9, 3))
    for name in (f'Q{x}' for x in order_x[:5]):
        draw.mpl.render(metal[name], ax=ax, kw=dict(fc='#4c72b0', alpha=0.7))
        c = metal[name].centroid
        ax.text(c.x, c.y - 0.25, name, ha='center')
    for p in pads:
        draw.mpl.render(p, ax=ax, kw=dict(fc='#dd8452'))
    xs = [metal[f'Q{x}'].bounds for x in order_x[:5]]
    ax.set_xlim(min(b[0] for b in xs) - 0.1, max(b[2] for b in xs) + 0.1)
    ax.set_ylim(-0.35, 0.25)
    ax.set_aspect('equal'); ax.set_xlabel('x (mm)'); ax.set_ylabel('y (mm)')
    ax.set_title(f'{des["revision"]} reconstruction: Q0-Q1-Q4-Q2-Q3 and coupler {cpl["id"]} (orange)')
    fig.tight_layout(); fig.savefig(HERE / 'local_region.png', dpi=150)
    (HERE / 'results.json').write_text(json.dumps(R, indent=2) + '\n', encoding='utf-8')

    print(f"built {R['qubits_built']} qubits + coupler; missing geometry ids: {R['geometry_ids_missing']}")
    print(f"physical order by x: {order_x[:8]}...; geometric NN edges == backend edges: "
          f"{R['geometric_edges_equal_backend_edges']} (arm separation {dmin:.1f} um)")
    print(f"coupler {cpl['id']}: touches {sorted(touches)}, pad owners {pad_owner}, rendered gap {pad_gap_um:.1f} um, "
          f"bridges target edge: {R['coupler']['bridges_exactly_target_edge']}")
    print(f"EJ from D21 Lj {ej_design} GHz vs model-20 {mod['parameters']['EJ_GHz']} GHz")


if __name__ == '__main__':
    main()
