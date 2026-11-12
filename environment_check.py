"""Smoke checks are standard-library only; full mode only imports packages."""
import argparse, importlib, json, platform, sys
from pathlib import Path
from validate_submission import ROOT, read_json, artifact_hashes
CORE_IMPORTS=['qiskit','qiskit_aer']
RECOMMENDED_IMPORTS=['qiskit_ibm_runtime']
BONUS_IMPORTS=['qiskit_metal','scqubits','qutip']
def main():
    p=argparse.ArgumentParser(description=__doc__); g=p.add_mutually_exclusive_group(required=True); g.add_argument('--smoke',action='store_true'); g.add_argument('--full',action='store_true'); args=p.parse_args()
    print('Python',platform.python_version()); failures=[]
    if sys.version_info<(3,11): failures.append('Python >=3.11 required')
    for name in read_json(ROOT/'rules.json')['required_files']:
        if not (ROOT/name).is_file(): failures.append('Missing '+name)
    for file in ROOT.rglob('*.json'):
        if not set(file.relative_to(ROOT).parts)&{'.git','outputs','extensions','.venv','.pytest_cache'}: read_json(file)
    for folder in ('demo','stages/stage_1','stages/stage_2'):
        for kind in ('intended','compiled'):
            if not (ROOT/folder/'artifacts'/f'{kind}_circuit.qpy').is_file(): failures.append('Missing QPY '+folder)
    artifact_hashes()
    print('Core required: Python >=3.11, qiskit, qiskit-aer, numpy, scipy')
    print('Recommended: qiskit-ibm-runtime (simulation/local testing only)')
    print('Optional bonus: quantum-metal (formerly Qiskit Metal), scqubits, qutip, qutip-qip')
    if args.full:
        for name in CORE_IMPORTS+RECOMMENDED_IMPORTS+BONUS_IMPORTS:
            try: importlib.import_module(name); print(name,'import OK')
            except Exception as exc:
                if name in CORE_IMPORTS: failures.append(name+': '+str(exc))
                else: print(name,'optional unavailable:',str(exc))
        from importlib.metadata import version, PackageNotFoundError
        for name in ('numpy','scipy','qutip-qip'):
            try: print(name,version(name))
            except PackageNotFoundError:
                if name in ('numpy','scipy'): failures.append(name+': core dependency not installed')
                else: print(name,'optional bonus not installed')
    for error in failures: print(error,file=sys.stderr)
    return bool(failures)
if __name__=='__main__': raise SystemExit(main())
