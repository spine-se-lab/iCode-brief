"""New checks for named delivery/state records, without replaying the old suite."""

import json
from dataclasses import asdict, fields

import pytest

from chrys.kernel import ChatResponse, Message
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.interactive import STATE, InteractiveBriefChat, InteractiveRunState
from compass.adapters.icode.middleware import BriefChat, PendingBrief
from compass.adapters.icode.storage import Artifacts
from compass.core.briefing.policy import DeliveryFacts, DeliveryMark
from compass.provenance import SOURCE_ROOT, source_constants
from compass.runtime.records import BudgetExhausted, InvalidCompassRun
from compass.runtime.run_state import RunState, WorkflowFlags
from compass.runtime.workflow import prepare_submission_check, require_handoff
from tests.integration.compass.test_multiturn import (
    FOLLOWUP,
    ROOT_TASK,
    host_for,
    initial_responses,
    update,
    write,
)

FLAG_NAMES = (
    "started",
    "handoff_seen",
    "rules_seen",
    "closing_seen",
    "check_done",
    "handoff_requested",
    "write_requested",
)


def state(tmp_path):
    return RunState("task", tmp_path, tmp_path, source_constants(SOURCE_ROOT), artifacts=Artifacts())


def test_flags_are_independent_and_have_exact_existing_wire_names():
    assert tuple(field.name for field in fields(WorkflowFlags)) == FLAG_NAMES
    first, second = WorkflowFlags(), WorkflowFlags()
    first.write_requested = True
    assert asdict(second) == dict.fromkeys(FLAG_NAMES, False)
    assert first.write_requested and not first.started and not first.handoff_seen


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_flag_roundtrip_preserves_all_combinations_and_legacy_defaults(tmp_path, version):
    stored = {}
    run = InteractiveRunState(
        "task",
        tmp_path,
        tmp_path,
        source_constants(SOURCE_ROOT),
        artifacts=Artifacts(),
        state=lambda: stored,
        invocation_id="turn",
        session_id="session",
    )
    for mask in range(1 << len(FLAG_NAMES)):
        expected = {name: bool(mask & (1 << index)) for index, name in enumerate(FLAG_NAMES)}
        run.flags = WorkflowFlags(**expected)
        run.save()
        saved = json.loads(json.dumps(stored[STATE]))
        assert saved["flags"] == expected
        saved["version"] = version
        run.flags = WorkflowFlags()
        await run.restore(saved)
        assert asdict(run.flags) == expected
    saved["flags"] = {"started": True}
    await run.restore(saved)
    assert asdict(run.flags) == {**dict.fromkeys(FLAG_NAMES, False), "started": True}


@pytest.mark.asyncio
async def test_snapshot_keeps_task_budget_inputs_and_delivery_state(tmp_path):
    stored = {}
    args = ("original task", tmp_path, tmp_path, source_constants(SOURCE_ROOT))
    first = InteractiveRunState(
        *args,
        artifacts=Artifacts(),
        state=lambda: stored,
        invocation_id="turn-2",
        task_id="task-1",
        session_id="session",
    )
    first.calls = 31
    first.flags = WorkflowFlags(started=True, handoff_requested=True, rules_seen=True)
    first.accepted_inputs = {"initial", "followup"}
    first.user_messages = [{"message_id": "followup", "text": "support empty input", "order": 1}]
    first.requirements_through = 1
    first.requirements_pending = True
    first.check_invocation_id = "turn-2"
    first.seen = {"tool-result"}
    first.conflicts = {"R1"}
    first.batch_size = 2
    first.mark = DeliveryMark(revision=3, escalations=frozenset({("R1", "REVIEW")}), degraded=True)
    first.save()
    saved = json.loads(json.dumps(stored[STATE]))
    second = InteractiveRunState(
        *args,
        artifacts=Artifacts(),
        state=lambda: stored,
        invocation_id="turn-3",
        task_id="task-1",
        session_id="session",
    )
    await second.restore(saved)
    second.save()
    assert json.loads(json.dumps(stored[STATE])) == saved
    assert second.remaining() == first.remaining() == float("inf")


def test_named_pending_brief_keeps_headless_delivery_behavior(tmp_path):
    run = state(tmp_path)
    run.flags.started = True
    chat = BriefChat(run, InjectionMiddleware())
    facts = DeliveryFacts(revision=7, escalations=(("R1", "REVIEW"),))
    chat.pending = PendingBrief("brief text", facts, {"R1 conflict"}, "maintenance")
    chat.observe_request([Message("user", ["other text"])])
    assert not run.flags.handoff_seen and chat.pending is not None
    chat.observe_request([Message("user", ["brief text"])])
    assert chat.pending is None and run.flags.handoff_seen and run.flags.rules_seen
    assert run.mark.revision == 7 and run.conflicts == {"R1 conflict"}


@pytest.mark.asyncio
async def test_ui_confirms_the_same_envelope_only_after_complete_response(tmp_path):
    stored = {}
    run = InteractiveRunState(
        "task",
        tmp_path,
        tmp_path,
        source_constants(SOURCE_ROOT),
        artifacts=Artifacts(),
        state=lambda: stored,
        invocation_id="turn",
    )
    run.flags.started = True
    chat = InteractiveBriefChat(run, InjectionMiddleware(), allow_stream=True)
    brief = PendingBrief("brief text", DeliveryFacts(revision=1), set(), "maintenance")
    chat.pending = brief
    chat.observe_request([Message("user", [brief.text])])
    assert chat.observed is brief and not run.flags.handoff_seen
    await chat.record_response(ChatResponse(messages=[], continuation_token="poll"))
    assert chat.pending is brief and not run.flags.handoff_seen
    await chat.record_response(ChatResponse(messages=[Message("assistant", ["received"])]))
    assert chat.pending is None and chat.observed is None and run.flags.handoff_seen


def test_validity_and_submission_budget_still_use_the_independent_flags(tmp_path):
    run = state(tmp_path)
    for flag in ("started", "handoff_seen", "rules_seen"):
        with pytest.raises(InvalidCompassRun):
            require_handoff(run)
        setattr(run.flags, flag, True)
    require_handoff(run)
    run.calls = 236
    held, _prompt = prepare_submission_check(run)
    assert not held and run.flags.check_done
    assert prepare_submission_check(run) is None
    run.calls = 250
    with pytest.raises(BudgetExhausted):
        run.check_budget()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_named_records_preserve_native_retry_followup_and_update(tmp_path, monkeypatch, stream):
    responses = [
        *initial_responses(),
        MockResponse(text="explanation"),
        update(),
        write(),
        MockResponse(text="updated"),
        MockResponse(text="checked"),
    ]
    host, client, probe, reads, writes = await host_for(tmp_path, monkeypatch, responses, stream=stream, failures={5})
    try:
        await host.run_until_final(ROOT_TASK)
        run = host.engine.current.require_loaded().bindings.compass.owner
        original_task_id, original_start_time = run.task_id, run.wall_started
        assert (await host.run_until_final("Why?")).text == "explanation"
        assert (await host.run_until_final(FOLLOWUP)).text == "checked"
        assert run.task_id == original_task_id and run.wall_started == original_start_time
        assert run.calls == len(client.requests) == 10
        assert len(probe.extractions) == 2 and reads == ["a.py"] and writes == ["a.py"]
        assert run.flags.started and run.flags.handoff_seen and run.flags.check_done
        assert [entry["text"] for entry in run.user_messages] == ["Why?", FOLLOWUP]
    finally:
        await host.shutdown()
