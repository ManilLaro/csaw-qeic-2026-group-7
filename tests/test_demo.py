import runpy
from conftest import ROOT
def test_demo():
    result=runpy.run_path(str(ROOT/'demo/walkthrough.py'))['walkthrough']()
    assert result['submission']['verdict']=='INTEGRITY_VIOLATION'
    assert len(result['instruction_differences'])==1
