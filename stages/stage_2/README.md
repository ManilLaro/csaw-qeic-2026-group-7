# Stage 2 — Cross-Layer Execution Forensics

**100 points**

## Objective

Investigate consistency across the intended circuit, compilation, mapping,
simulated runtime/backend information, device design, device model, control
calibration, supplied characterization and selected observables. Support your
verdict and localization with a cross-layer chain of evidence rather than
measurement fitting alone.

No physical quantum hardware is required. All execution is simulation-based.
The supplied frozen artifacts are authoritative challenge evidence. You may
use geometry metadata, coupling proxies, the reduced-model assumption and
characterization directly without reproducing those quantities numerically.

## Workload and verification protocol

The global workload uses 32 simulated qubits with sparse local connectivity,
continuously parameterized RZ gates and SX-conjugated RZZ pair interactions.
Consult backend_snapshot.json for physical ordering and supported edges, and
mapping.json and schedule.json for the workload realization.

You are not expected to construct a dense 2^32 statevector or enumerate the
full output distribution. Naive dense-state brute-force analysis is intentionally
impractical; use structured simulation, subsystem analysis and forensic reasoning.
Qiskit Aer matrix_product_state is recommended for participants who choose to
reproduce global behavior. simulation_config.json provides settings and
convergence guidance. Report approximations and observable sensitivity to them.

The large workload and targeted full-device verification companion are an
explicit challenge design choice. The limited verification protocol is diagnostic
evidence collected to investigate the integrity of large-workload execution;
it is not a toy replacement for the workload. Structural main-workload parity
invariants alone do not identify the fault. verification_circuit.qpy and
verification_protocol.json define the companion; observable_spec.json specifies
selected observations, their role and tolerances. Interpret these alongside
the characterization, circuit and provenance evidence.

## Evidence and model assumptions

Use the QPY circuits and readable drawings to inspect operations; mapping,
schedule and runtime/backend snapshots to inspect realization; device_design.json,
device_model.json and provenance_registry.json to inspect source relationships;
and control manifests/calibration, characterization and noise snapshots to
assess physical consequences and competing explanations. Follow artifact IDs,
revisions, timestamps, geometry fingerprints, units and record relationships.

The supplied reduced model assumes omega=2*pi*g_MHz/1000 rad/ns for the effective
entangling angular rate. It is a challenge-model assumption, not a universal
device law. Probe XI means X on Q1 and identity on Q4; its preparation and pulse
sequence are specified in characterization.json. The supplied local subsystem
contains two qubits and is described in local_subsystem.json. Full-device EM
or a 32-qubit physical Hamiltonian reconstruction is unnecessary.

Consider compiler, mapping, scheduling, physical-edge and control-channel
explanations; ordinary T1/T2 and readout degradation; device/model/calibration
provenance; and other explanations supported by evidence. Explain why each
accepted explanation fits and why rejected alternatives are insufficient.
Analytic synthetic records are supplied as challenge evidence; keep their
declared assumptions explicit in your reasoning.

## Rubric

| ID | Category | Points |
|---|---|---:|
| M1 | Execution verdict | 8 |
| M2 | Circuit/compilation assessment | 8 |
| M3 | Mapping/runtime physical-edge assessment | 8 |
| M4 | Current device-design provenance | 10 |
| M5 | Device-model provenance analysis | 18 |
| M6 | Coupling/rate physical consequence | 14 |
| M7 | Root-cause boundary localization | 14 |
| M8 | Rejection of alternatives/distractors | 8 |
| M9 | Proposed repair | 6 |
| M10 | Repair-validation strategy | 6 |
| | **Total** | **100** |

## Submission expectations

Implement solve_stage2(stage_dir) in solve.py and generate the required result
using evaluate.py. Cite artifacts with claims and locators. Include supported
layer assessments, estimates with units, physical resources, alternative
explanations, repair and repair-validation reasoning. Partial submissions are
valid; unsupported claims receive no corresponding milestone credit.

The public validator checks structure and hashes; organizers assign correctness
points. Stage 1 and Stage 2 total 200 core points, with an optional +30 bonus
for a maximum of 230. You can earn the full 200/200 core score without Quantum
Metal, scqubits or QuTiP. Python, Qiskit, Aer, NumPy and SciPy are core tools;
qiskit-ibm-runtime is recommended for local/fake-backend workflows only.

## Optional physics bonus

| Bonus | Category | Points |
|---|---|---:|
| B1 | Quantum Metal device/geometry reconstruction | +10 |
| B2 | scqubits reduced device-model reconstruction | +10 |
| B3 | QuTiP local control-dynamics validation | +10 |
| | **Maximum bonus** | **+30** |

Quantum Metal (formerly Qiskit Metal), scqubits, qutip and qutip-qip are optional.
Place source, assumptions, reproduction command, results and comparison against
the supplied evidence in the matching extensions/ directory. Organizers review
bonus work separately. Do not alter solve.py or the required result.json for
bonus work. Numerical reconstruction is never required for core completion.
