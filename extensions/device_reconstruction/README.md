# B1 — Quantum Metal device/geometry reconstruction

Optional bonus: up to +10 points, separate from the 200-point core challenge.
Include source code, assumptions, reproduction command, results and comparison
against supplied challenge evidence. Do not alter solve.py or outputs/result.json
for bonus work. Submit this directory separately for organizer review.

---

## Contents

| File | Purpose |
|---|---|
| `reconstruct_geometry.py` | Source: Quantum Metal rebuild of the local region, consistency checks, finite-difference electrostatics |
| `results.json` | All results, with package versions and runtime |
| `qgeometry_poly.json` | Exported Metal qgeometry polygon table (bounds in mm) |
| `local_region.gds` | GDS (gdstk) of the local region: layer 1 metal (ground plane and islands), layer 2 pocket outlines |
| `local_region.png` | Plot: Q0–Q1–Q4–Q2, coupler geom-C14 (orange), etched pockets (grey) |

## Reproduction

From the repository root, headless, with `requirements-advanced.txt` installed. Tested with Python 3.13.18,
quantum-metal 0.9.0 (import name `qiskit_metal`), numpy 2.5.4, scipy 1.18.1, shapely 2.2.0 and gdstk 1.0.2.
Exact versions are written to `results.json`.

```sh
QT_QPA_PLATFORM=offscreen python extensions/device_reconstruction/reconstruct_geometry.py
```

It takes about 60 s, mostly the four electrostatic solves, and peaks at about 2 GB of RAM. The output is
deterministic: no random numbers are used and CG runs to a fixed tolerance. Artifacts are only read.

## Scope and assumptions

1. **Scope.** The rules say to limit the work to the supplied local subsystem: Q1, Q4 and geom-C14
   (`local_subsystem.json`). Their chain neighbours Q0 and Q2 are built for context only. The chip is centred
   on this region.
2. **Qubits.** Each qubit is a Metal `TransmonCross` built from `device_design.json`:
   - `cross_length` is the arm length measured from the centre. This was checked against the Metal source,
     which draws arms from −L to +L.
   - `cross_width`, plus `cross_gap` for the etched pocket.
   - Lj/Cj are kept as metadata; the junction itself is not drawn.
3. **Coupler geom-C14.** It is specified only as "capacitive, gap 18 µm, length 200 µm". We model it as two
   pads as wide as the arms, 200 µm long in total, with an 18 µm gap.
   - It is centred between Q1 and Q4.
   - It sits in its own etched pocket (20 µm margin, as for the qubits), so it is isolated from the ground plane.
   - Each pad overlaps the facing cross arm, so pad and qubit form one island.
   - The pad shape is our interpretation; the record does not specify it.
4. **Electrostatics.** No commercial EM solver is available (Ansys/pyEPR, gmsh+Elmer), so we wrote a 3-D
   finite-difference Laplace solver.
   - It uses the Metal-rendered polygons: two islands (Q1 with its pad, Q4 with its pad).
   - Metal is a zero-thickness sheet. Silicon (εr = 11.45) is below it and vacuum above.
   - The sheet outside the etched pockets is grounded, as is the outer box.
   - Q0 and Q2 are not separate conductors: their area is treated as ground.
   - The grid is non-uniform: spacing h near the metal, growing geometrically away from it.
   - The CG solve uses a Jacobi preconditioner to relative residual 1e-9.
   - Converting to coupling: EC = e²/(2C), f01 = √(8·EJ·EC) − EC with EJ from the design Lj, and
     g = Cc·√(f1·f2) / (2·√(C1·C2)).
5. **Fingerprint.** `geometry_fingerprint` is documented as an opaque registry ID, not a digest of the JSON.
   We report the SHA-256 of our rendered qgeometry table to identify this rebuild only.

## Results

### Consistency checks

All qubits sit on one line at equal pitch, so the ordering checks reduce to sorting by x. The coupler bridges
Q1/Q4 because of how we placed it. These rows check that the records agree with each other; they are not
independent evidence of physical coupling.

| Check | Result |
|---|---|
| Geometry IDs in `provenance_registry` / `local_subsystem` missing from `design-21` | none |
| Physical order along x (full device) | 0-1-**4**-2-3-5-…-31 |
| Chain implied by the x order equals the 31 `backend_snapshot.edges` | yes |
| Coupler pads owned by | Q1 and Q4, one each |
| Coupler pads inside their etched pocket (isolated from ground) | yes |
| Rendered coupler gap | 18.0 µm |
| Couplers for the other 30 edges | none specified in `design-21` |
| D21 junction Lj = 10 nH → EJ | 16.35 GHz (model-20 uses 20.1 / 19.8 GHz) |

### Independent coupling estimate (finite-difference electrostatics)

| grid h (µm) | nodes | Cc (fF) | C1 / C2 (fF) | EC (GHz) | f01 with D21 EJ (GHz) | g (MHz) |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 261 625 | 0.693 | 77.9 / 77.9 | 0.249 | 5.45 | 24.3 |
| 6 | 695 604 | 0.973 | 95.5 / 97.2 | 0.203 / 0.199 | 4.95 / 4.91 | 24.9 |
| 5 | 1 013 725 | 0.883 | 91.7 / 91.7 | 0.211 | 5.05 | 24.3 |
| 4 | 1 577 928 | 1.184 | 100.6 / 100.6 | 0.193 | 4.83 | 28.4 |

For comparison, the Cc the proxies would need, using model-20's capacitances and frequencies, is
**0.40 fF for 16 MHz** and **0.45 fF for 18 MHz**.

- **Convergence.** It is not monotone: the grid cannot put all the 20 µm and 18 µm edges on nodes, and
  thin-sheet edge fields converge slowly. A 3 µm grid ran out of memory. Report this as a range:
  **Cc ≈ 0.7–1.2 fF, g ≈ 24–28 MHz**.
- **Interpretation.** This is order-of-magnitude agreement with the 16–18 MHz proxies: the same order, roughly
  1.4–1.6× higher.
  - The pad shape is assumed.
  - The neighbours and the full ground layout are simplified.
  - The supplied proxy "includes drive scaling".

  So this estimate **cannot tell 16 from 18 MHz** (an 11% difference), and it is not used to support that choice.
- **Capacitance and EC.** Total island capacitance comes out at about 78–101 fF, giving EC ≈ 0.19–0.25 GHz.
  That is consistent with model-20's EC of 0.25 / 0.26 GHz, which implies 77.5 / 74.5 fF. B2 uses this as
  an EC cross-check.

## Comparison with the supplied evidence

| Supplied evidence | What B1 shows |
|---|---|
| `backend_snapshot.edges` (0-1-4-2-3 chain), `selected_edge` [1,4] | Agrees with the D21 positions; Q1 and Q4 are adjacent and are the only pair with a specified coupler |
| `provenance_registry.geometry_ids`, `local_subsystem.geometry_refs` | All present in `design-21` |
| `design-21` coupling_proxy_MHz = 18 | Same order of magnitude as the electrostatic estimate (24–28 MHz); the method cannot resolve 16 vs 18 |
| `device_model.json` EC = 0.25 / 0.26 GHz | Consistent with the electrostatic EC of 0.19–0.25 GHz |
| `device_model.json` EJ = 20.1 / 19.8 GHz | Does not match the D21 Lj = 10 nH (16.35 GHz). This is consistent with model-20 not being derived from D21, but the D20 geometry is not supplied |

A proper EM extraction would tighten these numbers: Ansys Q3D or gmsh+Elmer via `quantum-metal[ansys]` or
`quantum-metal[mesh]`, run on this exact Metal design.
