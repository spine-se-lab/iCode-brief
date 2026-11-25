"""Frozen source verification and manifest serialization helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

UPSTREAM = "9228ec4af94bd6723ef9fb53503c1ea655fb4016"
ARCHIVE = "62c304ae6315ffb1c977def04b77fb9c552ac75ceb533fd249c2b0180818d257"
SOURCE_ROOT = Path(__file__).resolve().parent


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=lambda x: x.to_dict() if hasattr(x, "to_dict") else str(x))


def verify_source(root):
    manifest = json.loads((root / "provenance.json").read_text())
    if manifest["original"]["original_archive_sha256"] != ARCHIVE:
        raise RuntimeError("Unexpected Compass source provenance")
    for name, record in manifest["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError("Compass source mismatch: " + name)
    from compass import session as core

    if Path(core.__file__).resolve() != root / "session.py":
        raise RuntimeError("Wrong Compass core import path")
    return manifest


def source_constants(root):
    manifest = verify_source(root)
    from compass.evaluation import prompts

    values = {name: getattr(prompts, name) for name in manifest["submission_sha256"]}
    if any(sha(value) != manifest["submission_sha256"][name] for name, value in values.items()):
        raise RuntimeError("Compass submission prompts changed")
    return values
