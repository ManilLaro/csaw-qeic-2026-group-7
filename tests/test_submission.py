import copy, json, math
import pytest
from conftest import ROOT
import solve
from validate_submission import check, validate_stage, read_json
def test_starter_partial():
    for s in ('stage_1','stage_2'):
        r=solve.solve(s,ROOT/'stages'/s/'artifacts'); validate_stage(r,s)
        assert r['verdict']=='INSUFFICIENT_EVIDENCE'
        validate_stage({'verdict':'INSUFFICIENT_EVIDENCE','confidence':0},s)
@pytest.mark.parametrize('patch',[{'verdict':'OTHER'},{'confidence':-1},{'confidence':float('nan')},{'confidence':True},{'unknown':0},{'estimated_parameters':{'x':'bad'}},{'artifact_evidence':[{'artifact':'../../rules.json','claim':'bad'}]},{'diagnostic_evidence':[{'job_id':'x','shots':10001,'qubits':[1],'depth':1,'claim':'test'}]}])
def test_invalid_submissions(patch):
    r=solve.empty_submission(); r.update(patch)
    with pytest.raises(ValueError): validate_stage(r,'stage_1')
def test_duplicate_keys(tmp_path):
    p=tmp_path/'duplicate.json'; p.write_text('{"a":1,"a":2}')
    with pytest.raises(ValueError): read_json(p)
def test_budget_jobs():
    r=solve.empty_submission(); r['diagnostic_evidence']=[{'job_id':str(i),'shots':1,'qubits':[1],'depth':1,'claim':'test'} for i in range(21)]
    with pytest.raises(ValueError): validate_stage(r,'stage_1')
