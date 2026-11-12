"""Build a disposable local Git source from this working tree for offline smoke checks."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def prepare_source(destination):
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            "codex/compass-v1.15-evalhub",
            ROOT.as_uri(),
            str(destination),
        ],
        check=True,
    )
    for name in ("src", "integrations"):
        shutil.copytree(
            ROOT / name,
            destination / name,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    subprocess.run(["git", "-C", str(destination), "add", "src", "integrations"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(destination),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "offline source fixture",
        ],
        check=True,
    )
    return destination.as_uri()
