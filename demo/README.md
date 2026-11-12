# Demo — Compiled Operation Integrity

**DEMO — NOT SCORED · 0 points**

This worked tutorial shows how to load QPY, compare intended and compiled
operations, identify parameter changes, inspect frozen execution counts and
construct an evidence-based answer. Run from the repository root:

```sh
python demo/walkthrough.py
```

The walkthrough loads each QPY circuit, enumerates instruction names, qubits
and numeric parameters, and compares them. Intended RZ(pi/2) becomes compiled
RZ(pi), despite matching instruction structure. The conclusion is
INTEGRITY_VIOLATION at COMPILER, with INCORRECT_ROTATION_PARAMETER. Frozen counts
provide supporting evidence; counts alone do not locate the responsible layer.
The script prints the differences and an answer dictionary without simulation.

Use the same evidence fields for the scored stages, with artifact filenames
relative to the appropriate stage artifact directory. The demo answer is a
tutorial response and is not submitted as a scored stage. To exercise the
required evaluator and validator with your starter solution:

```sh
python evaluate.py --submission solve.py --output outputs/result.json
python validate_submission.py outputs/result.json --submission solve.py
```

The starter solution returns INSUFFICIENT_EVIDENCE for both scored stages.
Replace its logic with your analysis, regenerate the result, and submit the
exact solve.py together with outputs/result.json. Demo artifact identifiers
are retained for precise citation. The demo itself earns no points.
