# B3 — QuTiP local control-dynamics validation

Optional bonus: up to +10 points, separate from the 200-point core challenge.
Include source code, assumptions, reproduction command, results and comparison
against supplied challenge evidence. Do not alter solve.py or outputs/result.json
for bonus work. Submit this directory separately for organizer review.

---

## Contents

| File | Purpose |
|---|---|
| `validate_dynamics.py` | Source: QuTiP / qutip-qip simulation of the Stage-2 local subsystem (≤ 4 qubits) |
| `results.json` | All results, with package versions |

## Reproduction

From the repository root, with `requirements-advanced.txt` installed. Tested with Python 3.13.18,
QuTiP 5.3.1, qutip-qip 0.4.2 and numpy 2.5.4; versions are written to `results.json`.

```sh
python extensions/qutip_validation/validate_dynamics.py
```

It takes about 3 s and is deterministic. Artifacts are only read.

## Scope: what is and is not independent

- **Not independent.** Under the supplied reduced model the pulse Hamiltonian is H = ωZZ/2, which commutes
  with itself. QuTiP's integration therefore returns cos(nωt), the same closed form that `characterization.json`
  says it was generated from ("deterministic analytic synthetic probes, not QuTiP output"). Agreement on the
  probes checks the numbers and the assumptions; it is **not an independent physical derivation of g**.
- **What B3 adds:**
  - a fit window and the full set of alias solutions;
  - the effect of the supplied T1/T2;
  - the **actual compiled gate sequence** around the target, simulated with the pulse in place of the ideal gate;
  - a verification-subsystem check that actually compares with and without a neighbouring qubit;
  - a repair check with sensitivity and pass criteria.

## Assumptions

1. **Reduced model (supplied).** During the calibrated rectangular pulse of t = 15.625 ns,
   H/ħ = ωZZ/2 with ω = 2π·g/1000 rad/ns. The pulse propagator is integrated with `sesolve`/`mesolve`
   (atol 1e-12).
2. **Probes.** As specified in `characterization.json`: prepare |++⟩, apply n back-to-back pulses, measure XI.
3. **Decoherence (optional run).** Amplitude damping plus pure dephasing at rate γφ = 1/T2 − 1/(2T1),
   with collapse operator √(γφ/2)·Z. Q1/Q4 use T1 = 100 µs and T2 = 80 µs from `noise_snapshot.json`.
4. **Compiled block.**
   - From `compiled_circuit.qpy`: the target RZZ (instruction 694) plus the single-qubit gates on Q1 and Q4
     that run up to the nearest two-qubit gate involving another qubit.
   - Built as a qutip-qip `QubitCircuit` with gates SQRTNOT (= SX), RZ, a custom RZZ, and a custom PULSE gate
     that inserts the integrated propagator.
5. **Verification subsystem.** Built from the record: the qubits on edges incident to the measured Q1 (0, 1, 4),
   plus one non-incident neighbour (Q2) to measure its effect.

## Results

### 1. Characterization: fit, aliases and decoherence

| | max abs residual over 8 probes (tolerance 0.004) |
|---|---:|
| g = 16 MHz (model-20, what the calibration assumed) | 1.001 ✗ |
| g = 18 MHz (design-21) | 0.0010 ✓ |
| g = 18 MHz with T1/T2 | 0.0029; largest change from the coherent result **0.0019** |

- **Fit.** Minimax g = **18.0000 MHz**. The window within tolerance is **[17.9965, 18.0055] MHz**, which gives a
  realized angle of 1.7667–1.7676 rad per pulse (9π/16 = 1.7671).
- **Aliases.** Integer-n probes cannot tell g from k·64 ± g MHz (64 MHz = 1000/t). Between 0 and 192 MHz the
  probes fit equally well at **18, 46, 82, 110, 146 and 174 MHz**. We take 18 MHz because it is the branch
  nearest the supplied proxies. The others are excluded only as physically implausible, at 2.5× the proxies
  or more.
