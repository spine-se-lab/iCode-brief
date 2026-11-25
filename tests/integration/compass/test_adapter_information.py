"""New cases for the unified adapter input boundary; no external model calls."""

import asyncio
import json
from dataclasses import replace

import pytest
import pytest_asyncio

from chrys.extensions import ToolCall, ToolContext, ToolError, ToolResult
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.invoker.origin import current_invocation_origin
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.information import InformationInput
from compass.adapters.icode.tools import extension
from compass.gate.controller import STATE
from tests.integration.compass.support import ROOT_TASK, host_for, read
from tests.integration.compass.test_captured_requirements import update
from tests.integration.compass.test_independent_persistence import reopen
from tests.integration.compass.test_tool_interface import brief, results, runtime_for


def records(runtime):
    with runtime.artifacts.open_verified_binary(runtime.information.path) as stream:
        return [json.loads(line) for line in stream if line.strip()]


def origin_for(runtime, **changes):
    return InvocationOrigin(
        **{
            "kind": "turn",
            "session_id": runtime.session_id,
            "invocation_id": "test-turn",
            "parent": None,
            **changes,
        }
    )


def call_for(runtime, kind="shell", arguments=None):
    context = ToolContext.for_testing(
        runtime.cwd,
        session_data_dir=runtime.directory,
        session_id=runtime.session_id,
        agent_name="ExampleAgent",
    )
    context = replace(context, workspace_dirs=(runtime.cwd, runtime.cwd / "second"))
    return ToolCall(name="example", kind=kind, call_id="example-call", arguments=arguments or {}, context=context)


def result_for(**changes):
    return ToolResult(**{"text": "observed", "failed": False, "rejected": False, "duration_ms": 237, **changes})


async def deliver(runtime, call, result, origin=None):
    token = current_invocation_origin.set(origin or origin_for(runtime))
    try:
        await extension.after_tool_hooks[0].func(call, result)
    finally:
        current_invocation_origin.reset(token)


@pytest_asyncio.fixture
async def attached(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="ready")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        yield runtime_for(host), probe
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_native_context_and_tools_reach_one_input_boundary(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [update(), read(), update(), brief(), MockResponse(text="done")],
    )
    try:
        assert (await host.run_until_final(ROOT_TASK, timeout=10)).text == "done"
        runtime = runtime_for(host)
        entries = records(runtime)
        turn = next(item for item in entries if item["source"] == "turn_context")
        assert turn["prompt"] == ROOT_TASK
        assert turn["origin"]["kind"] == "turn"
        assert turn["context"]["session_id"] == runtime.session_id
        assert turn["context"]["workspace_dirs"][0] == str(tmp_path)
        assert turn["model"]["model_id"] == runtime.model.profile.model_id
        tools = [item for item in entries if item["source"] == "after_tool"]
        assert len(tools) == 4
        assert sum(item["evidence_eligible"] for item in tools) == 1
        assert any(item["disposition"] == "compass_feedback_not_repository_evidence" for item in tools)
        evidence = runtime.gate.session.evidence.snapshot()
        assert len(evidence) == 1
        assert evidence[0].invocation_id == turn["origin"]["invocation_id"]
        assert evidence[0].tool_call_id
        assert len(probe.extractions) == 2
        # Requirements changed after exploration, so the Brief call reassesses coverage.
        assert any('"maintenance_due": true' in text for text in results(client))
        outcome = next(json.loads(text) for text in results(client) if text.startswith('{"outcome"'))
        assert not outcome["maintenance_error"]
        claims = runtime.gate.session.claims.snapshot()
        assert claims.state.maintenance_degraded_after_step is None
        assert claims.state.processed_evidence_seq == 1
        assert set(claims.state.requirement_coverage) == {"R1"}

    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["filesystem.read", "filesystem.write", "search", "shell", "mcp", "extension.example", ""]
)
async def test_hook_preserves_all_public_tool_fields_and_only_maps_known_kinds(attached, kind):
    runtime, probe = attached
    call = call_for(runtime, kind, {"path": "a.py", "command": "pwd"})
    result = result_for(text="source result\n[exit_code: 0]")
    await deliver(runtime, call, result)
    record = records(runtime)[-1]
    assert record["tool"] == {"name": call.name, "kind": kind, "call_id": call.call_id, "arguments": call.arguments}
    assert record["result"] == {"text": result.text, "failed": False, "rejected": False, "duration_ms": 237}
    assert record["context"]["workspace_dirs"] == [str(runtime.cwd), str(runtime.cwd / "second")]
    assert record["context"]["agent_name"] == "ExampleAgent"
    assert record["origin"]["invocation_id"] == "test-turn"
    pending = runtime.gate.state()[STATE]["pending"]
    expected = kind in {"filesystem.read", "filesystem.write", "search", "shell"}
    assert record["evidence_eligible"] is expected
    assert len(pending) == int(expected)
    if expected:
        assert pending[0]["invocation_id"] == "test-turn"
        assert pending[0]["tool_call_id"] == "example-call"
        assert pending[0]["arguments"] == call.arguments
    assert not probe.prompts


