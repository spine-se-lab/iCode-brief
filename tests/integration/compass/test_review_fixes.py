"""New security, read-only-turn and refactor regressions; no provider traffic."""

import asyncio
import hashlib
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from chrys.foundation.models.invocations import InvocationOrigin, PassHandle
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.platform.process import managed_subprocess, wait_for_subprocess
from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env
from chrys.foundation.tool_kinds import set_tool_kind
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.kernel import Message, tool
from chrys.orchestration.invoker.contracts import Failed, Ok, RunIntent, RunRequest, StopCause, UsageDelta
from chrys.orchestration.invoker.evidence import ZERO_COUNT, PassEvidence
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.command_credentials import CommandCredentialAuth, ensure_fresh
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from compass import cli
from compass.adapters.icode.credentials import key_file_template
from compass.adapters.icode.interactive import InteractiveBriefChat, InteractiveCompass
from compass.adapters.icode.interactive_model import SelectedModel
from compass.adapters.icode.storage import Artifacts
from compass.provenance import SOURCE_ROOT, source_constants
from compass.runtime.budget import MAX_CALLS, MAX_SECONDS
from compass.runtime.records import InvalidCompassRun, TurnResult
from compass.runtime.run_state import RunState
from compass.runtime.workflow import CompassRun
from tests.integration.compass.test_interactive import maintenance
from tests.integration.compass.test_interactive_retry import RetryClient
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)


def events(owner):
    return [json.loads(line) for line in (owner.directory / "adapter-events.jsonl").read_text().splitlines()]


def test_cli_key_reaches_auth_but_not_child_environment_or_artifacts(tmp_path, monkeypatch, capsys):
    """Exercise main(), real command resolution/Auth and native shell env with a fake key."""
    dummy = "offline-compass-secret-marker"
    key_file = tmp_path / "key ' {{literal}} $(never-execute) .txt"
    key_file.write_text(dummy, encoding="utf-8")
    key_file.chmod(0o600)
    task = tmp_path / "task.txt"
    task.write_text("Explain the entry point", encoding="utf-8")
    directory = tmp_path / "run"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for name in (
        "CHRYS_CONFIG_DIR",
        "CHRYS_MAX_TRANSIENT_RETRIES",
        "CHRYS_SESSION_TITLE_AUTO",
        "CHRYS_DEBUG_LLM_RAW_HTTP_LOG",
    ):
        monkeypatch.setenv(name, "test-before-main")
    observed = []

    async def execute(request, constants, key):
        assert key == dummy
        assert "OPENROUTER_API_KEY" not in os.environ
        profile = yaml.safe_load((request.directory / "config/models/dee05ee03600.yaml").read_text())
        assert dummy not in json.dumps(profile)
        handle = resolve_env_templates(profile["api_key"], location="test.api_key")

        def transport(req):
            observed.append(req.headers["Authorization"] == "Bearer " + dummy)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport), auth=CommandCredentialAuth([handle])
        ) as client:
            await client.get("https://offline.invalid", headers={"Authorization": "Bearer " + handle})
        # The same environment constructor is used by native pipe and PTY shell tools.
        async with managed_subprocess(
            sys.executable,
            "-c",
            "import os; print('OPENROUTER_API_KEY' in os.environ)",
            env=build_shell_subprocess_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ) as proc:
            stdout, stderr = await wait_for_subprocess(proc.communicate(), timeout=10, process_group_id=proc.pid)
        assert stdout.strip() == b"False" and not stderr
        return TurnResult("done", "offline")

    monkeypatch.setattr(cli, "execute", execute)
    assert (
        cli.main(
            ["--directory", str(directory), "--task", str(task), "--cwd", str(tmp_path), "--key-file", str(key_file)]
        )
        == 0
    )
    assert observed == [True]
    assert "OPENROUTER_API_KEY" not in os.environ
    for path in directory.rglob("*"):
        if path.is_file():
            assert dummy.encode() not in path.read_bytes(), path
    captured = capsys.readouterr()
    assert dummy not in captured.out + captured.err
    intent = json.loads((directory / "run-intent.json").read_text())
    assert (intent["main_budget"], intent["wall_seconds"]) == (MAX_CALLS, MAX_SECONDS)


@pytest.mark.asyncio
async def test_command_credential_rechecks_file_permissions(tmp_path):
    key_file = tmp_path / "changed-key"
    key_file.write_text("offline-never-print-this", encoding="utf-8")
    key_file.chmod(0o644)
    handle = resolve_env_templates(key_file_template(key_file), location="test.api_key")
    with pytest.raises(ValueError) as caught:
        await ensure_fresh([handle])
    assert "offline-never-print-this" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["hello", "read", "invalid_handoff", "blocked_write", "missing_brief", "complete"])
