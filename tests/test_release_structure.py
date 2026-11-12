import json
from conftest import ROOT
from validate_submission import artifact_hashes
def test_required_files_and_json():
    rules=json.loads((ROOT/'rules.json').read_text(encoding='utf-8'))
    assert all((ROOT/p).is_file() for p in rules['required_files'])
    for path in ROOT.rglob('*.json'):
        if not set(path.relative_to(ROOT).parts)&{'outputs','.pytest_cache','.venv'}: json.loads(path.read_text(encoding='utf-8'))
    assert artifact_hashes()
def test_rubrics():
    rules=json.loads((ROOT/'rules.json').read_text(encoding='utf-8'))
    assert all(sum(m['weight'] for m in r)==100 for r in rules['rubrics'].values())
