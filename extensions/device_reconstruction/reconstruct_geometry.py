"""B1 — Quantum Metal (formerly Qiskit Metal) device/geometry reconstruction for Stage 2.

Scope: the supplied local subsystem (Q1, Q4 and coupler geom-C14, from local_subsystem.json),
plus their chain neighbours Q0 and Q2 for context.

1. Builds the region as a Quantum Metal DesignPlanar: TransmonCross qubits at the supplied positions
   and cross dimensions, and the capacitive coupler geom-C14 with its own etched pocket so it is
   isolated from the ground plane.
2. Consistency checks against the supplied records: geometry ids, chain ordering 0-1-4-2 vs backend
   edges, coupler pads owned by Q1/Q4 only, design Lj -> EJ vs model-20 EJ.
3. Independent coupling estimate (no commercial EM solver available): a 3-D finite-difference
   electrostatic solve of the Metal-rendered Q1/Q4 islands (thin metal on silicon, ground plane outside
   the etched pockets) gives the coupling capacitance Cc and island capacitances C1, C2 at several grid
   spacings; these give EC and an exchange g = Cc*sqrt(w1*w2)/(2*sqrt(C1*C2)).
4. Exports: qgeometry table, GDS (gdstk) of the local region, a plot, and results.json with versions.

Reproduce from the repository root (headless, ~40 s, ~2 GB RAM):
    QT_QPA_PLATFORM=offscreen python extensions/device_reconstruction/reconstruct_geometry.py
"""
import hashlib
import json
import math
import os
import platform
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import scipy  # noqa: E402
import scipy.sparse as sp  # noqa: E402
import scipy.sparse.linalg as spl  # noqa: E402
import shapely  # noqa: E402
from shapely import contains_xy  # noqa: E402
from shapely.geometry import box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

import qiskit_metal  # noqa: E402
from qiskit_metal import designs, draw  # noqa: E402
from qiskit_metal.qlibrary.core import QComponent  # noqa: E402
from qiskit_metal.qlibrary.qubits.transmon_cross import TransmonCross  # noqa: E402

HERE = Path(__file__).resolve().parent
ART = HERE.parents[1] / 'stages' / 'stage_2' / 'artifacts'
PHI0_RED = 2.067833848e-15 / (2 * math.pi)   # Wb
H_PLANCK = 6.62607015e-34                    # J s
E_CHARGE = 1.602176634e-19                   # C
EPS0 = 8.8541878128e-12                      # F/m
EPS_SI = 11.45                               # silicon, cryogenic
GRID_UM = (10.0, 6.0, 5.0, 4.0)              # finite-difference grid spacings (um) near the metal


def load(name):
    return json.loads((ART / name).read_text(encoding='utf-8'))


class GapCoupler(QComponent):
    """Series gap capacitor along x, centred at (pos_x, pos_y): two pads of width `width`, total length
    `length`, separated by `gap`, inside an etched pocket (`pocket` wide) so the pads are isolated from
    the ground plane. Assumption: each pad overlaps the facing cross arm of one qubit (galvanic joint),
    so the only series element between the two islands is the gap."""
    default_options = dict(pos_x='0um', pos_y='0um', length='200um', gap='18um', width='20um', pocket='20um')

    def make(self):
        p = self.p
        half = p.length / 2
        left = box(p.pos_x - half, p.pos_y - p.width / 2, p.pos_x - p.gap / 2, p.pos_y + p.width / 2)
        right = box(p.pos_x + p.gap / 2, p.pos_y - p.width / 2, p.pos_x + half, p.pos_y + p.width / 2)
        etch = box(p.pos_x - half, p.pos_y - p.width / 2 - p.pocket, p.pos_x + half, p.pos_y + p.width / 2 + p.pocket)
        self.add_qgeometry('poly', {'pad_left': left, 'pad_right': right})
        self.add_qgeometry('poly', {'pocket': etch}, subtract=True)


def graded(lo, hi, h, core, grow=1.25, hmax=40.0):
    pts = list(np.arange(core[0], core[1] + h / 2, h))
    s = h
    while pts[0] > lo:
        s = min(s * grow, hmax); pts.insert(0, pts[0] - s)
    s = h
    while pts[-1] < hi:
        s = min(s * grow, hmax); pts.append(pts[-1] + s)
    return np.array(pts)


