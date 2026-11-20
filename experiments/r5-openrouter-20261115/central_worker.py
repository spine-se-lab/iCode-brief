"""Grade immutable incoming candidates on the original mini; never call models."""
from pathlib import Path
import concurrent.futures, fcntl, hashlib, importlib.util, json, os, shutil, subprocess, time

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent.parent
os.environ['PATH'] = '/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:'+os.environ['PATH']
os.environ['DOCKER_CONTEXT'] = 'desktop-linux'
def read(p): return json.loads(p.read_text())
def save(p, value):
    q = p.with_suffix('.tmp'); q.write_text(json.dumps(value, indent=2)); q.replace(p)
def grade(d):
    row = read(d/'job.json'); image = None; owned_ref = None
    spec = importlib.util.spec_from_file_location('verify_'+d.name, ROOT/'verifier.py')
    v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
    v.ROOT = d; v.BASE = BASE
    save(d/'started.json', {'started_at':time.time()})
    try:
        for f, digest in row['tests_sha256'].items():
            assert hashlib.sha256((BASE/'deep-swe/tasks'/row['task']/f).read_bytes()).hexdigest() == digest, 'verifier_digest_mismatch'
        manifest=read(ROOT/'manifest.json');task=manifest['tasks'][row['task']]
        ref=task['base_registry_ref'];probe=subprocess.run(['docker','image','inspect',ref],text=True,capture_output=True)
        if probe.returncode:
            owned_ref=ref
            with (d/'pull.log').open('w') as log:subprocess.run(['docker','pull','--platform','linux/amd64',ref],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=1800)
        obj=json.loads(subprocess.check_output(['docker','image','inspect',ref],text=True))[0]
        assert task['base_registry_ref'] in obj['RepoDigests'] and obj['RootFS']['Layers']==task['base_layers']
        if not row.get('patch_sha256'):raise RuntimeError('missing_candidate_patch')
        image, image_id = v.build(row['task'])
        out = v.verify(row, image, image_id)
        import score_reports, regrade_remote
        regrade_remote.remap=score_reports.remap;regrade_remote.VERSION=score_reports.VERSION
        revision=regrade_remote.process({'id':row['candidate_id'],'task':row['task'],
            'evidence_root':str(d/(row['source_host']+'-'+row['task'])),'evidence_kind':'local'},ROOT/'regrading',BASE)
        out['score_revision']=revision
        out['patch_pass']=bool((revision.get('new_reward') or {}).get('reward')==1 and out.get('exit_code')==0)
        out['status']='pass' if out['patch_pass'] else 'fail'
    except Exception as e:
        out = {**row,'status':'fail','diagnostic':'verifier_infrastructure: '+str(e),'verifier_pass':False}
    finally:
        if image: subprocess.run(['docker','image','rm',image],capture_output=True)
        if owned_ref: subprocess.run(['docker','image','rm',owned_ref],capture_output=True)
    save(d/'result.json', out)
    return out

def main():
    lock = (ROOT/'worker.lock').open('w'); fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    jobs = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            for key, job in list(jobs.items()):
                if job.done(): job.result(); del jobs[key]
            ds = sorted(p.parent for p in (ROOT/'jobs').glob('*/job.json'))
            for d in ds:
                if d.name in jobs or (d/'result.json').exists(): continue
                if (d/'started.json').exists(): raise RuntimeError('Interrupted verification requires inspection: '+d.name)
                if len(jobs) >= 1 or shutil.disk_usage(ROOT).free < 15*2**30: break
                jobs[d.name] = pool.submit(grade,d)
            rows = [read(d/'result.json') for d in ds if (d/'result.json').exists()]
            complete = (ROOT/'collection-complete.json').exists() and len(rows)==len(ds) and not jobs
            save(ROOT/'summary.json', {'phase':'complete' if complete else 'running','received':len(ds),
                'passed':sum(r['status']=='pass' for r in rows),'failed':sum(r['status']=='fail' for r in rows),
                'pending':len(ds)-len(rows),'verifier_pass_including_unfinished_candidates':sum(bool(r.get('verifier_pass')) for r in rows),
                'running':list(jobs),'free_gib':round(shutil.disk_usage(ROOT).free/2**30,1),'rows':rows,'updated_at':time.time()})
            if complete: break
            time.sleep(15)

if __name__ == '__main__':
    try: main()
    except Exception as e:
        save(ROOT/'error.json', {'error':str(e),'at':time.time()}); raise
