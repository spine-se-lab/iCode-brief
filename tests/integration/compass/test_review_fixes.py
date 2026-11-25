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

from chrys.foundation.models.invocations import PassHandle
from chrys.foundation.platform.process import managed_subprocess, wait_for_subprocess
from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.orchestration.invoker.contracts import Ok, StopCause, UsageDelta
from chrys.orchestration.invoker.evidence import ZERO_COUNT, PassEvidence
from chrys.service.command_credentials import CommandCredentialAuth, ensure_fresh
from compass import cli
from compass.adapters.icode.credentials import key_file_template
from compass.adapters.icode.storage import Artifacts
from compass.evaluation.budget import MAX_CALLS, MAX_SECONDS
from compass.evaluation.records import InvalidCompassRun, TurnResult
from compass.evaluation.run_state import RunState
from compass.evaluation.workflow import CompassRun
from compass.provenance import SOURCE_ROOT, source_constants


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
async def test_headless_still_requires_handoff_for_readonly(tmp_path):
    state = RunState("Explain README", tmp_path, tmp_path, source_constants(SOURCE_ROOT), artifacts=Artifacts())
    backend = SimpleNamespace(start=AsyncMock(), run=AsyncMock(return_value=TurnResult("answer", "s")))
    with pytest.raises(InvalidCompassRun):
        await CompassRun(state, backend).run()
    assert backend.run.await_count == 1
    assert not state.flags.check_done


def test_frozen_core_and_prompt_hashes_still_match_manifest():
    manifest = json.loads((SOURCE_ROOT / "provenance.json").read_text())
    assert len(manifest["files"]) == 36
    for name, record in manifest["files"].items():
        assert hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest() == record["sha256"], name
    values = source_constants(SOURCE_ROOT)
    assert {name: hashlib.sha256(text.encode()).hexdigest() for name, text in values.items()} == manifest[
        "submission_sha256"
    ]


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
