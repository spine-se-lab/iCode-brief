"""Automatic maintenance must reach the next native request without model tool choice."""

import pytest

from chrys.service.llm.mock import MockResponse
from tests.integration.compass.support import ROOT_TASK, host_for, read
from tests.integration.compass.test_tool_interface import runtime_for


@pytest.mark.asyncio
async def test_native_observations_trigger_and_deliver_without_compass_calls(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [read(), read(), read(), read(), read(), MockResponse(text="done")]
    )
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        claims = runtime.gate.session.claims.snapshot()
        assert claims.state.revision == 1
        assert claims.state.processed_evidence_seq == 4
        assert claims.state.last_maintenance_ok
        assert len(probe.extractions) == 1
        assert len(probe.prompts) == 2
        # Assert the actual next request, not a status preview or saved ledger.
        results = [c for m in client.call_history[4][0] for c in m.contents if c.type == "function_result"]
        returned = str(results[-1].result)
        assert returned.startswith("def f(items): return list(items)")
        assert "[Goal & Claims" in returned
        assert ROOT_TASK in returned
        assert len(client.call_history) == 6
        evidence = runtime.gate.session.evidence.snapshot()
        assert len(evidence) == 5
        assert all("[Goal & Claims" not in e.excerpt for e in evidence)
    finally:
        await host.shutdown()


def observation(tmp_path, *, test=False):
    from compass.evidence.observation import ToolObservation

    return ToolObservation(
        tool_name="zsh" if test else "read_file",
        tool_kind="shell" if test else "filesystem.read",
        arguments={"command": "python -m pytest"} if test else {"path": "a.py"},
        result="1 passed\n[exit_code: 0]" if test else "def f(items): return list(items)",
        exit_code=0 if test else None,
        working_dir=str(tmp_path),
        phase="test",
    )


@pytest.mark.asyncio
async def test_test_result_triggers_immediately_and_restore_does_not_repeat(tmp_path):
    from tests.integration.compass.support import MaintenanceProbe
    from tests.integration.compass.test_flat_gate import new_gate

    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    gate.observe(observation(tmp_path, test=True))
    outcome = await gate.automatic_brief()
    assert outcome["reason"] == "test_result"
    assert outcome["revision"] == 1
    assert outcome["processed_evidence_seq"] == 1
    assert not outcome["maintenance_error"]
    await gate.aclose()
    restored = new_gate(tmp_path, model)
    assert await restored.automatic_brief() is None
    assert len(model.prompts) == 2
    restored.observe(observation(tmp_path, test=True))
    assert (await restored.automatic_brief())["processed_evidence_seq"] == 2
    assert len(model.prompts) == 3
    await restored.aclose()


@pytest.mark.asyncio
async def test_restored_backlog_and_parallel_checks_maintain_once(tmp_path):
    import asyncio

    from tests.integration.compass.support import MaintenanceProbe
    from tests.integration.compass.test_flat_gate import new_gate

    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    await gate.update_requirements()
    for _ in range(4):
        gate.observe(observation(tmp_path))
    await gate.aclose()
    restored = new_gate(tmp_path, model)
    outcomes = await asyncio.gather(restored.automatic_brief(), restored.automatic_brief())
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert restored.session.claims.snapshot().state.processed_evidence_seq == 4
    assert len(model.prompts) == 2
    assert await restored.automatic_brief() is None
    await restored.aclose()


@pytest.mark.asyncio
async def test_new_user_scope_is_refreshed_without_explicit_tool_call(tmp_path):
    from tests.integration.compass.support import FOLLOWUP, MaintenanceProbe
    from tests.integration.compass.test_flat_gate import new_gate

    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    gate.observe(observation(tmp_path, test=True))
    await gate.automatic_brief()
    gate.capture_user_input("2", FOLLOWUP)
    gate.observe(observation(tmp_path))
    outcome = await gate.automatic_brief()
    assert outcome["requirements_refreshed"]
    assert outcome["reason"] == "requirements_changed"
    assert FOLLOWUP in outcome["brief"]
    assert len(model.extractions) == 2
    assert set(gate.session.claims.snapshot().state.requirement_coverage) == {"R1", "R2"}
    await gate.aclose()


