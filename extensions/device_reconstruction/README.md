# B1 — Quantum Metal device/geometry reconstruction

Optional bonus: up to +10 points, separate from the 200-point core challenge.
Include source code, assumptions, reproduction command, results and comparison
against supplied challenge evidence. Do not alter solve.py or outputs/result.json
for bonus work. Submit this directory separately for organizer review.

---

## Contents

| File | Purpose |
|---|---|
| `reconstruct_geometry.py` | Source: Quantum Metal DesignPlanar rebuild of the D21 device, with geometric checks |
| `results.json` | Check results, junction cross-check and recipe digest |
| `qgeometry_poly.json` | Exported Metal qgeometry polygon table (component, name, subtract, layer, bounds in mm) |
| `local_region.png` | Rendered Q0–Q1–Q4–Q2–Q3 region; the coupler geom-C14 is orange |

## Reproduction

From the repository root, headless, with `requirements-advanced.txt` installed
(tested with quantum-metal 0.9.0, import name `qiskit_metal`, Python 3.13):

```sh
QT_QPA_PLATFORM=offscreen python extensions/device_reconstruction/reconstruct_geometry.py
```

Runtime is about 3 s. No GUI and no EM renderer are needed. Artifacts are only read.

## Assumptions

1. **Qubits.** Each `transmon_cross` component in `device_design.json` becomes a Metal `TransmonCross`.
   - Position: `x_um`, `y_um`.
   - Dimensions: `cross_length` = `cross_length_um` (arm length from the centre), `cross_width` = `cross_width_um`,
     `cross_gap` = `gap_um`.
   - Lj/Cj are kept as metadata; no junction geometry is drawn.
2. **Coupler `geom-C14`.** It is given only as "capacitive, gap 18 µm, length 200 µm" in `local_region`, so it is
   modelled as a series gap capacitor:
   - Two pads, as wide as the cross arms, with a total length of 200 µm and an 18 µm gap between them.
   - Centred on the Q1–Q4 midpoint and placed along the line joining the two qubits.
   - With this placement each pad overlaps one facing cross arm, so the only series element between Q1 and Q4
     is the 18 µm gap. This is our interpretation; the supplied geometry does not specify the pad shape.
3. **No coupling extraction.** No EM solver is available here: Ansys/pyEPR and gmsh+Elmer are not installed.
   The coupling capacitance, and hence g, is therefore **not** re-derived. The supplied `coupling_proxy_MHz` is
   treated as authoritative, as the challenge allows.
4. **Fingerprint.** `geometry_fingerprint` is documented as "an opaque registry identifier, not a digest of this
   JSON". A digest of our reconstruction recipe is reported for traceability; it is not expected to equal the
   registry ID.

## Results

| Check | Result |
|---|---|
| Components built | 32 TransmonCross qubits + the geom-C14 coupler (33 total) |
| `provenance_registry.geometry_ids` ∪ `local_subsystem.geometry_refs` present in the rebuild | all 33 present, none missing |
| Physical order of qubits along x | 0-1-**4**-2-3-5-6-…-31 |
| Nearest-neighbour graph from rendered arm-tip separations (140.0 µm) | **identical to the 31 `backend_snapshot.edges`** |
| Coupler connectivity (shapely intersection with rendered qubit metal) | pads owned by Q1 and Q4 respectively; touches no other qubit |
| Rendered coupler gap | 18.0 µm (matches supplied) |
| Coupler bridges exactly the target edge `selected_edge` [1, 4] | yes |
| D21 junction Lj = 10 nH → EJ | 16.35 GHz for both Q1 and Q4 |
| model-20 EJ | 20.1 / 19.8 GHz → **inconsistent with D21 junctions** |

## Comparison with the supplied evidence

- **Topology.** The D21 geometry places Q4 between Q1 and Q2. That reproduces the unusual backend ordering
  (edges [1,4] and [4,2]) and confirms the target edge (1,4) is a physically adjacent pair joined by the
  dedicated coupler geom-C14. This supports the Stage-2 finding that mapping, runtime and physical edge are
  correct (M3).
- **Device-design provenance (M4).** Every geometry ID in the registry and in the local subsystem is accounted
  for by `design-21` (D21). The D21 design is self-consistent and is the design the runtime used.
- **Device-model provenance (M5).** The rebuilt D21 junction metadata gives EJ = 16.35 GHz, while
  `device_model.json` (model-20) uses 20.1/19.8 GHz. Independently of the coupling proxy, model-20's parameters
  do not come from this geometry, consistent with its D20 `source_geometry_fingerprint`. B2 covers this in more
  detail.
- **Coupling.** The D21 coupler geometry (18 µm gap, 200 µm) is what `design-21` associates with g = 18 MHz.
  Without an EM extraction we cannot compute g from geometry. A follow-up with Ansys Q3D or gmsh+Elmer
  (`quantum-metal[ansys]` or `[mesh]`) on this exact Metal design would give the Q1–Q4 coupling capacitance
  for comparison with the 18 MHz proxy and the 18.000 MHz fitted from characterization.
