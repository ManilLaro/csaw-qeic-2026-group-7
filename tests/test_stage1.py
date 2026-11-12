import json
from conftest import ROOT
def test_stage1_static_files():
    folder=ROOT/'stages/stage_1/artifacts'
    for name in ('intended_circuit.qpy','compiled_circuit.qpy','mapping_current.json','schedule.json','control_manifest.json','diagnostic_budget.json'):
        assert (folder/name).is_file()
    client=(folder/'diagnostic_client.py').read_text(encoding='utf-8')
    assert 'subprocess' not in client.replace('No oracle, subprocess, simulation or network.','')
    assert not (folder/'organizer').exists()

def test_stage1_qpy_loads():
    import pytest
    qpy=pytest.importorskip("qiskit.qpy")
    for name in ("intended", "compiled"):
        with (ROOT/"stages/stage_1/artifacts"/f"{name}_circuit.qpy").open("rb") as f:
            assert len(qpy.load(f))==1
