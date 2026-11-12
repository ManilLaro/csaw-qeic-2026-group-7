"""Offline probe inspection only. No oracle, subprocess, simulation or network."""
import argparse, json
from pathlib import Path
def inspect_probe(circuit_path, physical_qubits, shots):
    from qiskit import qpy
    budget=json.loads(Path(__file__).with_name('diagnostic_budget.json').read_text(encoding='utf-8'))
    with Path(circuit_path).open('rb') as f: circuits=qpy.load(f)
    if len(circuits)!=1: raise ValueError('one circuit required')
    c=circuits[0]
    if len(set(physical_qubits))!=len(physical_qubits) or len(physical_qubits)!=c.num_qubits or not 1<=c.num_qubits<=budget['maximum_qubits_per_probe']: raise ValueError('invalid physical qubits')
    if any(type(q) is not int or not 0<=q<6 for q in physical_qubits): raise ValueError('physical index outside backend')
    if type(shots) is not int or not 1<=shots<=budget['maximum_total_diagnostic_shots']: raise ValueError('invalid shots')
    if c.depth()>budget['maximum_circuit_depth'] or set(c.count_ops())-set(budget['allowed_operations']): raise ValueError('probe outside budget')
    return {'status':'probe structurally valid; not executed','shots':shots,'qubits':physical_qubits,'depth':c.depth()}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('circuit',type=Path); p.add_argument('--physical-qubits',type=int,nargs='+',required=True); p.add_argument('--shots',type=int,required=True); a=p.parse_args()
    print(json.dumps(inspect_probe(a.circuit,a.physical_qubits,a.shots),indent=2))
