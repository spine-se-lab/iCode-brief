"""Synthetic patch handoff checks; no benchmark data or model requests."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "sandbox_agent.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class SubmissionChecks(unittest.TestCase):
    def test_candidate_survives_platform_export_and_clean_verifier(self):
        for committed in (False, True):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo = root / "repo"
                repo.mkdir()

                def git(*args, cwd=repo, data=None):
                    return subprocess.run(["git", *args], cwd=cwd, input=data, check=True, capture_output=True).stdout

                git("init", "-q")
                git("config", "user.name", "Synthetic Test")
                git("config", "user.email", "test@localhost")
                (repo / ".gitignore").write_text("ignored-*\n")
                (repo / "existing").write_text("old\n")
                (repo / "deleted").write_text("delete me\n")
                (repo / "renamed").write_text("move me\n")
                git("add", "-A")
                git("commit", "-qm", "base")
                base = git("rev-parse", "HEAD").decode().strip()
                (repo / "existing").write_text("changed\n")
                (repo / "deleted").unlink()
                (repo / "renamed").rename(repo / "renamed file")
                (repo / "new file\nwith newline").write_text("new\n")
                (repo / "binary").write_bytes(bytes(range(256)))
                (repo / "executable").write_text("#!/bin/sh\nexit 0\n")
                (repo / "executable").chmod(0o755)
                (repo / "link").symlink_to("existing")
                (repo / "ignored-tracked").write_text("forced tracked\n")
                git("add", "-f", "ignored-tracked")
                git("add", "-A")
                if committed:
                    git("commit", "-qm", "model changes")
                    # This is the failure that the old adapter missed.
                    self.assertEqual(git("diff", "--binary"), b"")
                model_head = git("rev-parse", "HEAD").decode().strip()
                (repo / "existing").write_text("changed plus unfinished edit\n")
                (repo / "untracked").write_text("last new file\n")
                (repo / "ignored-cache").write_text("not a submission\n")
                logs = root / "logs"
                logs.mkdir()
                with patch.object(bootstrap, "ROOT", logs):
                    receipt = bootstrap.finalize_workspace(repo, base, root / "artifacts")
                self.assertGreater(receipt["patch_bytes"], 0)
                self.assertEqual(git("rev-parse", "HEAD").decode().strip(), base)
                self.assertEqual(git("rev-parse", "refs/r5-evalhub/model-head").decode().strip(), model_head)
                self.assertEqual((root / "artifacts/patch.diff").read_bytes(), (logs / "submission.patch").read_bytes())
                self.assertEqual((repo / "ignored-cache").read_text(), "not a submission\n")
                # Simulate a platform overwriting our artifact using either common
                # working-tree export command, then applying it in a fresh verifier.
                for diff_args in [("diff", "--binary"), ("diff", "--binary", "HEAD")]:
                    exported = git(*diff_args)
                    self.assertTrue(exported)
                    with tempfile.TemporaryDirectory(dir=root) as clean:
                        verifier = Path(clean)
                        git("clone", "-q", str(repo), str(verifier))
                        git("checkout", "-q", base, cwd=verifier)
                        git("apply", "--binary", "--index", "-", cwd=verifier, data=exported)
                        self.assertEqual(git("write-tree", cwd=verifier).decode().strip(), receipt["candidate_tree"])
                        self.assertEqual((verifier / "existing").read_text(), "changed plus unfinished edit\n")
                        self.assertFalse((verifier / "ignored-cache").exists())

    def test_unchanged_workspace_is_recorded_as_empty_not_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@localhost",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "base",
                ],
                check=True,
            )
            base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
            with tempfile.TemporaryDirectory(dir=root.parent) as logdir, patch.object(bootstrap, "ROOT", Path(logdir)):
                receipt = bootstrap.finalize_workspace(root, base, Path(logdir) / "artifacts")
                self.assertEqual(receipt["patch_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
