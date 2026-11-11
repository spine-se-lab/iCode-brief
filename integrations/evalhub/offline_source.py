"""Build a disposable local Git source from this working tree for offline smoke checks."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def prepare_source(destination):
    destination.mkdir()
    for name in ("src", "integrations"):
        shutil.copytree(ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copyfile(ROOT / name, destination / name)
    subprocess.run(["git", "init", "-q", "-b", "codex/compass-v1.9-evalhub", str(destination)], check=True)
    subprocess.run(
        ["git", "-C", str(destination), "add", "src", "integrations", "pyproject.toml", "uv.lock", ".python-version"],
        check=True,
    )
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
            "-qm",
            "offline source fixture",
        ],
        check=True,
    )
    return destination.as_uri()
