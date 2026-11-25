"""Tool-only Compass behavior through native host calls; no external models."""

import asyncio
import json
from dataclasses import replace

import pytest

from chrys.extensions import ToolCall, ToolContext, ToolResult
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.invoker.origin import current_invocation_origin
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.tool_session import _SESSIONS, STATE, ToolCompass
from compass.adapters.icode.tools import extension
from compass.provenance import SOURCE_ROOT, source_constants
from tests.integration.compass.support import host_for, read, write


def update():
    return MockResponse(tool_calls=[("update_compass_requirements", "requirements", {})])


def brief():
    return MockResponse(tool_calls=[("update_compass_brief", "brief", {})])


def query(refs):
    return MockResponse(tool_calls=[("inspect_compass_brief", "query", {"refs": refs})])


def runtime_for(host):
    return next(value for value in _SESSIONS.values() if value.session_id == host.engine.session_id)


def results(client):
    return [str(c.result) for m in client.call_history[-1][0] for c in m.contents if c.type == "function_result"]


@pytest.mark.asyncio
async def test_tools_are_exactly_three_and_host_can_write_without_handoff(tmp_path, monkeypatch):
    assert {t.name for t in extension.tools} == {
        "inspect_compass_brief",
        "update_compass_requirements",
        "update_compass_brief",
    }
    assert not extension.before_tool_hooks
    host, client, probe, _, writes = await host_for(tmp_path, monkeypatch, [write(), MockResponse(text="done")])
    try:
        result = await host.run_until_final("Fix it", timeout=10)
        assert result.text == "done" and writes == ["a.py"]
        assert len(client.call_history) == 2  # no added submission pass
        assert probe.prompts == []  # observation does not call maintenance
        runtime = runtime_for(host)
        assert not runtime.gate.state()[STATE]["pending"]
        assert len(runtime.gate.session.evidence.snapshot()) == 1
        assert not any("Compass workflow:" in m.text for m in client.call_history[0][0])
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_query_is_read_only_and_missing_task_does_not_fail_turn(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [query(["goal", "R1", "K1", "C-001", "E-001"]), brief(), MockResponse(text="done")]
    )
    try:
        result = await host.run_until_final("Explain", timeout=10)
        assert result.text == "done" and len(probe.extractions) == 1
        payloads = [json.loads(text) for text in results(client) if text.startswith('{"outcome"')]
        assert payloads[0]["requirements_initialized"] is True
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_one_tool_creates_and_updates_contract_without_dropping_evidence(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [
            update(),
            read(),
            update(),
            query(["goal", "R1", "E-001"]),
            MockResponse(text="done"),
        ],
    )
    try:
        result = await host.run_until_final("Fix empty input", timeout=10)
        assert result.text == "done"
        assert len(probe.extractions) == 2
        runtime = runtime_for(host)
        assert len(runtime.gate.session.evidence.snapshot()) == 1
        assert any("def f(items)" in text for text in results(client))
        assert runtime.gate.session.claims.snapshot().state.last_maintenance_attempt_step == 0
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_brief_tool_waits_then_maintains_once_at_core_threshold(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [update(), read(), brief(), read(), read(), read(), brief(), brief(), MockResponse(text="done")],
    )
    try:
        result = await host.run_until_final("Fix empty input", timeout=10)
        assert result.text == "done"
        payloads = [json.loads(text) for text in results(client) if text.startswith('{"outcome"')]
        assert len(payloads) == 3
        assert payloads[0]["maintenance_due"] is False
        assert payloads[1]["maintenance_due"] is False  # automatic hook already maintained
        assert any("[Compass automatic check]" in text for text in results(client))
        assert runtime_for(host).gate.session.claims.snapshot().state.processed_evidence_seq == 4
        assert payloads[2]["maintenance_due"] is False
        assert len(probe.extractions) == 1
        assert len(runtime_for(host).gate.session.evidence.snapshot()) == 4
        assert runtime_for(host).gate.session.claims.snapshot().state.last_maintenance_attempt_step == 4
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_automatic_hook_maintains_once_when_observations_are_due(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [update(), read(), read(), read(), read(), MockResponse(text="done")]
    )
    try:
        await host.run_until_final("Fix empty input", timeout=10)
        assert len(probe.extractions) == 1 and len(probe.prompts) == 2
        runtime = runtime_for(host)
        assert not runtime.gate.state()[STATE]["pending"]
        assert runtime.gate.session.claims.snapshot().state.processed_evidence_seq == 4
        assert runtime.gate.session.claims.snapshot().state.last_maintenance_ok
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_snapshot_restore_preserves_pending_window(tmp_path, monkeypatch):
    host, _client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [update(), read(), read(), read(), read(), MockResponse(text="ready"), brief(), MockResponse(text="done")],
    )
    try:
        await host.run_until_final("Fix empty input", timeout=10)
        runtime = runtime_for(host)
        state = runtime.gate.state()
        state[STATE] = json.loads(json.dumps(state[STATE]))
        runtime.gate.session = None  # rebuild the loaded Compass checkpoint
        result = await host.run_until_final("Check the Brief", timeout=10)
        assert result.text == "done"
        assert len(runtime.gate.session.evidence.snapshot()) == 4
        assert runtime.gate.session.claims.snapshot().state.last_maintenance_attempt_step == 4
        assert len(probe.extractions) == 2  # the new user message refreshes the contract once
        outcome = next(json.loads(text) for text in results(_client) if text.startswith('{"outcome"'))
        assert not outcome["maintenance_error"]
        claims = runtime.gate.session.claims.snapshot()
        assert claims.state.maintenance_degraded_after_step is None
        assert claims.state.processed_evidence_seq == 4
        assert set(claims.state.requirement_coverage) == {"R1"}

    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_rejected_tools_are_not_evidence_and_child_does_not_touch_parent(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final("Hello", timeout=10)
        runtime = runtime_for(host)
        context = ToolContext.for_testing(tmp_path, session_data_dir=runtime.directory, session_id=runtime.session_id)
        call = ToolCall(
            name="write_file", kind="filesystem.write", call_id="blocked", arguments={"path": "x"}, context=context
        )
        result = ToolResult(text="rejected", failed=True, rejected=True, duration_ms=0)
        # Public replies now create a statement checkpoint, not repository evidence.
        before = json.loads(json.dumps(runtime.gate.state()))
        await extension.after_tool_hooks[0].func(call, result)
        assert runtime.gate.state() == before
        assert not runtime.gate.session.evidence.snapshot()
        origin = InvocationOrigin(kind="sub_agent", session_id=runtime.session_id, invocation_id="child", parent=None)
        token = current_invocation_origin.set(origin)
        try:
            await extension.after_tool_hooks[0].func(call, replace(result, rejected=False))
        finally:
            current_invocation_origin.reset(token)
        assert runtime.gate.state() == before
        assert not runtime.gate.session.evidence.snapshot() and not probe.prompts
    finally:
        await host.shutdown()


def test_core_provenance_is_unchanged():
    source_constants(SOURCE_ROOT)


@pytest.mark.asyncio
async def test_failed_observation_does_not_replace_tool_result(tmp_path, monkeypatch):
    async def fail_observation(self, call, result):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(ToolCompass, "observe", fail_observation)
    host, client, _, reads, _ = await host_for(tmp_path, monkeypatch, [read(), MockResponse(text="done")])
    try:
        result = await host.run_until_final("Read the code", timeout=10)
        assert result.text == "done" and reads == ["a.py"]
        assert any("def f(items)" in text for text in results(client))
        assert not any("withheld" in text for text in results(client))
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_native_retry_runs_without_compass_turn_wrapper(tmp_path, monkeypatch, stored):
    host, _client, probe, reads, _ = await host_for(
        tmp_path, monkeypatch, [read(), MockResponse(text="done")], failures={1}, stored=stored
    )
    try:
        result = await host.run_until_final("Read", timeout=10)
        assert result.text == "done" and reads == ["a.py"]
        assert not probe.prompts
        runtime = runtime_for(host)
        assert not runtime.gate.state()[STATE]["pending"]
        assert len(runtime.gate.session.evidence.snapshot()) == 1
    finally:
        await host.shutdown()
    assert runtime.closed
    assert (runtime.directory, runtime.session_id) not in _SESSIONS


@pytest.mark.asyncio
async def test_observation_does_not_wait_for_compass_model_lock(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    try:
        await host.run_until_final("Fix empty input", timeout=10)
        runtime = runtime_for(host)
        context = ToolContext.for_testing(tmp_path, session_data_dir=runtime.directory, session_id=runtime.session_id)
        call = ToolCall(
            name="read_file", kind="filesystem.read", call_id="parallel", arguments={"path": "a.py"}, context=context
        )
        result = ToolResult(text="def f(): pass", failed=False, rejected=False, duration_ms=0)
        origin = InvocationOrigin(
            kind="turn", session_id=runtime.session_id, invocation_id="parallel-turn", parent=None
        )
        token = current_invocation_origin.set(origin)
        try:
            async with runtime.gate.lock:
                await asyncio.wait_for(runtime.observe(call, result), timeout=1)
        finally:
            current_invocation_origin.reset(token)
        assert len(runtime.gate.state()[STATE]["pending"]) == 1
        await runtime.inspect(["E-001"])
        assert len(runtime.gate.session.evidence.snapshot()) == 1
    finally:
        await host.shutdown()