def capacitance(islands_um, pockets_um, h, bounds):
    """3-D finite-difference Laplace solve (non-uniform grid, uniform spacing h near the metal).
    Metal is a zero-thickness sheet at z=0; silicon (EPS_SI) below, vacuum above; the sheet outside the
    etched pockets is grounded, as is the outer box. Returns the 2x2 Maxwell capacitance matrix in fF."""
    (x0, x1), (y0, y1) = bounds
    x = graded(x0 - 300, x1 + 300, h, (x0, x1))
    y = graded(y0 - 300, y1 + 300, h, (y0, y1))
    z = graded(-500, 500, h, (-2 * h, 2 * h))
    iz0 = int(np.argmin(abs(z))); z = z - z[iz0]
    nx, ny, nz = len(x), len(y), len(z); N = nx * ny * nz
    X, Y = np.meshgrid(x, y, indexing='ij')
    plane = np.zeros((nx, ny), int)
    plane[contains_xy(pockets_um, X, Y)] = -1
    for k, g in enumerate(islands_um):
        plane[contains_xy(g, X, Y)] = k + 1
    fixed = -np.ones((nx, ny, nz), int)
    fixed[[0, -1]] = 0; fixed[:, [0, -1]] = 0; fixed[:, :, [0, -1]] = 0
    pl = fixed[:, :, iz0]; pl[plane >= 0] = plane[plane >= 0]; fixed[:, :, iz0] = pl

    def dual(c):
        d = np.zeros(len(c)); d[1:-1] = (c[2:] - c[:-2]) / 2; d[0] = (c[1] - c[0]) / 2; d[-1] = (c[-1] - c[-2]) / 2
        return d
    widths = [dual(x), dual(y), dual(z)]
    idx = np.arange(N).reshape(nx, ny, nz); fx = fixed.ravel(); fr = fx == -1
    ez = np.where(z < 0, EPS_SI, 1.0); ez[iz0] = (EPS_SI + 1) / 2
    faces = []
    for ax, c in enumerate((x, y, z)):
        s1 = [slice(None)] * 3; s2 = [slice(None)] * 3; s1[ax] = slice(0, -1); s2[ax] = slice(1, None)
        area = np.ones((nx, ny, nz))
        for o in range(3):
            if o != ax:
                shp = [1, 1, 1]; shp[o] = -1; area = area * widths[o].reshape(shp)
        area = area[tuple(s1)]
        shp = [1, 1, 1]; shp[ax] = -1
        if ax == 2:
            zm = 0.5 * (z[:-1] + z[1:]); epsf = np.where(zm < 0, EPS_SI, 1.0).reshape(1, 1, -1)
        else:
            epsf = ez.reshape(1, 1, -1)
        G = (epsf * area / np.diff(c).reshape(shp)).ravel()
        faces.append((idx[tuple(s1)].ravel(), idx[tuple(s2)].ravel(), G))
    fi = np.flatnonzero(fr); mapf = -np.ones(N, int); mapf[fi] = np.arange(len(fi))
    R, Cc, V = [], [], []; D = np.zeros(len(fi)); B = {k: np.zeros(len(fi)) for k in (1, 2)}
    for a, b, G in faces:
        for u, v in ((a, b), (b, a)):
            m = fr[u]; uu, vv, gg = u[m], v[m], G[m]
            np.add.at(D, mapf[uu], gg)
            ff = fr[vv]; R.append(mapf[uu[ff]]); Cc.append(mapf[vv[ff]]); V.append(-gg[ff])
            for k in (1, 2):
                kk = fx[vv] == k; np.add.at(B[k], mapf[uu[kk]], gg[kk])
    n = len(fi)
    A = sp.csr_matrix((np.concatenate(V + [D]), (np.concatenate(R + [np.arange(n)]), np.concatenate(Cc + [np.arange(n)]))),
                      shape=(n, n))
    Cm = np.zeros((2, 2)); info_all = []
    for k in (1, 2):
        sol, info = spl.cg(A, B[k], rtol=1e-9, maxiter=50000, M=sp.diags(1 / D))
        info_all.append(int(info))
        Vv = np.zeros(N); Vv[fi] = sol; Vv[fx == k] = 1.0
        for j in (1, 2):
            Q = 0.0
            for a, b, G in faces:
                for u, v in ((a, b), (b, a)):
                    m = (fx[u] == j) & (fx[v] != j)
                    Q += np.sum(G[m] * (Vv[u[m]] - Vv[v[m]]))
            Cm[j - 1, k - 1] = EPS0 * 1e-6 * Q * 1e15   # um-scaled conductance -> fF
    return Cm, N, info_all