@pytest.mark.asyncio
async def test_failed_maintenance_keeps_evidence_and_obeys_cooldown(tmp_path):
    from compass.ports import ModelReply
    from tests.integration.compass.support import MaintenanceProbe
    from tests.integration.compass.test_flat_gate import new_gate

    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    await gate.update_requirements()

    async def invalid(*, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        return ModelReply("{}")

    model.complete = invalid
    gate.observe(observation(tmp_path, test=True))
    outcome = await gate.automatic_brief()
    assert outcome["maintenance_error"]
    assert outcome["revision"] == 0
    assert outcome["processed_evidence_seq"] == 0
    assert len(gate.session.evidence.snapshot()) == 1
    gate.observe(observation(tmp_path))
    assert await gate.automatic_brief() is None
    # A later explicit retry can still process the retained evidence.
    model.complete = MaintenanceProbe().complete
    recovered = await gate.update_brief()
    assert recovered["revision"] == 1 and recovered["processed_evidence_seq"] == 2
    assert not recovered["maintenance_error"]
    await gate.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError, TimeoutError])
async def test_hook_failure_retains_original_output_in_next_request(tmp_path, monkeypatch, failure):
    from compass.gate.controller import CompassGate

    async def fail(self):
        raise failure("private provider detail")

    monkeypatch.setattr(CompassGate, "automatic_brief", fail)
    host, client, _, _, _ = await host_for(tmp_path, monkeypatch, [read(), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        from tests.integration.compass.test_tool_interface import results

        returned = results(client)[-1]
        assert returned.startswith("def f(items): return list(items)")
        assert "automatic check unavailable" in returned
        assert "private provider detail" not in returned
        assert "withheld" not in returned
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_real_run_sized_backlog_is_processed_on_next_check(tmp_path):
    from tests.integration.compass.support import MaintenanceProbe
    from tests.integration.compass.test_flat_gate import new_gate

    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    await gate.update_requirements()
    for _ in range(82):
        gate.observe(observation(tmp_path))
    await gate.aclose()
    restored = new_gate(tmp_path, model)
    outcome = await restored.automatic_brief()
    assert outcome["maintenance_due"]
    assert not outcome["maintenance_error"]
    assert outcome["processed_evidence_seq"] == outcome["latest_evidence_seq"] == 82
    assert "[Goal & Claims" in outcome["brief"]
    await restored.aclose()


@pytest.mark.asyncio
async def test_internal_timeout_does_not_make_host_withhold_tool_output(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from compass.adapters.icode import tools
    from compass.gate.controller import CompassGate
    from tests.integration.compass.test_tool_interface import results

    async def blocked(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(CompassGate, "automatic_brief", blocked)
    monkeypatch.setattr(tools, "asyncio", SimpleNamespace(timeout=lambda seconds: asyncio.timeout(0)))
    host, client, _, _, _ = await host_for(tmp_path, monkeypatch, [read(), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        returned = results(client)[-1]
        assert returned.startswith("def f(items): return list(items)")
        assert "automatic check unavailable" in returned
        assert "withheld" not in returned
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_external_stop_cancels_automatic_check(tmp_path, monkeypatch):
    import asyncio

    from chrys.orchestration.invoker.origin import current_invocation_origin
    from compass.adapters.icode.tools import extension
    from tests.integration.compass.test_adapter_information import call_for, origin_for, result_for

    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="ready")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime.gate, "automatic_brief", blocked)
        token = current_invocation_origin.set(origin_for(runtime))
        task = asyncio.create_task(extension.after_tool_hooks[1].func(call_for(runtime), result_for()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current_invocation_origin.reset(token)
    finally:
        await host.shutdown()
