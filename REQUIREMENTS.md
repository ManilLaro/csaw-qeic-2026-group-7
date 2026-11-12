# Environment requirements

Core required: Python >=3.11, Qiskit, Qiskit Aer, NumPy and SciPy.
Install with `python -m pip install -r requirements-core.txt`.

Recommended: qiskit-ibm-runtime for local/fake-backend workflows only. No IBM
Quantum account, authentication or QPU access is required.

Optional bonus: quantum-metal (formerly Qiskit Metal, import qiskit_metal),
scqubits, qutip and qutip-qip. requirements-advanced.txt includes the core,
recommended integration and optional bonus packages for convenience. A participant
can earn all 200 core points without these physics reconstruction packages.

`python environment_check.py --smoke` checks files, JSON and QPY presence using
the standard library, without simulation or heavy imports. The optional --full
mode imports Qiskit/Aer and available recommended/bonus packages; missing core
packages fail while missing optional packages are reported separately. NumPy,
SciPy and qutip-qip are checked through distribution metadata. No mode executes
circuits, authenticates, meshes geometry, diagonalizes models or runs dynamics.

Supplied geometry metadata, coupling proxies, reduced-model assumptions and
characterization are authoritative challenge inputs. Reconstructing them
numerically is optional bonus work, separate from the required submission.