def transmon_f01_ghz(ej, ec):
    return math.sqrt(8 * ej * ec) - ec


def main():
    t_start = time.time()
    des, be, reg, loc, mod = load('device_design.json'), load('backend_snapshot.json'), \
        load('provenance_registry.json'), load('local_subsystem.json'), load('device_model.json')
    comps = {c['id']: c for c in des['components']}
    lr = des['local_region']; cpl = lr['coupler']
    qa, qb = lr['qubits']
    chain_nb = sorted({q for e in be['edges'] if set(e) & {qa, qb} for q in e})   # Q1, Q4 and their neighbours
    xs = [comps[f'geom-Q{q}']['x_um'] for q in chain_nb]

    design = designs.DesignPlanar()
    design.overwrite_enabled = True
    design.chips.main.size['center_x'] = f'{(min(xs) + max(xs)) / 2}um'
    design.chips.main.size['size_x'] = f'{max(xs) - min(xs) + 1000}um'
    design.chips.main.size['size_y'] = '2mm'
    qubits = {}
    for q in chain_nb:
        c = comps[f'geom-Q{q}']
        qubits[q] = TransmonCross(design, f'Q{q}', options=dict(
            pos_x=f'{c["x_um"]}um', pos_y=f'{c["y_um"]}um', cross_length=f'{c["cross_length_um"]}um',
            cross_width=f'{c["cross_width_um"]}um', cross_gap=f'{c["gap_um"]}um'))
    ca, cb = comps[f'geom-Q{qa}'], comps[f'geom-Q{qb}']
    coupler = GapCoupler(design, cpl['id'].replace('geom-', ''), options=dict(
        pos_x=f'{(ca["x_um"] + cb["x_um"]) / 2}um', pos_y=f'{(ca["y_um"] + cb["y_um"]) / 2}um',
        length=f'{cpl["length_um"]}um', gap=f'{cpl["gap_um"]}um', width=f'{ca["cross_width_um"]}um',
        pocket=f'{ca["gap_um"]}um'))
    design.rebuild()

    # ---- rendered qgeometry (mm) ----
    poly = design.qgeometry.tables['poly']
    id2name = {v.id: k for k, v in design.components.items()}
    metal, etch = {}, {}
    for _, row in poly.iterrows():
        (etch if row['subtract'] else metal).setdefault(id2name[row['component']], []).append(row['geometry'])
    metal = {k: unary_union(v) for k, v in metal.items()}
    etch_all = unary_union([g for v in etch.values() for g in v])
    cname = coupler.name
    pads = [r['geometry'] for _, r in poly.iterrows() if id2name[r['component']] == cname and not r['subtract']]
    pad_owner = [[q for q in qubits if p.intersects(metal[f'Q{q}'])] for p in pads]
    pad_gap_um = pads[0].distance(pads[1]) * 1e3
    pads_in_etch = all(etch_all.buffer(1e-9).contains(p) for p in pads)

    # ---- consistency checks (true by construction where noted) ----
    order_x = sorted(qubits, key=lambda q: comps[f'geom-Q{q}']['x_um'])
    full_order = sorted((c for c in comps.values() if c['type'] == 'transmon_cross'), key=lambda c: c['x_um'])
    full_order = [int(c['id'].split('Q')[-1]) for c in full_order]
    chain_edges = {tuple(sorted(e)) for e in zip(full_order, full_order[1:])}
    be_edges = {tuple(sorted(e)) for e in be['edges']}
    ids_needed = set(reg.get('geometry_ids', [])) | set(loc.get('geometry_refs', []))
    ids_in_design = {c['id'] for c in des['components']} | {cpl['id']}

    # ---- electrostatics: Q1/Q4 islands (cross + own coupler pad) ----
    isl_um = []
    for q, p in zip((qa, qb), sorted(pads, key=lambda g: g.centroid.x)):
        g = unary_union([metal[f'Q{q}'], p])
        isl_um.append(shapely.affinity.scale(g, 1e3, 1e3, origin=(0, 0)))
    pockets_um = shapely.affinity.scale(etch_all, 1e3, 1e3, origin=(0, 0))
    bx = unary_union(isl_um).bounds
    caps = []
    for h in GRID_UM:
        t0 = time.time()
        Cm, N, info = capacitance(isl_um, pockets_um, h, ((bx[0] - 40, bx[2] + 40), (bx[1] - 40, bx[3] + 40)))
        caps.append({'grid_um': h, 'nodes': N, 'cg_info': info, 'Cc_fF': -Cm[0, 1], 'C1_total_fF': Cm[0, 0],
                     'C2_total_fF': Cm[1, 1], 'seconds': round(time.time() - t0, 1)})
    lj = [comps[f'geom-Q{q}']['Lj_nH'] for q in (qa, qb)]
    ej = [PHI0_RED ** 2 / (x * 1e-9) / H_PLANCK / 1e9 for x in lj]
    for c in caps:
        ec = [E_CHARGE ** 2 / (2 * C * 1e-15) / H_PLANCK / 1e9 for C in (c['C1_total_fF'], c['C2_total_fF'])]
        f = [transmon_f01_ghz(e, k) for e, k in zip(ej, ec)]
        c['EC_GHz'] = ec
        c['f01_GHz_with_design_EJ'] = f
        c['g_exchange_MHz'] = c['Cc_fF'] / (2 * math.sqrt(c['C1_total_fF'] * c['C2_total_fF'])) * math.sqrt(f[0] * f[1]) * 1e3
    gvals = [c['g_exchange_MHz'] for c in caps]
    ccs = [c['Cc_fF'] for c in caps]
    # what Cc would the proxies need, given the model-20 capacitances/frequencies
    ec_m = mod['parameters']['EC_GHz']; ej_m = mod['parameters']['EJ_GHz']
    Cm20 = [E_CHARGE ** 2 / (2 * e * 1e9 * H_PLANCK) * 1e15 for e in ec_m]
    f_m = [transmon_f01_ghz(a, b) for a, b in zip(ej_m, ec_m)]
    need = {f'{g}MHz': 2 * g * 1e-3 * math.sqrt(Cm20[0] * Cm20[1]) / math.sqrt(f_m[0] * f_m[1])
            for g in (mod['coupling_proxy_MHz'], lr['coupling_proxy_MHz'])}

    # ---- exports ----
    tbl = poly[['component', 'name', 'subtract', 'layer', 'chip']].copy()
    tbl['component'] = [id2name[c] for c in tbl['component']]
    tbl['bounds_mm'] = [[round(v, 6) for v in g.bounds] for g in poly['geometry']]
    tbl_json = tbl.to_json(orient='records', indent=1)
    (HERE / 'qgeometry_poly.json').write_text(tbl_json, encoding='utf-8')
    gds_ok = False
    try:
        import gdstk
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('D21_LOCAL_Q0_Q1_Q4_Q2_C14')
        chip = box(*[v * 1e3 for v in (min(xs) / 1e3 - 0.4, -0.5, max(xs) / 1e3 + 0.4, 0.5)])
        ground = chip.difference(shapely.affinity.scale(etch_all, 1e3, 1e3, origin=(0, 0)))
        for geom in [ground] + [shapely.affinity.scale(g, 1e3, 1e3, origin=(0, 0)) for g in metal.values()]:
            for pg in getattr(geom, 'geoms', [geom]):
                cell.add(gdstk.Polygon(list(pg.exterior.coords), layer=1))
                for hole in pg.interiors:
                    cell.add(gdstk.Polygon(list(hole.coords), layer=2))
        lib.write_gds(str(HERE / 'local_region.gds'))
        gds_ok = True
    except Exception as exc:  # gdstk optional
        print('GDS export skipped:', exc)

    fig, ax = plt.subplots(figsize=(9, 3))
    for pg in getattr(etch_all, 'geoms', [etch_all]):
        draw.mpl.render(pg, ax=ax, kw=dict(fc='#eeeeee', ec='#999999'))
    for name in (f'Q{x}' for x in order_x):
        draw.mpl.render(metal[name], ax=ax, kw=dict(fc='#4c72b0', alpha=0.8))
        c = metal[name].centroid
        ax.text(c.x, c.y - 0.25, name, ha='center')
    for p in pads:
        draw.mpl.render(p, ax=ax, kw=dict(fc='#dd8452'))
    b_ = etch_all.bounds
    ax.set_xlim(b_[0] - 0.1, b_[2] + 0.1); ax.set_ylim(-0.35, 0.25)
    ax.set_aspect('equal'); ax.set_xlabel('x (mm)'); ax.set_ylabel('y (mm)')
    ax.set_title(f'{des["revision"]} local region: coupler {cpl["id"]} (orange) in its own pocket (grey = etch)')
    fig.tight_layout(); fig.savefig(HERE / 'local_region.png', dpi=150)

    R = {
        'versions': {'python': platform.python_version(), 'quantum_metal': qiskit_metal.__version__,
                     'numpy': np.__version__, 'scipy': scipy.__version__, 'shapely': shapely.__version__,
                     'gdstk': __import__('gdstk').__version__ if gds_ok else None},
        'scope': {'qubits_built': order_x, 'reason': f'local subsystem {loc.get("qubits")} + chain neighbours'},
        'design_revision': des['revision'],
        'consistency_checks': {
            'note': 'Ordering/adjacency checks are consistency checks of the supplied positions against the backend; '
                    'with qubits on one line at equal pitch they reduce to sorting by x, and the coupler bridges Q1/Q4 '
                    'by construction of our placement. They are not independent evidence of physical coupling.',
            'geometry_ids_required_missing_from_design': sorted(ids_needed - ids_in_design),
            'physical_order_by_x_full_device': full_order,
            'x_order_chain_equals_backend_edges': chain_edges == be_edges,
            'coupler_pad_owners': pad_owner, 'coupler_gap_rendered_um': pad_gap_um,
            'coupler_pads_inside_etched_pocket': pads_in_etch,
            'other_couplers_in_design': 'none: local_region only specifies geom-C14; other 30 edges have no coupler geometry',
        },
        'junctions': {'design21_Lj_nH': lj, 'design21_EJ_GHz': ej, 'model20_EJ_GHz': ej_m},
        'electrostatics': {
            'method': 'finite-difference Laplace on the Metal-rendered Q1/Q4 islands; zero-thickness metal at z=0; '
                      f'silicon eps_r={EPS_SI} below, vacuum above; ground plane outside etched pockets and on the outer '
                      'box; neighbours Q0/Q2 omitted (their pockets are not included, i.e. treated as ground)',
            'runs': caps,
            'Cc_fF_range': [min(ccs), max(ccs)],
            'g_exchange_MHz_range': [min(gvals), max(gvals)],
            'Cc_needed_fF_for_proxy_given_model20_C_and_f01': need,
            'interpretation': 'Order-of-magnitude only: thin-sheet edge singularities converge slowly and the grid '
                              'cannot place all 20/18 um edges on nodes (non-monotone convergence); the coupler pad shape '
                              'is our assumption; the supplied proxy "includes drive scaling". The estimate cannot '
                              'discriminate 16 from 18 MHz (11%).',
        },
        'fingerprint_note': 'design geometry_fingerprint is an opaque registry id ("not a digest of this JSON"); the '
                            'digest below is of the exported rendered qgeometry table and identifies this rebuild only.',
        'rendered_qgeometry_sha256': hashlib.sha256(tbl_json.encode()).hexdigest(),
        'design_geometry_fingerprint': des['geometry_fingerprint'],
        'gds_exported': gds_ok,
        'runtime_s': round(time.time() - t_start, 1),
    }
    (HERE / 'results.json').write_text(json.dumps(R, indent=2) + '\n', encoding='utf-8')
    print(f"built {order_x} + {cpl['id']}; pads owned by {pad_owner}; pads in pocket: {pads_in_etch}; "
          f"x-order chain == backend edges: {chain_edges == be_edges}")
    for c in caps:
        print(f"h={c['grid_um']} um: Cc={c['Cc_fF']:.3f} fF, C1={c['C1_total_fF']:.1f} fF, C2={c['C2_total_fF']:.1f} fF, "
              f"EC={c['EC_GHz'][0]:.3f} GHz, g={c['g_exchange_MHz']:.1f} MHz ({c['seconds']} s)")
    print(f"Cc needed for proxy (model-20 C, f01): {need}; GDS: {gds_ok}; runtime {R['runtime_s']} s",
          file=sys.stdout)


if __name__ == '__main__':
    main()
