"""Run the two stage solvers in independent local Python processes."""
import argparse, importlib.util, importlib.metadata, json, os, platform, subprocess, sys, tempfile, time
from pathlib import Path
from validate_submission import ROOT, artifact_hashes, sha, validate_stage, validate
def worker(submission,stage,output):
    spec=importlib.util.spec_from_file_location('participant_solution',submission)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    value=mod.solve(stage,ROOT/'stages'/stage/'artifacts'); validate_stage(value,stage)
    Path(output).write_text(json.dumps(value,allow_nan=False),encoding='utf-8')
def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--submission',type=Path,default=ROOT/'solve.py'); p.add_argument('--output',type=Path,default=ROOT/'outputs/result.json'); p.add_argument('--timeout',type=float,default=120); args=p.parse_args()
    start=time.perf_counter(); submission=args.submission.resolve(); before=sha(submission); hashes=artifact_hashes(); stages={}
    with tempfile.TemporaryDirectory() as tmp:
        for stage in ('stage_1','stage_2'):
            out=Path(tmp)/(stage+'.json')
            subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',str(submission),stage,str(out)],check=True,timeout=args.timeout,cwd=ROOT)
            stages[stage]=json.loads(out.read_text(encoding='utf-8'))
    packages={}
    for name in ('qiskit','numpy','scipy'):
        try: packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name]=None
    if before!=sha(submission) or hashes!=artifact_hashes(): raise ValueError('inputs changed during evaluation')
    result=dict(schema_version='1.0',challenge_release_id='qei-2026-11-12',participant_solution_sha256=before,public_artifact_sha256=hashes,stage_submissions=stages,runtime_seconds=time.perf_counter()-start,environment=dict(python=platform.python_version(),platform=platform.platform(),packages=packages))
    validate(result,submission)
    if args.output.resolve().parent != (ROOT/'outputs').resolve(): p.error('output must be directly inside outputs/')
    args.output.parent.mkdir(exist_ok=True); args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print('submission structurally valid; '+str(args.output))
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker': worker(*sys.argv[2:])
    else: main()