async def test_native_ui_readonly_and_strict_paths(tmp_path, monkeypatch, stream, mode):
    reads, writes = [], []

    @tool
    async def read_file(path: str) -> str:
        """Read a synthetic source file."""
        reads.append(path)
        return "def main(): pass"

    @tool
    async def write_file(path: str, content: str) -> str:
        """Write synthetic code."""
        writes.append(path)
        return "written"

    set_tool_kind(read_file, "filesystem.read")
    set_tool_kind(write_file, "filesystem.write")
    read = MockResponse(tool_calls=[("read_file", "r1", {"path": "main.py"})])
    handoff = MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "main.py inspected"})])
    write = MockResponse(tool_calls=[("write_file", "w1", {"path": "main.py", "content": "fixed"})])
    responses = {
        "hello": [MockResponse(text="hello")],
        "read": [read, MockResponse(text="explanation")],
        "invalid_handoff": [handoff, MockResponse(text="done")],
        "blocked_write": [write, MockResponse(text="done")],
        "missing_brief": [read, handoff, MockResponse(text="done")],
        "complete": [read, handoff, write, MockResponse(text="done"), MockResponse(text="checked")],
    }[mode]
    # RetryClient with no failures still uses the real native mock transport.
    client = RetryClient(responses, failures=())
    _patch_runtime(monkeypatch, [client], [read_file, write_file])
    maintain = AsyncMock(side_effect=maintenance)

    # Class replacement is a function so native method binding remains intact.
    async def complete(self, **kwargs):
        return await maintain(self, **kwargs)

    monkeypatch.setattr(SelectedModel, "complete", complete)
    if mode == "missing_brief":
        monkeypatch.setattr(InteractiveBriefChat, "observe_request", lambda self, messages: None)
    settings, models = _make_settings_and_models()
    models.get("mock-profile").stream = stream
    profile = _make_profile()
    profile.metadata["compass_enabled"] = True
    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=_make_agent_registry(profile),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        surface=SessionSurface.TUI,
    )
    try:
        if mode in {"invalid_handoff", "blocked_write", "missing_brief"}:
            with pytest.raises(RuntimeError, match="not a valid Compass treatment"):
                await host.run_until_final("task", timeout=10)
        else:
            result = await host.run_until_final(
                "Explain the entry point" if mode != "complete" else "Fix main", timeout=10
            )
            assert result.text == ("checked" if mode == "complete" else responses[-1].text)
        owner = host.engine.current.require_loaded().bindings.compass.owner
        assert len(client.requests) == len(responses)
        audit = events(owner)
        if mode in {"hello", "read"}:
            assert maintain.await_count == 0
            assert not owner.flags.started and not owner.flags.check_done
            assert any(e["event"] == "compass_skipped" for e in audit)
            assert not any(e["event"] in {"brief_delivered", "submission_check"} for e in audit)
        elif mode == "complete":
            assert writes == ["main.py"] and owner.flags.check_done and owner.flags.handoff_seen
            assert len([e for e in audit if e["event"] == "submission_check"]) == 1
        else:
            assert writes == []
            assert not any(e["event"] == "compass_skipped" for e in audit)
    finally:
        await host.shutdown()


