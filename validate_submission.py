"""Standard-library structural validation. No correctness scoring."""
import argparse, hashlib, json, math, re
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def read_json(path):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise ValueError('duplicate JSON key: '+k)
            result[k]=v
        return result
    def reject(x): raise ValueError('nonfinite JSON number: '+x)
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=reject)
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def artifact_hashes():
    # Immutable release manifest defines exactly the public artifact set.
    manifest=read_json(ROOT/'release_manifest.json')
    actual={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file()
            and not set(p.relative_to(ROOT).parts)&{'.git','outputs','extensions','__pycache__','.pytest_cache','.venv'}
            and p.name not in {'solve.py','release_manifest.json'}}
    if actual != set(manifest): raise ValueError('release artifact file set changed')
    for name,expected in manifest.items():
        if sha(ROOT/name)!=expected: raise ValueError('modified public artifact: '+name)
    return {**manifest,'release_manifest.json':sha(ROOT/'release_manifest.json')}
def check(value, schema, path='$', root=None):
    root=root or schema
    if '$ref' in schema:
        ref=root
        for key in schema['$ref'][2:].split('/'): ref=ref[key]
        return check(value,ref,path,root)
    if 'const' in schema and value!=schema['const']: raise ValueError(path+': invalid constant')
    if 'enum' in schema and value not in schema['enum']: raise ValueError(path+': invalid enum')
    types=schema.get('type',[]); types=[types] if isinstance(types,str) else types
    matches={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),'null':value is None,'integer':type(value) is int,'number':type(value) in (int,float) and math.isfinite(value)}
    if types and not any(matches[t] for t in types): raise ValueError(path+': invalid type')
    if isinstance(value,float) and not math.isfinite(value): raise ValueError(path+': nonfinite')
    if isinstance(value,dict):
        for k in schema.get('required',[]):
            if k not in value: raise ValueError(path+': missing '+k)
        for k,v in value.items():
            child=schema.get('properties',{}).get(k,schema.get('additionalProperties',{}))
            if child is False: raise ValueError(path+': unknown '+k)
            if isinstance(child,dict): check(v,child,path+'.'+k,root)
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',float('inf')): raise ValueError(path+': array length')
        if schema.get('uniqueItems') and len({json.dumps(x,sort_keys=True) for x in value})!=len(value): raise ValueError(path+': duplicates')
        for i,v in enumerate(value): check(v,schema.get('items',{}),path+f'[{i}]',root)
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0) or ('pattern' in schema and not re.fullmatch(schema['pattern'],value)): raise ValueError(path+': invalid string')
    if type(value) in (int,float):
        if not schema.get('minimum',-math.inf)<=value<=schema.get('maximum',math.inf): raise ValueError(path+': range')
def validate_stage(value,stage):
    schema=read_json(ROOT/'submission_schema.json')
    check(value,schema['$defs']['stage'],root=schema)
    rules=read_json(ROOT/'rules.json'); limits=rules['diagnostic_limits']; jobs=value.get('diagnostic_evidence',[])
    if len(jobs)>limits['maximum_diagnostic_jobs'] or sum(j['shots'] for j in jobs)>limits['maximum_total_diagnostic_shots']: raise ValueError('diagnostic budget exceeded')
    if len({j['job_id'] for j in jobs})!=len(jobs): raise ValueError('duplicate diagnostic job')
    for key in ('artifact_evidence','runtime_evidence','device_design_evidence','device_model_evidence','control_evidence','simulation_evidence'):
        for e in value.get(key,[]):
            path=ROOT/'stages'/stage/'artifacts'/e['artifact']
            base=(ROOT/'stages'/stage/'artifacts').resolve()
            if not path.resolve().is_relative_to(base) or not path.is_file(): raise ValueError('invalid evidence path')
def validate(result,submission):
    check(result,read_json(ROOT/'submission_schema.json'))
    for stage,value in result['stage_submissions'].items(): validate_stage(value,stage)
    if result['participant_solution_sha256']!=sha(submission): raise ValueError('solution hash differs')
    if result['public_artifact_sha256']!=artifact_hashes(): raise ValueError('artifact hashes differ')
    return True
def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('result',type=Path); parser.add_argument('--submission',type=Path,default=ROOT/'solve.py'); args=parser.parse_args()
    try: validate(read_json(args.result),args.submission)
    except (ValueError,KeyError,TypeError,OSError) as e: parser.exit(1,str(e)+'\n')
    print('submission structurally valid; final correctness scoring is organizer-side')
if __name__=='__main__': main()
