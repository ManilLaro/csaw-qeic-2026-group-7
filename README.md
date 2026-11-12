# Quantum Execution Integrity Challenge

The Quantum Execution Integrity Challenge asks whether a quantum execution stack
faithfully realized the computation requested by the user. Investigate consistency
across the intended computation, transformed execution, physical realization and
observed evidence. **NOISE ≠ INTEGRITY FAILURE**: noisy execution can still be
valid, while plausible output can hide an integrity failure. Your task is to give
the strongest conclusion supported by the artifacts, explain the evidence, and
propose a repair and validation strategy when appropriate.

No physical quantum hardware is required. All execution is simulation-based,
using local tools and supplied frozen artifacts. No IBM Quantum account, QPU
access, credentials or live service calls are required.

## Challenge structure

| Part | Task | Points |
|---|---|---:|
| Demo — not scored | Worked introduction to artifact inspection and submissions | 0 |
| Stage 1 | Circuit, mapping, scheduling, control, calibration and execution forensics | 100 |
| Stage 2 | Larger simulated workload with device/model/calibration provenance | 100 |
| Optional bonus | Independent physical reconstruction | Up to +30 |

Work through the demo to learn the interfaces, then investigate each scored
stage independently. The demo explains its own answer; each stage asks you to
develop your own evidence-based analysis. Read the stage README before drawing
conclusions from individual files. A mismatch in one record can matter differently
depending on its relationship to the rest of the execution stack.

## Scoring

**Stage 1: 100 points · Stage 2: 100 points · Core total: 200 points**  
**Optional bonus: up to +30 points · Maximum possible score: 230 points**

The demo contributes 0 points. Partial credit is awarded by milestone, with each
milestone evaluated independently where possible. You do not need every field
correct to earn partial credit. Unsupported or incorrect claims do not receive
the corresponding milestone points. An incorrect answer in one milestone does
not automatically erase unrelated, correctly supported milestones. Distinguish
what you established from what you suspect, and cite evidence for both.

The public validator confirms that your submission is structurally valid. Final
challenge points are assigned by the organizers using the published milestone
rubric. It does not reveal whether your conclusions are correct. Bonus work is
reviewed separately and does not change the required core submission. You can
earn the full **200/200 core score** without installing or using Quantum Metal,
scqubits or QuTiP.

## Setup

Use Python >=3.11. Core required packages are Qiskit, Qiskit Aer, NumPy and SciPy;
install them from requirements-core.txt. A virtual environment keeps the challenge
dependencies separate from other projects. Run the commands below from the
repository root so relative paths resolve consistently.