def success():
    return Ok(
        handle=PassHandle("t", "p"),
        usage=UsageDelta(),
        effects=PassEvidence("t", "p", ZERO_COUNT, ZERO_COUNT, ZERO_COUNT, False),
        stop=StopCause.COMPLETED,
        continuation=None,
        segments=(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("calls, held", [(234, True), (235, True), (236, False), (250, False)])
async def test_headless_and_ui_share_exact_submission_decision(tmp_path, calls, held):
    constants = source_constants(SOURCE_ROOT)
    directory = tmp_path / "headless"
    directory.mkdir()
    state = RunState("task", tmp_path, directory, constants, artifacts=Artifacts())
    state.flags.started = state.flags.handoff_seen = state.flags.rules_seen = True
    state.calls = calls
    state.core.maintain = AsyncMock()
    backend = SimpleNamespace(start=AsyncMock(), run=AsyncMock(return_value=TurnResult("done", "s")))
    await CompassRun(state, backend).run()
    request = RunRequest([Message("user", ["task"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "t", None))
    ui = InteractiveCompass(ModelProfile(id="test", name="Test"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    bindings = SimpleNamespace(backend=SimpleNamespace(history_state={}, run=AsyncMock(return_value=success())))
    ui.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    try:
        await ui.begin(request)
        ui.owner.calls = calls
        ui.owner.flags.started = ui.owner.flags.handoff_seen = ui.owner.flags.rules_seen = True
        ui.owner.core.maintain = AsyncMock()
        await ui.run(request)
        assert backend.run.await_count == bindings.backend.run.await_count == 1 + held
        left = [e for e in events(state) if e["event"] == "submission_check"]
        right = [e for e in events(ui.owner) if e["event"] == "submission_check"]
        assert len(left) == len(right) == 1
        assert left[0]["text"] == right[0]["text"]
        assert left[0]["held"] == right[0]["held"] == held
    finally:
        await ui.aclose()


@pytest.mark.asyncio
async def test_headless_still_requires_handoff_for_readonly(tmp_path):
    state = RunState("Explain README", tmp_path, tmp_path, source_constants(SOURCE_ROOT), artifacts=Artifacts())
    backend = SimpleNamespace(start=AsyncMock(), run=AsyncMock(return_value=TurnResult("answer", "s")))
    with pytest.raises(InvalidCompassRun):
        await CompassRun(state, backend).run()
    assert backend.run.await_count == 1
    assert not state.flags.check_done


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["handoff_requested", "write_requested"])
async def test_attempt_flags_survive_restore_and_prevent_readonly_bypass(tmp_path, flag):
    request = RunRequest([Message("user", ["task"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "t", None))
    bindings = SimpleNamespace(
        backend=SimpleNamespace(history_state={}, run=AsyncMock(return_value=success())),
        report_policy_failure=AsyncMock(),
    )
    first = InteractiveCompass(ModelProfile(id="test", name="Test"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    second = InteractiveCompass(ModelProfile(id="test", name="Test"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    first.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    second.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    try:
        await first.begin(request)
        setattr(first.owner.flags, flag, True)
        first.owner.save()
        bindings.backend.history_state = json.loads(json.dumps(bindings.backend.history_state))
        outcome = await second.run(request)
        assert isinstance(outcome, Failed) and isinstance(outcome.exception, InvalidCompassRun)
        bindings.report_policy_failure.assert_awaited_once()
        assert getattr(second.owner.flags, flag) is True
    finally:
        await first.aclose()
        await second.aclose()


def test_frozen_core_and_prompt_hashes_still_match_manifest():
    manifest = json.loads((SOURCE_ROOT / "provenance.json").read_text())
    assert len(manifest["files"]) == 36
    for name, record in manifest["files"].items():
        assert hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest() == record["sha256"], name
    values = source_constants(SOURCE_ROOT)
    assert {name: hashlib.sha256(text.encode()).hexdigest() for name, text in values.items()} == manifest[
        "submission_sha256"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_readonly_native_retry_then_next_turn_can_start_compass(tmp_path, monkeypatch, stream):
    from dataclasses import replace

    from chrys.orchestration.engine.run.bindings import TurnBindings

    @tool
    async def read_file(path: str) -> str:
        """Read synthetic source."""
        return "def f(items): return list(items)"

    set_tool_kind(read_file, "filesystem.read")
    client = RetryClient(
        [
            MockResponse(text="explanation"),
            MockResponse(tool_calls=[("read_file", "r1", {"path": "a.py"})]),
            MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})]),
            MockResponse(text="implemented"),
            MockResponse(text="checked"),
        ],
        failures={1},
    )
    _patch_runtime(monkeypatch, [client], [read_file])
    monkeypatch.setattr(SelectedModel, "complete", maintenance)
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    settings, models = _make_settings_and_models()
    settings = replace(settings, max_transient_retries=1)
    models.get("mock-profile").stream = stream
    profile = _make_profile()
    profile.metadata["compass_enabled"] = True
    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=_make_agent_registry(profile),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        surface=SessionSurface.TUI,
    )
    try:
        first = await host.run_until_final("Explain the entry point", timeout=10)
        assert first.text == "explanation" and len(client.requests) == 2
        bridge = host.engine.current.require_loaded().bindings.compass
        first_owner = bridge.owner
        assert first_owner.calls == 2 and not first_owner.flags.started
        second = await host.run_until_final("Now fix the code", timeout=10)
        assert second.text == "checked" and len(client.requests) == 6
        assert bridge.owner.invocation_id != first_owner.invocation_id
        assert bridge.owner.calls == 4 and bridge.owner.flags.handoff_seen and bridge.owner.flags.check_done
        assert any(e["event"] == "compass_skipped" for e in events(first_owner))
        assert not any(e["event"] == "compass_skipped" for e in events(bridge.owner))
    finally:
        await host.shutdown()


def test_prepare_only_is_credential_free(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def forbid_read(_path):
        raise AssertionError("prepare-only must not read a credential")

    monkeypatch.setattr(cli, "read_key", forbid_read)
    assert cli.main(["--prepare-only", "--directory", str(tmp_path / "prepared")]) == 0
    profile = yaml.safe_load((tmp_path / "prepared/config/models/dee05ee03600.yaml").read_text())
    assert profile["api_key"] == ""
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0


def test_cli_failure_does_not_echo_key_or_exception_text(tmp_path, monkeypatch, capsys):
    dummy = "offline-error-secret-marker"
    key_file = tmp_path / "key"
    key_file.write_text(dummy, encoding="utf-8")
    key_file.chmod(0o600)
    task = tmp_path / "task"
    task.write_text("task", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for name in (
        "CHRYS_CONFIG_DIR",
        "CHRYS_MAX_TRANSIENT_RETRIES",
        "CHRYS_SESSION_TITLE_AUTO",
        "CHRYS_DEBUG_LLM_RAW_HTTP_LOG",
    ):
        monkeypatch.setenv(name, "test-before-main")

    async def fail(request, constants, key):
        raise RuntimeError(key)

    monkeypatch.setattr(cli, "execute", fail)
    directory = tmp_path / "failed"
    assert (
        cli.main(
            ["--directory", str(directory), "--task", str(task), "--cwd", str(tmp_path), "--key-file", str(key_file)]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert dummy not in captured.out + captured.err
    assert "OPENROUTER_API_KEY" not in os.environ
    for path in directory.rglob("*"):
        if path.is_file():
            assert dummy.encode() not in path.read_bytes()
