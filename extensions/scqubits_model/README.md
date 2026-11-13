# B2 — scqubits reduced device-model reconstruction

Optional bonus: up to +10 points, separate from the 200-point core challenge.
Include source code, assumptions, reproduction command, results and comparison
against supplied challenge evidence. Do not alter solve.py or outputs/result.json
for bonus work. Submit this directory separately for organizer review.

---

## Contents

| File | Purpose |
|---|---|
| `reconstruct_model.py` | Source: scqubits reconstruction of model-20 and a D21 cross-check |
| `results.json` | All numerical results, including a proposed D21-consistent model record |

## Reproduction

From the repository root, with `requirements-advanced.txt` installed (tested with scqubits 4.3.1, Python 3.13):

```sh
python extensions/scqubits_model/reconstruct_model.py
```

Runtime is about 2 s. Artifacts are only read.

## Assumptions

1. **Transmons.** Two `scqubits.Transmon` objects are built from `device_model.json` `parameters`:
   EJ = [20.1, 19.8] GHz, EC = [0.25, 0.26] GHz, ng = 0, ncut = 20, truncated_dim = 3.
   Convergence: raising ncut to 40 changes f01 by 1×10⁻¹⁰ MHz.
2. **Coupling.** The pair is coupled by a charge-charge term G·n₁n₂. G is chosen so that the bare exchange
   strength G·|⟨0|n|1⟩₁|·|⟨0|n|1⟩₂| equals the supplied `coupling_proxy_MHz`.
   - The coupled Hamiltonian is built in the scqubits eigenbasis, from scqubits eigenvalues and n matrix elements.
   - scqubits' own `HilbertSpace` is not used: scqubits 4.3.1 rejects SciPy ≥ 1.15 sparse arrays in
     `identity_wrap`, so the same construction is done explicitly.
   - Results are checked with 3 and 6 levels per transmon.
3. **Entangling rate.** The effective rate ω = 2π·g/1000 rad/ns is the **supplied** reduced assumption, and the
   model says "Proxy g includes drive scaling". The driven entangling rate is therefore taken from that
   assumption, not derived from the static Hamiltonian. The static ZZ below is reported for context only.
4. **Physical constants.** EJ is converted from the D21 junction inductance with EJ = (Φ₀/2π)²/Lj (SI CODATA).
   EC sets the total island capacitance, C = e²/(2h·EC).

## Results

### Single transmons (model-20)

| | f01 (GHz) | anharmonicity (MHz) | ⟨0\|n\|1⟩ | EJ/EC |
|---|---:|---:|---:|---:|
| Q1 | 6.0793 | −277.3 | 1.2326 | 80.4 |
| Q4 | 6.1456 | −289.4 | 1.2153 | 76.2 |

Both are in the transmon regime (EJ/EC > 50), and the bare detuning is 66.3 MHz.

### Coupled pair

| coupling | dressed 01/10 splitting (MHz) | g recovered from splitting (MHz) | static ZZ (kHz) |
|---|---:|---:|---:|
| model-20, 16 MHz | 73.645 | 16.000 | 3561 |
| design-21, 18 MHz (3 levels) | 75.469 | 18.000 | 4492 |
| design-21, 18 MHz (6 levels) | 75.469 | 18.000 | 4490 |

The exchange strength recovered from the dressed spectrum matches the proxy it was set to. This confirms the
scqubits charge-basis model and the proxy definition agree; it is not an independent extraction of g.

### Reduced-rate consequence

| | ω (rad/ns) | angle for a 15.625 ns pulse |
|---|---:|---:|
| model-20, 16 MHz (`cal-21` nominal_rate = 0.100531, matches exactly) | 0.100531 | π/2 (intended) |
| design-21, 18 MHz (D21 device) | 0.113097 | **1.767146 rad = 9π/16** |

### New provenance finding: model-20 junction energies are not D21's

| | Q1 | Q4 |
|---|---:|---:|
| `design-21` Lj (nH) | 10.0 | 10.0 |
| EJ implied by D21 Lj (GHz) | **16.35** | **16.35** |
| `model-20` EJ (GHz) | 20.1 | 19.8 |
| Lj implied by model-20 EJ (nH) | 8.13 | 8.26 |
| f01 if EJ were taken from D21 Lj, EC unchanged (GHz) | 5.455 | 5.558 |

The coupling proxy is not the only thing that differs. The model-20 junction energies are also inconsistent
with the D21 junction metadata: D21 specifies identical 10 nH junctions, while model-20 has asymmetric EJ that
implies about 8.1–8.3 nH. This independently supports the conclusion that model-20 was not derived from the D21
geometry, matching its D20 `source_geometry_fingerprint`.

Caveats:
- The D20 geometry is not supplied, so we cannot confirm model-20 matches D20 junctions.
- The EC values imply total island capacitances of 77.5 / 74.5 fF. Only the junction Cj (2 fF) is supplied,
  not the cross/gap capacitance, so EC is not cross-checked.

### Proposed replacement record

`results.json` → `proposed_model_record` is a model-21 candidate.
- **Changed:** `source_geometry_fingerprint` is set to the D21 fingerprint and `coupling_proxy_MHz` to 18.0.
- **Kept, flagged:** EJ/EC are retained from model-20 until a D21 EM capacitance extraction and junction
  characterization exist. Only the coupling enters the reduced entangling rate, so this minimal update is
  enough to correct the calibration.

## Comparison with the supplied evidence

| Supplied evidence | Agreement |
|---|---|
| `device_model.json` (model-20) parameters | Reconstructed; well-formed converged transmons, 66 MHz detuned |
| `control_calibration.json` nominal_rate 0.100531 rad/ns | Equals ω(16 MHz) from model-20 exactly |
| `device_design.json` coupling_proxy_MHz 18 | Gives ω = 0.113097 rad/ns and a 9π/16 realized angle, matching `characterization.json` (see B3) |
| `device_design.json` Lj = 10 nH | Inconsistent with model-20 EJ (16.35 vs 20.1/19.8 GHz): more evidence model-20 is not D21-derived |
| `provenance_registry.json` (model-20 → D20 fingerprint) | Consistent with the junction mismatch above |
