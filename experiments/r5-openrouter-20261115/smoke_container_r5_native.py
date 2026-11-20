"""Exercise the real headless iCode CLI with scripted HTTP responses, never a network model."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
import httpx
import r5_native as r

root = Path(r.__file__).resolve().parent
os.environ['PATH']='/opt/homebrew/bin:/usr/local/bin:'+os.environ['PATH']
os.environ['DOCKER_CONTEXT']='desktop-linux'
image=json.loads((root/'runtime.json').read_text())['tasks'][json.loads((root/'selection.json').read_text())['tasks'][0]]['image_id']
cid=subprocess.check_output(['docker','run','-d','--label','compass.icode.r5=openrouter-20261115','--platform','linux/amd64','--network','none','--cpus','2','--memory','5g','--entrypoint','bash',image,'-lc','sleep 600'],text=True).strip()
main_bodies, side_bodies = [], []

async def send(client, request, **kwargs):
    assert request.url.host == 'openrouter.ai', 'Unexpected endpoint in offline test'
    body = json.loads(request.content)
    assert body['model'] == r.MODEL
    assert body['provider'] == r.PROVIDER
    if body.get('tools'):
        main_bodies.append(body)
        step = len(main_bodies)
        assert body['reasoning'] == {'effort': 'max'}
        assert body.get('max_tokens') == 64000
        assert body.get('stream', False) is False
        names = {t['function']['name'] for t in body['tools']}
        print(json.dumps({'offline_debug_tools': sorted(names)}), flush=True)
        assert {'execute','start_compass_brief','inspect_compass_brief'} <= names
        assert names <= {'execute','start_compass_brief','inspect_compass_brief','compress_context','list_compressed_contexts','recall_context'}
        calls = {1: ('execute', {'command':'cat /tmp/icode-fixture.py','reason':'Inspect synthetic fixture'}),
                 2: ('start_compass_brief', {'exploration_summary': 'Read fixture.py: parse returns None; issue asks empty list.'}),
                 3: ('execute', {'command':"printf 'def parse(data):\\n    return []\\n' > /tmp/icode-fixture.py", 'reason':'Edit synthetic fixture'})}
        if step in calls:
            name, args = calls[step]
            message = {'role':'assistant','content':'','reasoning':'Synthetic offline reasoning.',
                       'tool_calls':[{'id':f'call{step}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
            finish = 'tool_calls'
        else:
            assert step in (4, 5), f'Unexpected repeated main call: {step}'
            message = {'role':'assistant','content':'Finished synthetic offline task.','reasoning':'Synthetic final reasoning.'}
            finish = 'stop'
        if step == 3:
            assert any(r.controller.constants['SUBMISSION_RULES'] in str(m.get('content')) for m in body['messages'])
            assert any(m.get('reasoning') for m in body['messages'] if m['role'] == 'assistant')
        if step == 5:
            assert any(r.controller.constants['SUBMISSION_CHECK'] in str(m.get('content')) for m in body['messages'])
    else:
        side_bodies.append(body)
        assert body['reasoning'] == {'enabled':False}
        system = body['messages'][0]['content']
        if '"title": "IssueRequirementExtraction"' in system:
            content = {'requirements':[{'id':'R1','text':'Return an empty list for empty input.'}],
                       'constraints':[], 'signature_changes':[]}
        else:
            content = {k:[] for k in ('new_claims','augment','merge','conflicts','contradiction_scan','deactivate')}
        message, finish = {'role':'assistant','content':json.dumps(content)}, 'stop'
    return httpx.Response(200, request=request, json={'id':f'synthetic-{len(main_bodies)}-{len(side_bodies)}',
        'object':'chat.completion','created':0,'model':r.MODEL,'provider':'DeepSeek',
        'choices':[{'index':0,'message':message,'finish_reason':finish}],
        'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,'prompt_cache_hit_tokens':50}})

try:
    with tempfile.TemporaryDirectory(prefix='r5-offline-', dir=root) as temp:
        directory = Path(temp).resolve()
        work = directory / 'work'
        work.mkdir()
        subprocess.run(['docker','exec',cid,'bash','-lc',"printf 'def parse(data):\\n    return None\\n' > /tmp/icode-fixture.py"],check=True)
        task = directory / 'task.txt'
        task.write_text('In fixture.py, make parse(data) return an empty list for empty input. Use the Compass workflow.')
        key = directory / 'synthetic.key'
        r.Artifacts.write_text(key, 'offline-placeholder\n')
        with patch.object(httpx.AsyncClient, 'send', send):
            result = r.main(['--upstream', str(root/'iCode'), '--frozen', str(root/'r5-frozen'),
                             '--directory',str(directory/'trial'), '--task',str(task), '--cwd',str(work), '--key-file',str(key),'--container',cid])
        assert result == 0, result
        assert len(main_bodies) == 5, len(main_bodies)
        assert 'return []' in subprocess.check_output(['docker','exec',cid,'cat','/tmp/icode-fixture.py'],text=True)
        assert not (work/'fixture.py').exists()
        subprocess.run(['docker','rm','-f',cid],check=True,capture_output=True)
        events = [json.loads(line) for line in (directory/'trial/adapter-events.jsonl').read_text().splitlines()]
        assert r.controller.calls == len(main_bodies) == 5
        assert sum(e['event']=='main_request' for e in events) == len(main_bodies)
        assert sum(e['event']=='main_response' for e in events) == len(main_bodies)
        assert sum(e['event']=='submission_check' for e in events) == 1
        assert any(e['event']=='brief_delivered' for e in events)
        assert len(r.controller.core.evidence.snapshot()) == 2
        print(json.dumps({'offline_container_smoke':'passed','main_http_requests_mocked':len(main_bodies),
                          'side_http_requests_mocked':len(side_bodies),'external_model_calls':0,
                          'actual_file_edit':True,'native_reasoning_replay':True,'brief_and_submission_check':True}))
finally:
    subprocess.run(['docker','rm','-f',cid],capture_output=True)
