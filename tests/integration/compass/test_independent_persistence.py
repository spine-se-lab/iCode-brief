"""Compass state survives independently of the host history serializer."""

import asyncio
import errno
import json
import shutil

import pytest

from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.storage import Artifacts
from compass.adapters.icode.tool_session import STATE, ToolCompass
from compass.persistence.session_store import SessionStore
from compass.state import STATE_KEY
from tests.integration.compass.support import ROOT_TASK, host_for, read
from tests.integration.compass.test_captured_requirements import update
from tests.integration.compass.test_tool_interface import brief, runtime_for


def reopen(runtime, directory=None, session_id=None):
    restored = ToolCompass(
        runtime.model.profile,
        runtime.cwd,
        directory or runtime.directory,
        session_id=session_id or runtime.session_id,
    )
    restored.bind()  # no native checkpoint
    return restored


@pytest.mark.asyncio
async def test_restart_restores_requirements_inputs_and_pending_evidence_without_host_state(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), read(), MockResponse(text="done")])
    restored = None
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        assert STATE not in host.engine.current.require_loaded().bindings.backend.history_state
        assert (runtime.directory / "session.json").exists()
        await runtime.aclose()
        restored = reopen(runtime)
        await restored.inspect(["goal", "E-001"])
        assert restored.gate.session.goals.snapshot().description
        assert len(restored.gate.session.evidence.snapshot()) == 1
        assert restored.gate.state()[STATE]["user_inputs"][0]["text"] == ROOT_TASK
        assert len(probe.extractions) == 1
        disk = json.loads((restored.directory / "session.json").read_text())
        assert disk["state"]["core"][STATE_KEY]["artifact_directory"] == "ledger"
    finally:
        if restored:
            await restored.aclose()
        await host.shutdown()


@pytest.mark.asyncio
async def test_copied_session_uses_own_raw_evidence_after_source_removed(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [read(), brief(), MockResponse(text="done")])
    restored = None
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        copied = tmp_path / "fork" / "compass"
        await runtime.aclose()
        await asyncio.to_thread(shutil.copytree, runtime.directory, copied)
        await asyncio.to_thread(runtime.directory.rename, tmp_path / "old-session-not-at-original-path")
        restored = reopen(runtime, copied, "fork-session")
        evidence_text = await restored.inspect(["E-001"])
        assert "def f(items)" in evidence_text
        assert (
            json.loads((copied / "session.json").read_text())["state"]["core"][STATE_KEY]["artifact_directory"]
            == "ledger"
        )
    finally:
        if restored:
            await restored.aclose()
        await host.shutdown()


@pytest.mark.asyncio
async def test_legacy_tool_checkpoint_import_keeps_original(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    restored = None
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        legacy = runtime.directory / "tool-state.json"
        payload = json.dumps(runtime.gate.state()[STATE])
        runtime.artifacts.write_text(legacy, payload)
        (runtime.directory / "session.json").unlink()  # fixture for the previous file layout
        await runtime.aclose()
        restored = reopen(runtime)
        await restored.inspect(["goal"])
        assert restored.gate.session.goals.snapshot().description
        assert legacy.read_text() == payload
        assert (runtime.directory / "session.json").exists()
        assert len(probe.extractions) == 1
    finally:
        if restored:
            await restored.aclose()
        await host.shutdown()


@pytest.mark.parametrize("payload", ["{broken", '{"format":"unknown"}', '{"format":"compass-session-v1","state":{}}'])
def test_bad_checkpoint_does_not_silently_start_empty(tmp_path, payload):
    store = SessionStore(tmp_path, Artifacts())
    store.artifacts.write_text(store.path, payload)
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_text() == payload


def test_no_checkpoint_is_new_but_access_error_is_not(tmp_path, monkeypatch):
    artifacts = Artifacts()
    store = SessionStore(tmp_path, artifacts)
    assert store.load() is None

    def denied(path):
        raise OSError(errno.EACCES, "synthetic access failure")

    monkeypatch.setattr(artifacts, "open_verified_binary", denied)
    with pytest.raises(OSError) as error:
        store.load()
    assert error.value.errno == errno.EACCES


def test_checkpoint_cannot_reference_an_external_ledger(tmp_path):
    store = SessionStore(tmp_path, Artifacts())
    payload = {
        "format": "compass-session-v1",
        "state": {"version": 1, "core": {STATE_KEY: {"artifact_directory": "../other"}}},
    }
    store.artifacts.write_text(store.path, json.dumps(payload))
    with pytest.raises(ValueError, match="artifact location"):
        store.load()


def test_write_failure_preserves_last_successful_checkpoint(tmp_path, monkeypatch):
    artifacts = Artifacts()
    store = SessionStore(tmp_path, artifacts)
    initial = {"version": 1, "core": None, "pending": [], "user_inputs": []}
    store.save(initial)

    def denied(path, payload):
        raise OSError(errno.ENOSPC, "synthetic full disk")

    monkeypatch.setattr(artifacts, "write_text", denied)
    with pytest.raises(OSError):
        store.save({**initial, "user_inputs": [{"id": "1", "text": ROOT_TASK}]})
    assert store.load() == initial
