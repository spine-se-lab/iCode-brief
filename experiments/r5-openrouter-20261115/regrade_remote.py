"""Executed on the evidence host; only metadata is returned to control host."""
import contextlib, hashlib, importlib.util, io, json
from pathlib import Path


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def score(full, seen):
    buckets = {k:list(dict.fromkeys(str(x).strip() for x in full[k] if str(x).strip())) for k in ('p2p_node_ids','f2p_node_ids')}
    pp = sum(seen.get(x,('missing',''))[0]=='passed' for x in buckets['p2p_node_ids'])
    fp = sum(seen.get(x,('missing',''))[0]=='passed' for x in buckets['f2p_node_ids'])
    pt, ft = len(buckets['p2p_node_ids']),len(buckets['f2p_node_ids'])
    return dict(reward=int(ft>0 and fp==ft and pp==pt),f2p_total=ft,f2p_passed=fp,p2p_total=pt,p2p_passed=pp,
                f2p=fp/ft if ft else 0.0,p2p=pp/pt if pt else 1.0,partial=(fp+pp)/(ft+pt) if ft+pt else 0.0)


def process(j, root, base):
    out={**j,'score_version':VERSION}
    d=Path(j['evidence_root'])
    rewards=list(d.rglob('reward.json')) if j['evidence_kind']=='central' else [d/'verifier/reward.json']
    rewards=[p for p in rewards if p.exists() and 'score96' not in str(p)]
    if len(rewards)!=1:
        return {**out,'replay':'unavailable','reason':'no_unique_reward','reward_files':len(rewards)}
    rp=rewards[0];v=rp.parent;old=json.loads(rp.read_text());out['old_reward']=old
    cfgpath=base/'deep-swe/tasks'/j['task']/'tests/config.json';gp=cfgpath.with_name('grader.py')
    full=json.loads(cfgpath.read_text());cfg=full['grade']
    spec=importlib.util.spec_from_file_location('grader_'+j['id'],gp);g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
    seen={};hashes={str(rp):digest(rp),str(cfgpath):digest(cfgpath),str(gp):digest(gp)};missing=[]
    for rep in cfg['reports']:
        name=Path(rep).name;candidates=[p for p in (v/name,v/'reports'/name) if p.exists()]
        if len(candidates)!=1:
            missing.append(name);continue
        p=candidates[0];hashes[str(p)]=digest(p)
        for k,(status,message) in g.PARSERS[cfg.get('format','ctrf')](p,cfg).items():g.add(seen,k,status,message)
    old_replay=score(full,seen)
    if old.get('apply_failed'):
        return {**out,'replay':'apply_failed_retained','new_reward':old,'input_hashes':hashes}
    keys=('reward','f2p_total','f2p_passed','p2p_total','p2p_passed')
    if any(old.get(k)!=old_replay.get(k) for k in keys):
        return {**out,'replay':'mismatch_stop','old_replay':old_replay,'missing_reports':missing,'input_hashes':hashes}
    repaired,aliases=remap(j['task'],seen,full['p2p_node_ids']+full['f2p_node_ids'])
    new=score(full,repaired);dest=root/j['id'];dest.mkdir(parents=True,exist_ok=False)
    (dest/'reward.json').write_text(json.dumps(new,indent=2))
    (dest/'aliases.json').write_text(json.dumps(aliases,indent=2))
    out.update(replay='verified',new_reward=new,aliases=len(aliases),missing_reports=missing,input_hashes=hashes,
               output=str(dest),changed=any(new.get(k)!=old.get(k) for k in keys))
    (dest/'audit.json').write_text(json.dumps(out,indent=2))
    return out


def run_all(jobs):
    base=Path.home()/'Downloads/Research/Repair/Codes'
    root=base/'deepswe-runs/c0240-score96-report-id-v1-20261013'
    root.mkdir(exist_ok=False)
    rows=[]
    for j in jobs:
        try:rows.append(process(j,root,base))
        except Exception as e:rows.append({**j,'replay':'error_stop','error':type(e).__name__+': '+str(e)})
    out={'version':VERSION,'rows':rows,'root':str(root)}
    (root/'summary.json').write_text(json.dumps(out,indent=2));return out
