import json
from conftest import ROOT

def test_published_scoring_totals():
    rules=json.loads((ROOT/'rules.json').read_text(encoding='utf-8'))
    assert sum(x['weight'] for x in rules['rubrics']['stage_1'])==100
    assert sum(x['weight'] for x in rules['rubrics']['stage_2'])==100
    assert sum(x['weight'] for x in rules['bonus']['rubric'])==30
    assert rules['score_totals']==dict(stage_1=100,stage_2=100,core=200,bonus=30,maximum=230,demo=0)
    root=(ROOT/'README.md').read_text(encoding='utf-8')
    for stage in ('stage_1','stage_2'):
        doc=(ROOT/'stages'/stage/'README.md').read_text(encoding='utf-8')
        for row in rules['rubrics'][stage]:
            line=f"| {row['id']} | {row['category'][0].upper()+row['category'][1:]} | {row['weight']} |"
            assert line in root and line in doc
