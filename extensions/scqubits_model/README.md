# B2 — scqubits reduced device-model reconstruction

Optional bonus: up to +10 points, separate from the 200-point core challenge.
Include source code, assumptions, reproduction command, results and comparison
against supplied challenge evidence. Do not alter solve.py or outputs/result.json
for bonus work. Submit this directory separately for organizer review.

---

## Contents

| File | Purpose |
|---|---|
| `reconstruct_model.py` | Source: scqubits reconstruction of model-20, mechanism test, D21 cross-checks |
| `results.json` | All results, with package versions and a proposed D21-consistent model record |

## Reproduction

From the repository root, with `requirements-advanced.txt` installed. Tested with Python 3.13.18,
scqubits 4.3.1, numpy 2.5.4 and scipy 1.18.1; versions are written to `results.json`.

```sh
python extensions/scqubits_model/reconstruct_model.py
```

It takes about 3 s and is deterministic. If `extensions/device_reconstruction/results.json` exists (run B1
first), its electrostatic EC range is added as a cross-check. Artifacts are only read.

## Assumptions

1. **Transmons.** Two `scqubits.Transmon` objects use the `device_model.json` parameters: EJ = [20.1, 19.8] GHz,
   EC = [0.25, 0.26] GHz, ng = 0, ncut = 20, truncated_dim = 3.
2. **Coupling.** The pair is coupled by a charge-charge term G·n₁n₂. G is chosen so that the bare exchange
   strength G·|⟨0|n|1⟩|² equals the supplied `coupling_proxy_MHz`. The coupled spectrum comes from
   `scqubits.HilbertSpace`, and is checked against an explicit construction in the eigenbasis with 3 and 6
   levels per transmon.
   - scqubits 4.3.1 cannot build a HilbertSpace under QuTiP 5, which returns SciPy sparse *arrays*.
   - We apply Quantum Metal's shipped fix, `qiskit_metal.analyses.quantization._scqubits_compat.apply()`.
3. **Entangling rate.** The rate ω = 2π·g/1000 rad/ns is the **supplied** reduced assumption, and the model
   says "Proxy g includes drive scaling". Identifying the proxy with a static exchange coupling is our
   modelling choice. The mechanism test below checks which rate law the characterization data supports.
4. **Physical constants.** EJ = (Φ₀/2π)²/Lj and C = e²/(2h·EC), using SI CODATA constants.

## Results

### Single transmons (model-20)

| | f01 (GHz) | anharmonicity (MHz) | ⟨0\|n\|1⟩ | EJ/EC |
|---|---:|---:|---:|---:|
| Q1 | 6.0793 | −277.3 | 1.2326 | 80.4 |
| Q4 | 6.1456 | −289.4 | 1.2153 | 76.2 |

- Raising ncut from 20 to 40 changes f01 by 1×10⁻¹⁰ MHz.
- The bare detuning is 66.3 MHz.

### Coupled pair

| proxy g | HilbertSpace 01/10 splitting (MHz) | static ZZ: HilbertSpace / explicit 3-level / explicit 6-level (MHz) | 2nd-order perturbative ZZ (MHz) | g/Δ |
|---|---:|---:|---:|---:|
| 16 MHz (model-20) | 73.645 | 3.5613 / 3.5613 / 3.5603 | 3.87 | 0.24 |
| 18 MHz (design-21) | 75.469 | 4.4916 / 4.4916 / 4.4902 | 4.89 | 0.27 |

- `HilbertSpace` and the explicit construction agree to 10⁻⁴ MHz.
- The dressed 01, 10 and 11 states each overlap their bare state by at least 0.94, so labelling them is unambiguous.
- The splitting matches √(Δ² + 4g²) **by construction**: G was set from the proxy. This is a consistency check,
  not an extraction of g.
- The static ZZ is **several MHz** because g/Δ ≈ 0.25 is large for a fixed coupling. Perturbation theory
  overestimates it by about 9% at this ratio.
  - This always-on ZZ is outside the supplied reduced model, which describes only the driven entangling pulse.
  - It is reported, not used.

### Mechanism test: which rate law matches `characterization.json`?

| hypothesis for the 15.625 ns pulse at g = 18 MHz | angle per pulse (rad) | max residual over 8 probes (tolerance 0.004) |
|---|---:|---:|
| supplied linear proxy ω = 2π·g/1000 | 1.7671 (9π/16) | **0.0010 ✓** |
| rate ∝ g² (static-ZZ-like scaling from the 16 MHz calibration) | 1.9880 | 1.187 ✗ (⟨XI⟩ at n=1 would be −0.405 against the observed −0.194) |
| bare static ZZ alone (4.49 MHz) | 0.2205 | 1.829 ✗ |

A sweep of g from 14 to 20 MHz in 0.5 MHz steps fits within tolerance **only at 18.0 MHz** (residual 0.001). The
nearest other steps, 17.5 and 18.5 MHz, have residuals of 0.38. The data therefore support the supplied linear
proxy with g = 18 MHz, and rule out the 16 MHz value used by the calibration.

### Reduced-rate consequence

| | ω (rad/ns) | angle for a 15.625 ns pulse |
|---|---:|---:|
| model-20, 16 MHz (`cal-21` nominal_rate 0.100531, matches exactly) | 0.100531 | π/2 (intended) |
| design-21, 18 MHz | 0.113097 | **1.767146 rad = 9π/16** |

### Design cross-checks

| | Q1 | Q4 |
|---|---:|---:|
| `design-21` Lj (nH) | 10.0 | 10.0 |
| EJ implied by D21 Lj (GHz) | 16.35 | 16.35 |
| `model-20` EJ (GHz) | 20.1 | 19.8 |
| Lj implied by model-20 EJ (nH) | 8.13 | 8.26 |
| model-20 EC → total C (fF) | 77.5 | 74.5 |
| B1 electrostatic EC (GHz), if B1 has run | 0.19–0.25 | 0.19–0.25 |

- **EC is consistent:** the B1 electrostatic range overlaps model-20's 0.25 / 0.26 GHz.
- **EJ is not consistent:** model-20's junction energies don't match the D21 Lj.
- This is *consistent with* model-20 not being derived from D21, but it is not proof:
  - model-20 calls itself "not a claimed ab initio transmon extraction";
  - the D20 geometry is not supplied;
  - no frequency data exist to test either EJ.

### Proposed replacement record

`results.json` → `proposed_model_record` (model-21 candidate):
- **Changed:** `source_geometry_fingerprint` is set to D21 and `coupling_proxy_MHz` to 18.0.
- **Kept, flagged:** EJ/EC are retained from model-20 pending D21 junction characterization and an EM extraction.
- **Left blank:** `created_at` is null until the record is registered.

Only the coupling enters the reduced rate, so this minimal update is enough to correct the calibration.

## Comparison with the supplied evidence

| Supplied evidence | Agreement |
|---|---|
| `device_model.json` (model-20) | Reconstructed; converged transmons, 66 MHz detuned |
| `control_calibration.json` nominal_rate 0.100531 rad/ns | Equals ω(16 MHz) from model-20 exactly |
| `characterization.json` | Fits only the linear proxy at g = 18 MHz (the `design-21` value); rejects 16 MHz, g² scaling and static ZZ |
| `device_design.json` Lj = 10 nH | Inconsistent with model-20 EJ (16.35 vs 20.1 / 19.8 GHz); consistent with, not proof of, non-D21 provenance |
| B1 electrostatics | Consistent with model-20 EC |