- **Decoherence.** It can shift a probe by at most 0.0019, far below the 0.2–1.0 differences between g = 16 and
  g = 18.

### 2. The actual compiled local block (compiled instructions 547–551, 689–699 and 716–720)

The block is 21 gates (`SQRTNOT…RZ…RZZ…`), taken from `compiled_circuit.qpy`.

| target in block | process fidelity vs intended block |
|---|---:|
| integrated pulse at the model-20 rate (16 MHz) | 1.000000 |
| integrated pulse at the D21 rate (18 MHz) | **0.990393** |
| analytic cos²(over-rotation / 2) | 0.990393 |

The 9π/16 over-rotation costs about 1% process infidelity per occurrence in the real compiled context. The
surrounding SX/RZ gates neither cancel nor amplify it.

We also checked that SX†⊗SX† · RZZ(θ) · SX⊗SX equals exp(−iθYY/2), and that it commutes with ZZ parity. This
explains why the workload's parity observables cannot see the fault. The identity holds for either SX
orientation, so it does **not** check gate order; the compiled-block simulation above does.

### 3. Verification companion ⟨X_Q1⟩

| subsystem | g = 16 MHz | g = 18 MHz | observed |
|---|---:|---:|---:|
| {0, 1, 4} (edges incident to Q1) | 0.000000 | −0.193199 | −0.193199 (tolerance 0.004) |
| {0, 1, 2, 4} (adds non-incident neighbour Q2) | — | −0.193199 (difference 9×10⁻¹³) | |

The intended π/2 rotation misses the observation by **48× the tolerance**. Adding Q2 has no effect, as the
analytic identity predicts.

### 4. Repair validation with sensitivity

The repair is a corrected pulse of **13.8889 ns** at the D21 rate (π/2 / 0.113097 rad/ns).

- **At the fitted g,** ⟨XI⟩ = [0, −1, 0, 1, 0, −1, 0, 1]. The compiled-block fidelity is 1.000000, and the
  verification ⟨X_Q1⟩ is −3×10⁻¹¹.
- **Across the whole fit window** [17.9965, 18.0055] MHz, the residual angle error is at most 4.8×10⁻⁴ rad. The
  worst probe deviation is **0.0034, inside the 0.004 tolerance**. The repair is robust to the remaining
  uncertainty in g, but only just: the margin is 0.0006.
- **Pass criteria, per probe:** ⟨XI⟩(n) = cos(nπ/2) ± 0.004.
  - The clearest fault signatures are n = 4 (the fault gives 0.707, the repair 1.000) and n = 8 (the fault gives
    0.000, the repair 1.000).
  - Odd n must read 0 ± 0.004; the fault gives −0.195, 0.556, −0.831 and 0.981.
- **Amplitude alternative.** Keeping 15.625 ns and scaling the drive by 0.8889 gives ωt = π/2 by the same
  algebra, so it is identical to the 16 MHz case and is not simulated separately.

## Comparison with the supplied evidence

| Supplied evidence | Result | Independence |
|---|---|---|
| `characterization.json` probes | Reproduced at g = 18 MHz within 0.001; g = 16 MHz excluded | Same closed form, so a consistency check |
| `observable_spec.json` verification-X1 = −0.193199 | Reproduced at g = 18 MHz; intended is off by 48× tolerance | Consistent with the analytic identity in `verification_protocol.json` |
| `compiled_circuit.qpy` around instruction 694 | The real compiled block loses 0.96% process fidelity with the D21 pulse | New: uses the actual compiled gates |
| `device_model.json` "SX basis changes realize YY pair blocks" | Confirmed numerically; explains parity blindness | Does not test gate order |
| `noise_snapshot.json` T1/T2 | At most 0.0019 effect | Rules out decoherence as the explanation |
| Stage-2 repair (13.889 ns) | Passes all probes over the full fit window (worst 0.0034 against tolerance 0.004) | New sensitivity bound |
