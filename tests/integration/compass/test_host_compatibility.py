"""Regression checks for the optional Compass dispatch and session boundary."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chrys.foundation.models.invocations import InvocationOrigin
from chrys.kernel import Message
from chrys.orchestration.engine.run.runner import TurnRunner
from chrys.orchestration.invoker.contracts import AbortCause, Aborted, StopCause
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.profiles.models.schema import ModelProfile
from compass.adapters.icode.interactive import STATE, InteractiveCompass
from tests.integration.compass.test_multiturn import bare_bridge
from tests.integration.compass.test_review_fixes import success


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "disabled", "enabled"])
@pytest.mark.parametrize("steer", [False, True])
async def test_runner_keeps_native_backend_contract_without_run_pass(mode, steer):
    initial = SimpleNamespace(messages=[Message("user", ["task"])])
    followup = SimpleNamespace(messages=[Message("user", ["also handle empty input"])])
    finished = success()
    interrupted = Aborted(
        handle=finished.handle,
        usage=finished.usage,
        effects=finished.effects,
        stop=StopCause.ABORTED,
        continuation=None,
        cause=AbortCause.STEER,
    )
    results = [interrupted, finished] if steer else [finished]
    backend_run = AsyncMock(side_effect=results)
    compass_run = AsyncMock(side_effect=results)
    bindings = SimpleNamespace(
        backend=SimpleNamespace(run=backend_run),
        record_outcome=Mock(),
        state=SimpleNamespace(steered=steer),
        inputs=SimpleNamespace(steer_request=Mock(return_value=followup)),
    )
    if mode != "legacy":
        bindings.compass = SimpleNamespace(run=compass_run) if mode == "enabled" else None
    # Deliberately no run_pass method: this is the pre-Compass executor contract.
    assert not hasattr(bindings, "run_pass")
    runner = object.__new__(TurnRunner)
    runner._current = SimpleNamespace(require_loaded=lambda: SimpleNamespace(bindings=bindings))
    runner._finalizer = SimpleNamespace(settle_steered_pass=AsyncMock())
    runner._withdraw_answer = AsyncMock()
    runner._turn_state = SimpleNamespace(lease=SimpleNamespace(consume_pre_executor_interrupt=lambda: False))
    runner._session = SimpleNamespace(shutting_down=False)
    await runner._run_passes(initial)
    selected = compass_run if mode == "enabled" else backend_run
    other = backend_run if mode == "enabled" else compass_run
    assert [call.args[0] for call in selected.await_args_list] == ([initial, followup] if steer else [initial])
    other.assert_not_awaited()
    assert [call.args[0] for call in bindings.record_outcome.call_args_list] == results
    assert runner._finalizer.settle_steered_pass.await_count == int(steer)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session", [False, True])
async def test_snapshot_restore_requires_the_actual_session_identity(tmp_path, same_session):
    first, bindings, _, request = await bare_bridge(tmp_path)
    second = InteractiveCompass(ModelProfile(id="test", name="Test"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    try:
        first.owner.calls = 17
        first.owner.flags.started = True
        first.owner.flags.handoff_requested = True
        first.owner.seen = {"already-observed"}
        first.owner.save()
        saved = json.loads(json.dumps(bindings.backend.history_state))
        ledger = saved[STATE]
        bindings.backend.history_state = saved
        second.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
        session_id = ledger["session_id"] if same_session else "different-session"
        await second.begin(replace(request, origin=InvocationOrigin("turn", session_id, "next-turn", None)))
        if same_session:
            assert second.owner.calls == 17 and second.owner.flags.started
            assert second.owner.task_id == ledger["task_id"]
            assert second.owner.seen == {"already-observed"}
            assert second.owner.wall_started == ledger["wall_started"]
        else:
            assert second.owner.calls == 0 and not second.owner.flags.started
            assert second.owner.task_id == "next-turn" and not second.owner.seen
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_ui_removes_only_the_wall_limit_not_call_cap_or_headless_deadline(tmp_path):
    import time

    from compass.adapters.icode.storage import Artifacts
    from compass.provenance import SOURCE_ROOT, source_constants
    from compass.runtime.budget import MAX_CALLS, MAX_SECONDS
    from compass.runtime.records import BudgetExhausted
    from compass.runtime.run_state import RunState

    bridge, _, _, _ = await bare_bridge(tmp_path)
    try:
        ui = bridge.owner
        ui.started_at = time.monotonic() - MAX_SECONDS * 10
        ui.wall_started = time.time() - MAX_SECONDS * 10
        ui.check_budget()
        assert ui.remaining() == float("inf")
        headless = RunState("task", tmp_path, tmp_path, source_constants(SOURCE_ROOT), artifacts=Artifacts())
        headless.started_at = ui.started_at
        with pytest.raises(BudgetExhausted):
            headless.check_budget()
        ui.calls = MAX_CALLS
        with pytest.raises(BudgetExhausted):
            ui.check_budget()
    finally:
        await bridge.aclose()
