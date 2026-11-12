import json
from conftest import ROOT
from validate_submission import artifact_hashes

def load(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))

def test_core_and_bonus_rubrics():
    rules=load('rules.json')
    assert rules['max_points']['stage_2']==100
    assert sum(m['weight'] for m in rules['rubrics']['stage_2'])==100
    assert rules['bonus']['max_points']==30
    assert sum(m['weight'] for m in rules['bonus']['rubric'])==30
    assert [m['id'] for m in rules['bonus']['rubric']]==['B1','B2','B3']
    assert rules['bonus']['separate_from_core']
    assert not rules['bonus']['required_for_completion']

def test_dependency_classification():
    deps=load('rules.json')['dependencies']
    assert deps['core_required']==['Python>=3.11','qiskit','qiskit-aer','numpy','scipy']
    assert deps['recommended']==['qiskit-ibm-runtime']
    assert deps['optional_bonus']==['quantum-metal','scqubits','qutip','qutip-qip']
    core=(ROOT/'requirements-core.txt').read_text(encoding='utf-8')
    assert 'qiskit-aer>=0.17,<1' in core
    assert all(name not in core for name in deps['optional_bonus'])
    assert load('stages/stage_2/artifacts/tool_requirements.json')['dependencies']==deps

def test_bonus_files_do_not_change_core_hashes(tmp_path, monkeypatch):
    import validate_submission as validator
    # Test manifest behavior in an isolated tree; never modify release artifacts.
    root=tmp_path/'release'; root.mkdir()
    (root/'fixed.txt').write_text('immutable')
    (root/'release_manifest.json').write_text(json.dumps({'fixed.txt':validator.sha(root/'fixed.txt')}))
    monkeypatch.setattr(validator,'ROOT',root)
    before=validator.artifact_hashes()
    (root/'extensions/device_reconstruction').mkdir(parents=True)
    (root/'extensions/device_reconstruction/results.json').write_text('{}')
    assert validator.artifact_hashes()==before

def test_bonus_templates():
    rules=load('rules.json')
    for item in rules['bonus']['rubric']:
        text=(ROOT/item['directory']/'README.md').read_text(encoding='utf-8')
        for term in ('source code','assumptions','reproduction command','results','comparison','solve.py','result.json'): assert term in text
