"""Deterministic QPY inspection; never invokes a simulator."""
import json, math, sys
from pathlib import Path
from qiskit import qpy
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from solve import empty_submission
def walkthrough():
    circuits=[]
    for name in ('intended','compiled'):
        with (Path(__file__).parent/'artifacts'/f'{name}_circuit.qpy').open('rb') as f: circuits.append(qpy.load(f)[0])
    def instructions(c):
        return [(i.operation.name,[c.find_bit(q).index for q in i.qubits],[float(p) for p in i.operation.params]) for i in c.data]
    intended,compiled=map(instructions,circuits)
    changes=[{'index':n,'intended':a,'compiled':b} for n,(a,b) in enumerate(zip(intended,compiled)) if a!=b]
    assert len(intended)==len(compiled) and len(changes)==1
    a,b=changes[0]['intended'],changes[0]['compiled']
    assert a[:2]==b[:2] and a[0]=='rz' and math.isclose(a[2][0],math.pi/2) and math.isclose(b[2][0],math.pi)
    result=empty_submission(); result.update(verdict='INTEGRITY_VIOLATION',suspected_fault_type='INCORRECT_ROTATION_PARAMETER',layer_assessments={'COMPILER':'RZ parameter differs'},root_cause='RZ(pi/2) became RZ(pi)',confidence=1.0,
        artifact_evidence=[{'artifact':'intended_circuit.qpy','claim':'RZ(pi/2)'},{'artifact':'compiled_circuit.qpy','claim':'RZ(pi)'}])
    return {'instruction_differences':changes,'counts_support':json.loads((Path(__file__).parent/'artifacts/execution_counts.json').read_text(encoding='utf-8')),'submission':result}
if __name__=='__main__': print(json.dumps(walkthrough(),indent=2))
