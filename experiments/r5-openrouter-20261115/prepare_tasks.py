"""Single-dispatch free preparation; task inputs are public instructions only."""
from pathlib import Path
import fcntl,hashlib,json,os,shutil,subprocess,time
ROOT=Path(__file__).resolve().parent
BASE=ROOT.parent
os.environ['PATH']='/opt/homebrew/bin:/usr/local/bin:'+os.environ['PATH']
os.environ['DOCKER_CONTEXT']='desktop-linux'
def read(p):return json.loads(p.read_text())
def save(p,x):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(x,indent=2));t.replace(p)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def cmd(args,**kw):return subprocess.check_output(args,text=True,timeout=30,**kw)
def main():
 locks=[]
 for rel in ('deepswe-runs/r5-verification-global.lock','official-deepswe-20261020/verification-global.lock'):
  f=(BASE/rel).open('a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(f)
 assert not cmd(['docker','ps','-q']).strip(),'other_running_containers'
 assert not (ROOT/'launch-intent.json').exists(),'already_launched'
 assert shutil.disk_usage(BASE).free>=60*2**30
 tasks=read(ROOT/'task-identities.json');out={}
 for i,task in enumerate(read(ROOT/'selection.json')['tasks'],1):
  save(ROOT/'prepare-progress.json',{'at':time.time(),'task':task,'index':i,'total':113})
  t=tasks[task];source=BASE/'deep-swe/tasks'/task
  for rel,digest in t['tests_sha256'].items():assert sha(source/rel)==digest,'test_identity_mismatch'
  ref=t['base_registry_ref']
  if subprocess.run(['docker','image','inspect',ref],capture_output=True).returncode:
   with (ROOT/('pull-'+task+'.log')).open('x') as log:
    subprocess.run(['docker','pull','--platform','linux/amd64',ref],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1800)
  obj=json.loads(cmd(['docker','image','inspect',ref]))[0]
  assert ref in obj['RepoDigests'] and obj['RootFS']['Layers']==t['base_layers'] and obj['Architecture']=='amd64'
  dest=ROOT/'inputs'/task/'instruction.md';dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/'instruction.md',dest)
  name='icode-r5-or-preflight-'+str(i)
  cid=cmd(['docker','run','-d','--name',name,'--label','compass.icode.r5=openrouter-20261115','--platform','linux/amd64','--network','none','--cap-drop','NET_RAW','--cpus','2','--memory','5g','--entrypoint','bash',obj['Id'],'-lc','sleep 600']).strip()
  try:
   info=json.loads(cmd(['docker','inspect',cid]))[0]
   assert info['HostConfig']['NetworkMode']=='none' and not info.get('Mounts')
   assert not any(any(word in e.partition('=')[0].upper() for word in ('API_KEY','ACCESS_TOKEN','SECRET')) for e in info['Config'].get('Env',[]))
   base_commit=cmd(['docker','exec','-w','/app',cid,'bash','-lc','command -v timeout >/dev/null && test ! -e /tests && test ! -e /logs/artifacts && git rev-parse HEAD']).strip()
   assert len(base_commit)==40
   out[task]={**t,'image_id':obj['Id'],'base_commit':base_commit,'instruction_sha256':sha(dest),'task_toml_sha256':sha(source/'task.toml')}
  finally:subprocess.run(['docker','rm','-f',cid],check=True,capture_output=True)
 cfg={'base':str(BASE),'root':str(ROOT),'tasks':out,'python':str(ROOT/'iCode/.venv/bin/python'),'eval':str(BASE/'deepswe-runs/icode-r5-openrouter-eval-20261115')}
 save(ROOT/'runtime.json',cfg)
 evaluation=Path(cfg['eval']);evaluation.mkdir(exist_ok=False);(evaluation/'jobs').mkdir()
 for name in ('central_worker.py','verifier.py','score_reports.py','regrade_remote.py'):shutil.copyfile(ROOT/name,evaluation/name)
 save(evaluation/'manifest.json',{'tasks':out})
 save(ROOT/'preflight.json',{'at':time.time(),'passed':True,'task_count':len(out),'model_calls':0,'task_images':{n:t['image_id'] for n,t in out.items()},'free_gib':shutil.disk_usage(BASE).free/2**30})
if __name__=='__main__':
 try:main()
 except Exception as e:
  save(ROOT/'prepare-error.json',{'at':time.time(),'error':type(e).__name__+': '+str(e)});raise
