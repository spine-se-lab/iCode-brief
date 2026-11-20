"""Verify frozen generated patches centrally; never invoke a model or alter task tests."""
from pathlib import Path
import concurrent.futures,hashlib,json,os,shutil,subprocess,time,traceback
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent
BASE=ROOT.parent.parent
os.environ['PATH']='/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:'+os.environ['PATH']
os.environ['DOCKER_CONTEXT']='desktop-linux'

def now():return datetime.now(timezone.utc).isoformat()
def save(p,d):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(d,indent=2));t.replace(p)
def call(args,log,timeout):
 with log.open('w') as f:return subprocess.run(args,stdout=f,stderr=subprocess.STDOUT,timeout=timeout).returncode

def build(task):
 d=ROOT/('image-'+task);d.mkdir(exist_ok=True)
 image='compass-central-'+task+':'+ROOT.name.lower()
 tests=BASE/'deep-swe/tasks'/task/'tests'
 manifest=json.loads((ROOT.parent.parent/'manifest.json').read_text())
 ref=manifest['tasks'][task]['base_registry_ref']
 lines=(tests/'Dockerfile').read_text().splitlines(keepends=True)
 assert sum(l.strip().upper().startswith('FROM ') for l in lines)==1
 (d/'Dockerfile').write_text(''.join('FROM '+ref+'\n' if l.strip().upper().startswith('FROM ') else l for l in lines))
 for attempt in range(1):
  rc=call(['docker','build','--pull=false','--network','none','--platform','linux/amd64','-f',str(d/'Dockerfile'),'-t',image,str(tests)],d/f'build-{attempt}.log',1800)
  if rc==0:break
  txt=(d/f'build-{attempt}.log').read_text(errors='replace')
  if '429' not in txt and 'Too Many Requests' not in txt:raise RuntimeError('verifier_image_build_failed')
  time.sleep(30)
 else:raise RuntimeError('verifier_registry_throttled')
 obj=json.loads(subprocess.check_output(['docker','image','inspect',image],text=True))[0]
 save(d/'image.json',{k:obj.get(k) for k in ['Id','RepoTags','RootFS','Architecture','Created']})
 return image,obj['Id']

def verify(row,image,image_id):
 d=ROOT/(row['source_host']+'-'+row['task']);d.mkdir(exist_ok=True);(d/'verifier').mkdir(exist_ok=True)
 name='c0240-r5-'+hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
 out={**row,'status':'running','started_verifying_at':now(),'verifier_image_id':image_id,'memory_mb':5120,'cpus':2,'timeout_seconds':3600,'model_called':False,'network':'bridge','build_network':'none'}
 save(d/'central-result.json',out)
 try:
  patch=d/'artifacts/model.patch'
  if not patch.exists():raise RuntimeError('missing_submitted_patch')
  if hashlib.sha256(patch.read_bytes()).hexdigest()!=row['patch_sha256']:raise RuntimeError('patch_digest_mismatch')
  for f,h in row['tests_sha256'].items():
   if hashlib.sha256((BASE/'deep-swe/tasks'/row['task']/f).read_bytes()).hexdigest()!=h:raise RuntimeError('verifier_digest_mismatch')
  rc=call(['docker','run','--rm','--name',name,'--label','compass.central='+ROOT.name,'--platform','linux/amd64','--network','bridge','--memory','5g','--cpus','2','-v',str(d/'verifier')+':/logs/verifier','-v',str(d/'artifacts')+':/logs/artifacts:ro',image,'bash','/tests/test.sh'],d/'verifier/test-stdout.txt',3600)
  out['exit_code']=rc
  p=d/'verifier/reward.json';out['rewards']=json.loads(p.read_text()) if p.exists() else None
  out['verifier_pass']=bool(out['rewards'] and out['rewards'].get('reward')==1 and rc==0)
  out['status']='pass' if out['verifier_pass'] and not row.get('generation_exception') else 'fail'
  if out['rewards'] is None:out['diagnostic']='missing_reward'
  txt=(d/'verifier/test-stdout.txt').read_text(errors='replace');out['segfault_seen']='Segmentation fault' in txt
 except subprocess.TimeoutExpired:
  subprocess.run(['docker','kill',name],capture_output=True);out.update(status='fail',diagnostic='verifier_timeout',exit_code=124)
 except Exception as e:out.update(status='fail',diagnostic=str(e))
 finally:subprocess.run(['docker','rm',name],capture_output=True)
 out['finished_verifying_at']=now();save(d/'central-result.json',out)
 print(json.dumps({k:out.get(k) for k in ['source_host','task','status','diagnostic','generation_exception']}),flush=True)
 return out
