"""Two assigned rounds per host, one generation and verification per task/round."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
os.umask(0o077)
os.environ['PATH'] = '/opt/homebrew/bin:/usr/local/bin:' + os.environ['PATH']
os.environ['DOCKER_CONTEXT'] = 'desktop-linux'


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args, timeout=30):
    return subprocess.check_output(args, timeout=timeout)


def valid_score(result):
    revision = result.get('score_revision') or {}
    reward = revision.get('new_reward') or {}
    return (revision.get('replay') in ('verified', 'apply_failed_retained')
            and not revision.get('missing_reports')
            and reward.get('reward') in (0, 1) and result.get('exit_code') == 0)


def dispatch_ready(pending, busy, capacity, base_for):
    selected = []
    for item in list(pending):
        if len(selected) >= capacity:
            break
        base = base_for(item)
        if base not in busy:
            busy.add(base)
            pending.remove(item)
            selected.append(item)
    return selected


def main():
    assert (ROOT / 'launch-intent.json').exists()
    guards = []
    for path in [ROOT / 'worker.lock', BASE / 'deepswe-runs/r5-verification-global.lock',
                 BASE / 'official-deepswe-20261020/verification-global.lock']:
        handle = path.open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guards.append(handle)
    assert shutil.disk_usage(ROOT).free >= 60 * 2**30
    for name, expected in read(ROOT / 'harness-sha256.json').items():
        assert digest(ROOT / name) == expected, 'harness_changed:' + name
    cfg = read(ROOT / 'runtime.json')
    assignment = read(ROOT / 'assignment.json')
    host, rounds = assignment['host'], assignment['rounds']
    tasks = read(ROOT / 'selection.json')['tasks']
    assert len(tasks) == len(set(tasks)) == 113 and len(rounds) == 2
    evaluation = Path(cfg['eval'])
    sys.path.insert(0, str(evaluation))
    spec = importlib.util.spec_from_file_location('isolated_grader', evaluation / 'central_worker.py')
    grader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grader)
    runs = ROOT / 'runs'
    runs.mkdir(exist_ok=False)
    save(ROOT / 'started.json', {'pid': os.getpid(), 'at': time.time(), 'host': host,
                                'rounds': rounds, 'expected': 226})
    rows, active = [], {}
    state_lock = threading.Lock()
    grade_slots = threading.Semaphore(2)
    stop = threading.Event()

    def status(phase='running'):
        with state_lock:
            save(ROOT / 'status.json', {
                'at': time.time(), 'phase': phase, 'host': host, 'expected': 226,
                'terminal': len(rows), 'passed': sum(r['status'] == 'pass' for r in rows),
                'failed': sum(r['status'] == 'fail' for r in rows),
                'unscored': sum(r['status'] == 'unscored' for r in rows),
                'active': dict(active), 'not_started': 226 - len(rows) - len(active), 'rows': list(rows),
            })

    def trial(round_number, index, task):
        slot = f'or1115-r{round_number}-{index:03d}'
        directory = runs / slot
        directory.mkdir(exist_ok=False)
        work = directory / 'workspace'
        work.mkdir()
        meta = cfg['tasks'][task]
        row = {'slot': slot, 'task': task, 'round': round_number, 'source_host': host,
               'started_at': time.time()}
        container = None
        with state_lock:
            active[slot] = {'task': task, 'round': round_number, 'phase': 'generation'}
        status()
        try:
            assert shutil.disk_usage(ROOT).free >= 15 * 2**30, 'generation_disk_floor'
            assert digest(ROOT / 'inputs' / task / 'instruction.md') == meta['instruction_sha256']
            container = command([
                'docker', 'run', '-d', '--name', slot, '--label', 'compass.icode.r5=openrouter-20261115',
                '--platform', 'linux/amd64', '--network', 'none', '--cap-drop', 'NET_RAW',
                '--cpus', '2', '--memory', '5g', '--entrypoint', 'bash', meta['image_id'], '-lc', 'sleep 18000',
            ]).decode().strip()
            save(directory / 'container.json', {'id': container, 'image': meta['image_id']})
            assert command(['docker', 'exec', '-w', '/app', container, 'git', 'rev-parse', 'HEAD']).decode().strip() == meta['base_commit']
            env = dict(os.environ, PYTHONPATH=str(ROOT) + ':' + str(ROOT / 'r5-frozen/src'))
            for key in ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'):
                env.pop(key, None)
            args = [cfg['python'], '-c', 'import r5_native; raise SystemExit(r5_native.main())',
                    '--upstream', str(ROOT / 'iCode'), '--frozen', str(ROOT / 'r5-frozen'),
                    '--directory', str(directory / 'agent'), '--task', str(ROOT / 'inputs' / task / 'instruction.md'),
                    '--cwd', str(work), '--key-file', str(ROOT / '.secrets/openrouter.key'), '--container', container]
            start = time.time()
            with (directory / 'generation.log').open('x') as log:
                process = subprocess.Popen(args, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                save(directory / 'generation-process.json', {'pid': process.pid, 'started_at': start})
                try:
                    rc = process.wait(timeout=11400)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    rc = 124
            row.update(generation_exit_code=rc, generation_seconds=time.time() - start,
                       generation_exception=None if rc == 0 else 'generation_exit_' + str(rc))
            path = directory / 'agent/adapter-events.jsonl'
            events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            delivered = next((i for i, event in enumerate(events) if event['event'] == 'brief_delivered'), None)
            row['integration'] = {
                'main_calls': sum(e['event'] == 'main_request' for e in events),
                'response_models': sorted({e.get('model') or '<missing>' for e in events if e['event'] == 'main_response'}),
                'providers': sorted({e.get('provider') or '<missing>' for e in events if e['event'] == 'wire_response'}),
                'brief_consumed': delivered is not None and any(e['event'] == 'main_response' for e in events[delivered + 1:]),
                'isolation': any(e['event'] == 'isolation_verified' for e in events),
                'configuration_violation': any(e['event'] == 'configuration_violation' for e in events),
            }
            row['budget_exhausted'] = any(e['event'] == 'budget_exhausted' for e in events)
            row['r5_valid'] = row['integration']['brief_consumed'] and row['integration']['isolation'] and not row['integration']['configuration_violation']
            if row['integration']['configuration_violation'] or not row['integration']['isolation']:
                stop.set()
            patch = command(['docker', 'exec', '-w', '/app', container, 'bash', '-lc',
                             'git add -N . && git diff --binary ' + meta['base_commit']], timeout=60)
            (directory / 'model.patch').write_bytes(patch)
            row['patch_sha256'] = digest(directory / 'model.patch')
            subprocess.run(['docker', 'rm', '-f', container], check=True, capture_output=True)
            container = None
            with state_lock:
                active[slot]['phase'] = 'verification'
            status()
            job = evaluation / 'jobs' / slot
            artifacts = job / (host + '-' + task) / 'artifacts'
            artifacts.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(directory / 'model.patch', artifacts / 'model.patch')
            save(job / 'job.json', {**row, 'candidate_id': slot, 'tests_sha256': meta['tests_sha256']})
            with grade_slots:
                assert shutil.disk_usage(ROOT).free >= 25 * 2**30, 'verification_disk_floor'
                result = grader.grade(job)
            row.update(verification_result=str(job / 'result.json'), score_revision=result.get('score_revision'),
                       diagnostic=result.get('diagnostic'))
            row['patch_status'] = ('pass' if result['score_revision']['new_reward']['reward'] == 1 else 'fail') if valid_score(result) else 'unscored'
            row['status'] = row['patch_status'] if row['r5_valid'] else 'unscored'
        except Exception as exc:
            row.update(status='unscored', diagnostic=type(exc).__name__ + ': ' + str(exc))
            stop.set()
        finally:
            if container:
                subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
            row['finished_at'] = time.time()
            save(directory / 'result.json', row)
            with state_lock:
                active.pop(slot, None)
        return row

    for round_number in rounds:
        if stop.is_set():
            break
        pending = [(round_number, index, task) for index, task in enumerate(tasks, 1)]
        busy, running = set(), {}
        base_for = lambda item: cfg['tasks'][item[2]]['base_registry_ref']
        status()
        with ThreadPoolExecutor(max_workers=4) as pool:
            while running or (pending and not stop.is_set()):
                if not stop.is_set():
                    for item in dispatch_ready(pending, busy, 4 - len(running), base_for):
                        running[pool.submit(trial, *item)] = item
                if not running:
                    break
                finished, _ = wait(running, timeout=5, return_when=FIRST_COMPLETED)
                for future in finished:
                    item = running.pop(future)
                    result = future.result()
                    with state_lock:
                        rows.append(result)
                    busy.remove(base_for(item))
                status()
    status('complete' if len(rows) == 226 else 'blocked')
    save(ROOT / 'finished.json', {'at': time.time(), 'terminal': len(rows), 'expected': 226})


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        save(ROOT / 'scheduler-error.json', {'at': time.time(), 'error_type': type(exc).__name__, 'message': str(exc)})
        raise
