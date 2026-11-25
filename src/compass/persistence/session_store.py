"""Compass-owned session checkpoint, using the existing secure artifact port."""

from __future__ import annotations

import errno
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from compass.ports import SessionArtifactStore
from compass.state import STATE_KEY


class SessionStore:
    """Keep one authoritative checkpoint beside core Claims/Evidence artifacts.

    Only missing files mean a new session. Corrupt or unsafe files must not
    silently discard the task. No iCode history serializer is involved.
    """

    def __init__(self, directory: Path, artifacts: SessionArtifactStore) -> None:
        self.directory = directory
        self.artifacts = artifacts
        self.path = directory / "session.json"

    def _read(self, path: Path) -> Any:
        with self.artifacts.open_verified_binary(path) as stream:
            return json.load(stream)

    @staticmethod
    def _validate(state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
            raise ValueError("Invalid Compass session state version")
        if state.get("core") is not None and not isinstance(state["core"], dict):
            raise ValueError("Invalid Compass core checkpoint")
        for key in ("pending", "user_inputs"):
            if not isinstance(state.get(key, []), list) or not all(
                isinstance(item, dict) for item in state.get(key, [])
            ):
                raise ValueError("Invalid Compass pending/input records")
        for item in state.get("user_inputs", []):
            if not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
                raise ValueError("Invalid Compass original user input")
        return state

    def load(self) -> dict[str, Any] | None:
        try:
            document = self._read(self.path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
            # Import the previously written diagnostic checkpoint once, without deleting it.
            try:
                return self._validate(self._read(self.directory / "tool-state.json"))
            except OSError as legacy_error:
                if legacy_error.errno != errno.ENOENT:
                    raise
                return None
        if not isinstance(document, dict) or document.get("format") != "compass-session-v1":
            raise ValueError("Invalid Compass session checkpoint format")
        state = self._validate(document.get("state"))
        envelope = (state.get("core") or {}).get(STATE_KEY)
        if envelope is not None:
            if not isinstance(envelope, dict) or envelope.get("artifact_directory") != "ledger":
                raise ValueError("Invalid Compass checkpoint artifact location")
            # The extension folder is copied on fork; references must follow that copy.
            envelope["artifact_directory"] = str(self.directory / "ledger")
        return state

    def save(self, state: dict[str, Any]) -> None:
        state = deepcopy(self._validate(state))
        envelope = (state.get("core") or {}).get(STATE_KEY)
        if envelope is not None:
            if not isinstance(envelope, dict) or envelope.get("artifact_directory") != str(self.directory / "ledger"):
                raise ValueError("Compass checkpoint belongs to a different artifact directory")
            envelope["artifact_directory"] = "ledger"
        payload = json.dumps({"format": "compass-session-v1", "state": state}, ensure_ascii=False)
        self.artifacts.write_text(self.path, payload)
