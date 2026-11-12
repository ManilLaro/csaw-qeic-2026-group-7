import json, math
import pytest
from conftest import ROOT
A=ROOT/'stages/stage_2/artifacts'
def test_configuration():
    config=json.loads((A/'simulation_config.json').read_text(encoding='utf-8'))
    assert config['active_qubits']>=30
    assert config['recommended_method']=='matrix_product_state'
    assert config['local_physics_subsystem_max_qubits']<=4
    assert not config['dense_statevector_required']
def test_qpy_inspection():
    qpy=pytest.importorskip('qiskit.qpy')
    circuits=[]
    for name in ('intended','compiled'):
        with (A/f'{name}_circuit.qpy').open('rb') as f: circuits.append(qpy.load(f)[0])
    a,b=circuits
    assert a.num_qubits==b.num_qubits==32
    assert a==b
    assert 120<=b.depth()<=180
    assert len({a.find_bit(q).index for i in a.data for q in i.qubits})>=30
    assert sum(len(i.qubits)==2 for i in a.data)>30
    assert any(i.operation.name=='rz' and abs(float(i.operation.params[0])/(math.pi/2)-round(float(i.operation.params[0])/(math.pi/2)))>1e-6 for i in a.data)
def test_registry_digests():
    from validate_submission import sha
    registry=json.loads((A/'provenance_registry.json').read_text(encoding='utf-8'))
    ids={item['artifact_id'] for item in registry['artifacts']}
    assert len(ids)==len(registry['artifacts'])
    for item in registry['artifacts']: assert sha(A/item['path'])==item['sha256']