@pytest.mark.asyncio
async def test_rejected_call_is_audited_but_never_becomes_executed_evidence(attached):
    runtime, _ = attached
    await deliver(runtime, call_for(runtime, "filesystem.write"), result_for(failed=True, rejected=True))
    entry = records(runtime)[-1]
    assert entry["result"]["rejected"] and not entry["evidence_eligible"]
    assert entry["disposition"] == "rejected_not_executed"
    assert not runtime.gate.state()[STATE]["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("workdir", [None, "subdir", "subdir/../other", "/tmp"])
async def test_shell_uses_actual_workdir_parameter_and_native_path_rules(attached, workdir):
    runtime, _ = attached
    arguments = {"command": "pwd"}
    if workdir is not None:
        arguments["workdir"] = workdir
    await deliver(runtime, call_for(runtime, arguments=arguments), result_for(text="ok\n[exit_code: 0]"))
    expected = {
        None: str(runtime.cwd),
        "subdir": str(runtime.cwd / "subdir"),
        "subdir/../other": str(runtime.cwd / "other"),
        "/tmp": "/tmp",
    }[workdir]
    record = records(runtime)[-1]
    pending = runtime.gate.state()[STATE]["pending"][-1]
    assert record["working_dir"] == pending["working_dir"] == expected
    assert record["working_dir_source"] == ("context.cwd" if workdir is None else "shell.workdir_resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,failed,code",
    [("running in background", False, None), ("error", True, None), ("failed\n[exit_code: 2]", True, 2)],
)
async def test_missing_exit_code_stays_unknown_and_failure_is_preserved(attached, text, failed, code):
    runtime, _ = attached
    await deliver(runtime, call_for(runtime), result_for(text=text, failed=failed))
    item = records(runtime)[-1]
    assert item["exit_code"] == code
    assert item["exit_code_source"] == ("not_provided" if code is None else "result_text_suffix")
    assert runtime.gate.state()[STATE]["pending"][-1]["failed"] is failed


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["child", "wrong_session", "wrong_directory", "missing_origin"])
async def test_input_rejects_foreign_or_unidentified_call(attached, invalid):
    runtime, _ = attached
    before = records(runtime)
    call = call_for(runtime)
    origin = origin_for(runtime)
    if invalid == "child":
        origin = origin_for(runtime, kind="sub_agent")
    elif invalid == "wrong_session":
        origin = origin_for(runtime, session_id="another")
    elif invalid == "wrong_directory":
        call = replace(
            call,
            context=ToolContext.for_testing(
                runtime.cwd, session_id=runtime.session_id, session_data_dir=runtime.directory / "another"
            ),
        )
    else:
        origin = None
    with pytest.raises(ToolError, match="current main session"):
        runtime.information.tool_result(call, result_for(), origin)
    assert records(runtime) == before
    assert not runtime.gate.state()[STATE]["pending"]


@pytest.mark.asyncio
async def test_observation_does_not_wait_for_maintenance_and_audit_failure_keeps_result(attached, monkeypatch):
    runtime, _ = attached

    def fail(path, payload):
        raise OSError("synthetic disk error")

    monkeypatch.setattr(runtime.artifacts, "append_text", fail)
    async with runtime.gate.lock:
        await asyncio.wait_for(deliver(runtime, call_for(runtime), result_for()), timeout=1)
    assert len(runtime.gate.state()[STATE]["pending"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_retry_and_restart_keep_original_input_and_evidence_identity(tmp_path, monkeypatch, stored):
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [read(), MockResponse(text="done")], failures={1}, stored=stored
    )
    restored = None
    try:
        assert (await host.run_until_final(ROOT_TASK, timeout=10)).text == "done"
        runtime = runtime_for(host)
        assert len(runtime.gate.state()[STATE]["user_inputs"]) == 1
        recorded = runtime.gate.session.evidence.snapshot()[0]
        assert recorded.invocation_id
        await runtime.aclose()
        restored = reopen(runtime)
        await restored.inspect(["E-001"])
        assert restored.gate.session.evidence.snapshot()[0].invocation_id == recorded.invocation_id
        assert records(restored)
        assert not probe.prompts
    finally:
        if restored:
            await restored.aclose()
        await host.shutdown()


@pytest.mark.asyncio
async def test_model_audit_whitelists_identity_not_credentials(attached):
    runtime, _ = attached
    profile = replace(
        runtime.model.profile, api_key="sentinel-do-not-log", http_headers='{"Authorization": "sentinel-header"}'
    )
    source = InformationInput(runtime.gate, runtime.artifacts, profile)
    source.user_input(ROOT_TASK, origin_for(runtime))
    record = records(runtime)[-1]
    assert record["model"] == {"profile_id": profile.id, "provider": profile.provider, "model_id": profile.model_id}
    assert "sentinel" not in json.dumps(record)