```sh
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```sh
source .venv/bin/activate
```

Then install the core requirements:

```sh
python -m pip install -r requirements-core.txt
```

qiskit-ibm-runtime is recommended for local/fake-backend style workflows only.
No IBM Quantum account or QPU access is required. Quantum Metal (formerly Qiskit
Metal), scqubits, qutip and qutip-qip are optional bonus dependencies. Install the
combined optional environment only if you want those tools:

```sh
python -m pip install -r requirements-advanced.txt
```

See REQUIREMENTS.md for dependency classifications and environment-check behavior.
You do not need the optional environment to complete the scored core stages.

## Quick start

After activating the environment and installing the core requirements:

```sh
python environment_check.py --smoke
python demo/walkthrough.py
python evaluate.py --submission solve.py --output outputs/result.json
python validate_submission.py outputs/result.json --submission solve.py
```

The smoke check inspects release files, JSON and QPY presence without running a
simulation. The demo loads frozen circuits and compares instructions. The
evaluator runs the supplied starter solution independently for each stage and
writes the result. The validator checks the generated submission. A structurally
valid starter result demonstrates the format; it does not establish a correct
stage diagnosis or earn correctness points by itself.

## What to submit

Your required core submission consists of:

1. **solve.py**, containing your forensic analysis.
2. **outputs/result.json**, generated from that exact solve.py.

After changing your solution, regenerate and validate the result:

```sh
python evaluate.py --submission solve.py --output outputs/result.json
python validate_submission.py outputs/result.json --submission solve.py
```

Do not edit challenge artifacts. The evaluator hashes the participant solution
and public challenge artifacts; if artifacts are modified, validation may fail.
Do not manually change result.json to make it disagree with the submitted
solution. Submit optional extensions separately if you attempt the bonus.

solve.py is the main participant-editable file. Implement solve_stage1(stage_dir)
and solve_stage2(stage_dir) according to the provided interface; solve(stage_name,
stage_dir) dispatches to them. Each stage receives its own artifact directory.
The supplied starter solution deliberately returns INSUFFICIENT_EVIDENCE with
zero confidence. Replace that logic with your forensic analysis. Keep each
stage's logic self-contained: the evaluator imports the solution in a separate
Python process for each stage. The --timeout option controls the time allowed
per stage if your analysis needs longer than the default 120 seconds.

The root submission_schema.json defines the result envelope and stage-response
format. Verdicts are VALID_EXECUTION, INTEGRITY_VIOLATION and INSUFFICIENT_EVIDENCE.
Confidence must be a finite number between 0 and 1. Partial stage responses are
valid: return a verdict and confidence, then include the supported fields.
Unsupported fields may remain empty or null where the schema permits.

Use layer_assessments for your layer-by-layer conclusions and artifact_evidence
for citations. Each artifact citation has an artifact filename relative to that
stage's artifact directory, a claim, and an optional locator such as a JSON key
or instruction index. Resource lists use descriptive strings. Estimated
parameters must be numeric; include units in the parameter key. Additional
evidence fields can separate runtime, device design, device model, control and
simulation reasoning. Explain assumptions so another person can reproduce your
conclusions from the same evidence.

The public validator checks formatting, schema, required stages, artifact hashes,
legal values, confidence range and submission consistency. It also checks the
format and aggregate limits of self-reported diagnostic records. These are
structural checks, not correctness scoring. Use the root schema even where an
artifact directory includes an additional case-specific schema.

## Stage 1

Stage 1 asks you to investigate execution-stack consistency using intended and
compiled circuits, mapping records, a schedule, calibration/control records and
execution evidence. Identify which explanations the combined records support,
which alternatives they rule out, and what repair and validation you recommend.
Read [the Stage-1 task](stages/stage_1/README.md) for artifact guidance.

| ID | Category | Points |
|---|---|---:|
| M1 | Execution verdict | 10 |
| M2 | Intended/compiled consistency | 8 |
| M3 | Current mapping assessment | 8 |
| M4 | Schedule/mapping consistency | 8 |
| M5 | Control/calibration provenance | 12 |
| M6 | Affected logical operation | 8 |
| M7 | Expected physical resource | 8 |
| M8 | Suspected realized physical resource | 15 |
| M9 | Incidental-anomaly rejection | 8 |
| M10 | Diagnostic evidence | 5 |
| M11 | Proposed repair | 5 |
| M12 | Repair validation | 5 |
| | **Total** | **100** |


Separate observations from causal conclusions. A circuit comparison alone may
leave questions about downstream realization; output counts alone may leave
questions about where a discrepancy arose. Your response should connect the
relevant records rather than simply list anomalies.

## Stage 2

Stage 2 extends the analysis to a 32-qubit simulated workload and the provenance
of device design, device models and control calibration. The supplied frozen
artifacts are authoritative challenge evidence. Core participants may use
geometry metadata, coupling proxies, the reduced-model assumption and supplied
characterization directly; numerical physics reconstruction is not required.

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


The challenge deliberately pairs the large workload with a targeted full-device
verification companion. This limited protocol is diagnostic evidence collected
to investigate the workload's execution integrity; it is not a toy replacement
for the workload. Structural main-workload parity invariants alone do not
identify the fault. Analyze verification observations alongside the circuit,
runtime, characterization and provenance records.

You are not expected to construct a dense 2^32 statevector or enumerate the full
output distribution. Qiskit Aer matrix_product_state is recommended if you choose
to reproduce global simulation behavior. Use the selected observables and local
subsystem evidence, and document structured-simulation settings and convergence
checks for any results you produce. Read [the Stage-2 task](stages/stage_2/README.md)
for the model assumptions, protocol and evidence map.

## Optional bonus

Bonus work is optional and separate from the core 200 points:

| Bonus | Category | Points |
|---|---|---:|
| B1 | Quantum Metal device/geometry reconstruction | +10 |
| B2 | scqubits reduced device-model reconstruction | +10 |
| B3 | QuTiP local control-dynamics validation | +10 |
| | **Maximum bonus** | **+30** |


Place work in extensions/device_reconstruction/, extensions/scqubits_model/ or
extensions/qutip_validation/. Each bonus submission must contain source code,
assumptions, an exact reproduction command, results and a comparison against
supplied challenge evidence. Quantum Metal is formerly Qiskit Metal and uses
the qiskit_metal import name. Limit physical reconstruction to the supplied local
subsystem; full-device EM or a 32-qubit Hamiltonian reconstruction is unnecessary.

Bonus work is reviewed separately by the organizers. Do not alter solve.py or
the required result.json for bonus work. extensions/ is outside the core artifact
hash set, so adding bonus code and results does not change your required core
submission. Include the relevant extension directories with your bonus submission.

## Rules

- Only local simulation is required; no real hardware access is necessary.
- Do not modify released challenge artifacts or evaluation tools.
- Make participant analysis reproducible and cite supporting artifact evidence.
- Submit result.json generated from the exact solve.py you submit.
- Network access is not necessary for the core challenge. Use frozen evidence
  and local tools; do not call live IBM services or submit hardware jobs.
- Optional diagnostic probes must obey the per-stage limits in rules.json.
  Inspection of supplied frozen artifacts is unmetered. Diagnostic records must
  accurately describe any optional local experiments you report.
- Bonus tools are optional; final correctness scoring is organizer-side.

The evaluator executes participant Python locally; run only code you trust.
It is not a security sandbox. Keep scratch work inside your own workspace and
do not rely on files outside the supplied evidence to support challenge claims.

## File structure

| File/Directory | Purpose | Edit? |
|---|---|---|
| solve.py | Participant solution | YES |
| evaluate.py | Runs participant solution and creates result | No |
| validate_submission.py | Checks result format/integrity | No |
| rules.json | Challenge rules and rubrics | No |
| submission_schema.json | Required result format | No |
| demo/ | Worked tutorial | No |
| stages/stage_1/ | Stage-1 evidence | No |
| stages/stage_2/ | Stage-2 evidence | No |
| extensions/ | Optional bonus work | YES |
| outputs/ | Generated results | Generated |

requirements-core.txt supplies the required environment; requirements-advanced.txt
adds the recommended integration and optional bonus packages. tests/ contains
lightweight format and artifact checks. release_manifest.json defines the core
artifact identity used by the validator. Keep a clean copy of the downloaded
challenge when comparing local file changes.

## Troubleshooting

If the environment check cannot find files, run it from the repository root and
confirm you have the complete download. If QPY loading fails, use the Qiskit
version range in requirements-core.txt and confirm your virtual environment is
active. The smoke check does not require importing optional physics packages.

If submission validation fails, check for an unsupported verdict, nonfinite
number, confidence outside [0,1], unknown field, malformed evidence record, or
an artifact path outside the relevant stage. Regenerate result.json after every
solution change. A solution-hash mismatch means the result and solution no longer
correspond. An artifact-hash mismatch means a released file has changed; restore
that file from a clean challenge copy instead of editing hashes.

Keep units and bit ordering explicit. MHz and rad/ns are different quantities;
logical indices and physical resource indices need not be interchangeable. Read
the observable and characterization specifications before interpreting values.
Do not treat a plausible histogram or an isolated noisy result as a complete
integrity argument: tie your verdict to the full set of supporting evidence.
