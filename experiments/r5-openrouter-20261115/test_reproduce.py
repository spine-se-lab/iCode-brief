"""Offline checks for new local staging plumbing; never start a model worker."""
import contextlib
import io
import tempfile
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import reproduce


class ReproductionChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def stage(self, host='95'):
        with contextlib.redirect_stdout(io.StringIO()):
            reproduce.stage(self.base, host)
        return self.base / reproduce.RUN_NAME

    def test_original_files_and_round_assignment(self):
        root = self.stage()
        manifest = reproduce.verify(root)
        self.assertEqual(len(manifest['files_sha256']), 90)
        self.assertEqual(reproduce.read(root / 'assignment.json'), {'host': '95', 'rounds': [1, 3]})
        self.assertFalse((root / 'launch-intent.json').exists())
        self.assertFalse(list((root / '.secrets').iterdir()))

    def test_other_host(self):
        root = self.stage('96')
        self.assertEqual(reproduce.read(root / 'assignment.json')['rounds'], [2, 4])

    def test_stage_is_exclusive(self):
        self.stage()
        with self.assertRaises(FileExistsError):
            self.stage()

    def test_tampering_is_rejected(self):
        root = self.stage()
        (root / 'r5_native.py').write_text('# changed\n')
        with self.assertRaisesRegex(ValueError, 'Frozen file differs'):
            reproduce.verify(root)

    def test_launch_marker_blocks_spawn(self):
        root = self.stage()
        (root / 'launch-intent.json').write_text('{}')
        with patch.object(reproduce.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'Already launched'):
                reproduce.launch(self.base, self.base / 'not-read.json')
            spawn.assert_not_called()

    def test_unconfirmed_max_blocks_spawn(self):
        self.stage()
        proof = self.base / 'proof.json'
        proof.write_text('{"passed": false}')
        with patch.object(reproduce.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'upstream max'):
                reproduce.launch(self.base, proof)
            spawn.assert_not_called()

    def test_launch_records_original_harness_and_starts_once(self):
        root = self.stage()
        proof = self.base / 'proof.json'
        reproduce.save(proof, {'passed': True, 'method': 'synthetic offline fixture'})
        reproduce.save(root / 'preflight.json', {'passed': True, 'task_count': 113, 'at': time.time()})
        reproduce.save(root / 'bootstrap-finished.json', {'passed': True})
        for name in ('test_r5_native.py', 'test_wire_audit.py', 'test_run_batch.py',
                     'prepare_tasks.py', 'smoke_container_r5_native.py'):
            reproduce.save(root / (name + '.result.json'), {'returncode': 0})
        reproduce.save(root / 'runtime.json', {'synthetic': True})
        key = root / '.secrets/openrouter.key'
        key.write_text('offline-placeholder')
        key.chmod(0o600)
        commit = reproduce.read(root / 'FROZEN-MANIFEST.json')['upstream_commit']
        with patch.object(reproduce.subprocess, 'check_output', side_effect=['3.14.7', commit, '', '']), \
             patch.object(reproduce.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 2**30)), \
             patch.object(reproduce.subprocess, 'Popen', return_value=SimpleNamespace(pid=123)) as spawn, \
             patch.dict(reproduce.os.environ), contextlib.redirect_stdout(io.StringIO()):
            reproduce.launch(self.base, proof)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[0][1], str(root / 'run_batch.py'))
            self.assertEqual(reproduce.read(root / 'launch.json')['pid'], 123)
            self.assertEqual(reproduce.read(root / 'launch-intent.json')['rounds'], [1, 3])
            self.assertIn('runtime.json', reproduce.read(root / 'harness-sha256.json'))
            with self.assertRaisesRegex(ValueError, 'Already launched'):
                reproduce.launch(self.base, proof)
            spawn.assert_called_once()

    def test_stale_preflight_rejects_launch(self):
        root = self.stage()
        proof = self.base / 'proof.json'
        reproduce.save(proof, {'passed': True})
        reproduce.save(root / 'preflight.json', {'passed': True, 'task_count': 113,
                                                'at': time.time() - 7200})
        with patch.object(reproduce.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'one hour'):
                reproduce.launch(self.base, proof)
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
