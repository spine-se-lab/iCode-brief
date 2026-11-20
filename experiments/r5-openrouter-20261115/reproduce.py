"""Local packaging/launch commands; archived experiment sources are unchanged.

verify and stage do not call models. bootstrap.py is a separate, model-free step.
launch explicitly starts 226 paid generations on one host. No automatic resume.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

ARCHIVE = Path(__file__).resolve().parent
RUN_NAME = 'icode-r5-openrouter-20261115'


def read(path):
    return json.loads(path.read_text())


def save(path, data):
    with path.open('x') as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')


def verify(directory):
    manifest = read(ARCHIVE / 'FROZEN-MANIFEST.json')
    for name, expected in manifest['files_sha256'].items():
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('Frozen file differs: ' + name)
    return manifest


def stage(base, host):
    verify(ARCHIVE)
    root = base / RUN_NAME
    # mkdir is deliberately exclusive: an interrupted stage needs inspection.
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in read(ARCHIVE / 'FROZEN-MANIFEST.json')['files_sha256']:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ARCHIVE / name, target)
    for name in ('FROZEN-MANIFEST.json', 'reproduce.py'):
        shutil.copyfile(ARCHIVE / name, root / name)
    for directory in ('deepswe-runs', 'official-deepswe-20261020'):
        (base / directory).mkdir(exist_ok=True)
    (root / '.secrets').mkdir(mode=0o700)
    save(root / 'assignment.json', {'host': host, 'rounds': [1, 3] if host == '95' else [2, 4]})
    print(root)


def launch(base, confirmation):
    root = base / RUN_NAME
    manifest = verify(root)
    # Serialize this entrypoint; run_batch.py owns both global locks while running.
    with (root / 'dispatch.lock').open('a') as dispatch:
        fcntl.flock(dispatch, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in ('launch-intent.json', 'launch.json', 'started.json', 'runs'):
            if (root / name).exists():
                raise ValueError('Already launched or uncertain; inspect: ' + name)
        proof = read(confirmation)
        if proof.get('passed') is not True:
            raise ValueError('Supply independently reviewed upstream max confirmation with passed=true')
        preflight = read(root / 'preflight.json')
        if not (preflight['passed'] and preflight['task_count'] == 113
                and 0 <= time.time() - preflight['at'] < 3600):
            raise ValueError('Preflight must be successful and less than one hour old')
        if not read(root / 'bootstrap-finished.json')['passed']:
            raise ValueError('Bootstrap checks failed')
        checks = ('test_r5_native.py', 'test_wire_audit.py', 'test_run_batch.py',
                  'prepare_tasks.py', 'smoke_container_r5_native.py')
        for name in checks:
            if read(root / (name + '.result.json'))['returncode'] != 0:
                raise ValueError('Check failed: ' + name)
        checkout = root / 'iCode'
        python = checkout / '.venv/bin/python'
        def output(args):
            return subprocess.check_output(args, text=True, timeout=30).strip()
        if output([str(python), '-c', 'import sys;print(sys.version.split()[0])']) != '3.14.7':
            raise ValueError('Expected historical Python 3.14.7')
        if output(['git', '-C', str(checkout), 'rev-parse', 'HEAD']) != manifest['upstream_commit']:
            raise ValueError('Wrong upstream commit')
        if output(['git', '-C', str(checkout), 'status', '--porcelain']):
            raise ValueError('Upstream checkout is modified')
        key = root / '.secrets/openrouter.key'
        info = key.lstat()
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0 and info.st_size > 0):
            raise ValueError('Key must be an owner-only regular file')
        os.environ['DOCKER_CONTEXT'] = 'desktop-linux'
        if output(['docker', 'ps', '-q']) or shutil.disk_usage(base).free < 60 * 2**30:
            raise ValueError('Require no running containers and at least 60 GiB free')
        locks = []
        try:
            for name in ('deepswe-runs/r5-verification-global.lock',
                         'official-deepswe-20261020/verification-global.lock'):
                handle = (base / name).open('a')
                locks.append(handle)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            hashes = dict(manifest['files_sha256'])
            for name in ('runtime.json', 'assignment.json'):
                hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
            save(root / 'harness-sha256.json', hashes)
            save(root / 'upstream-max-probe.json', proof)
            assignment = read(root / 'assignment.json')
            if assignment not in ({'host': '95', 'rounds': [1, 3]}, {'host': '96', 'rounds': [2, 4]}):
                raise ValueError('Unexpected round assignment')
            save(root / 'launch-intent.json', {'at': time.time(), **assignment, 'expected': 226})
        finally:
            for handle in locks:
                handle.close()
        with (root / 'batch.log').open('x') as log:
            process = subprocess.Popen([str(python), str(root / 'run_batch.py')], cwd=root,
                                       stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        save(root / 'launch.json', {'at': time.time(), 'pid': process.pid, **assignment})
        print('Started worker', process.pid, 'for', assignment)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('verify')
    prepare = commands.add_parser('stage')
    prepare.add_argument('--base', type=Path, required=True)
    prepare.add_argument('--host', choices=('95', '96'), required=True)
    start = commands.add_parser('launch')
    start.add_argument('--base', type=Path, required=True)
    start.add_argument('--upstream-max-confirmation', type=Path, required=True)
    start.add_argument('--accept-paid-226', action='store_true', required=True)
    args = parser.parse_args()
    if args.command == 'verify':
        manifest = verify(ARCHIVE)
        print('Verified', len(manifest['files_sha256']), 'original files')
    elif args.command == 'stage':
        stage(args.base.expanduser().resolve(), args.host)
    else:
        launch(args.base.expanduser().resolve(), args.upstream_max_confirmation)


if __name__ == '__main__':
    main()
