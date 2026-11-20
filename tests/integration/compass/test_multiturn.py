"""New task-continuity cases through native iCode sessions, with offline providers."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chrys.foundation.events.types import SessionNew, SetModelProfile
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.tool_kinds import set_tool_kind
from chrys.kernel import ChatResponse, Message, tool
from chrys.kernel.middleware import ChatContext, ChatMiddlewarePipeline
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.contracts import RunIntent, RunRequest
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from compass.adapters.icode.interactive import STATE, InteractiveCompass
from compass.adapters.icode.interactive_model import SelectedModel
from compass.ports import ModelReply
from compass.runtime.budget import MAX_CALLS, MAX_SECONDS
from tests.integration.compass.test_interactive_retry import RetryClient
from tests.integration.compass.test_review_fixes import events, success
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)

ROOT_TASK = "Return an empty list for empty input."
FOLLOWUP = "Also support empty strings."


def read():
    return MockResponse(tool_calls=[("read_file", "r1", {"path": "a.py"})])


def handoff():
    return MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})])


def write():
    return MockResponse(tool_calls=[("write_file", "w1", {"path": "a.py", "content": "fixed"})])


def update():
    return MockResponse(tool_calls=[("update_compass_requirements", "u1", {})])


def initial_responses():
    return [read(), handoff(), MockResponse(text="implemented"), MockResponse(text="checked")]


class MaintenanceProbe:
    def __init__(self):
        self.extractions = []
        self.prompts = []

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        self.prompts.append(user_prompt)
        if response_schema["title"] == "IssueRequirementExtraction":
            self.extractions.append(user_prompt)
            requirements = [{"id": "R1", "text": ROOT_TASK}]
            if FOLLOWUP in user_prompt:
                requirements.append({"id": "R2", "text": FOLLOWUP})
            payload = {"requirements": requirements, "constraints": [], "signature_changes": []}
        else:
            payload = {
                k: [] for k in ("new_claims", "augment", "merge", "conflicts", "contradiction_scan", "deactivate")
            }
        return ModelReply(json.dumps(payload))


async def host_for(tmp_path, monkeypatch, responses, *, stream=False, failures=(), extra_clients=(), stored=False):
    reads, writes = [], []

    @tool
    async def read_file(path: str) -> str:
        """Read synthetic source."""
        reads.append(path)
        return "def f(items): return list(items)"

    @tool
    async def write_file(path: str, content: str) -> str:
        """Write synthetic code."""
        writes.append(path)
        return "written"

    set_tool_kind(read_file, "filesystem.read")
    set_tool_kind(write_file, "filesystem.write")
    client = RetryClient(responses, failures)
    _patch_runtime(monkeypatch, [client, *extra_clients], [read_file, write_file])
    probe = MaintenanceProbe()

    async def maintain(self, **kwargs):
        return await probe.complete(**kwargs)

    monkeypatch.setattr(SelectedModel, "complete", maintain)
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    settings, models = _make_settings_and_models()
    settings = replace(settings, max_transient_retries=1)
    models.get("mock-profile").stream = stream
    models.get("mock-profile").chat_options = json.dumps({"store": stored})
    models.register(ModelProfile(id="second", name="Second", provider="mock", model_id="second", stream=stream))
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
    return host, client, probe, reads, writes


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_followups_preserve_task_requirements_evidence_budget_and_edit_check(tmp_path, monkeypatch, stream):
    responses = [
        *initial_responses(),
        MockResponse(text="explanation"),
        MockResponse(text="continued"),
        update(),
        write(),
        MockResponse(text="updated"),
        MockResponse(text="checked update"),
        write(),
        MockResponse(text="typo fixed"),
        MockResponse(text="checked typo"),
    ]
    host, client, probe, reads, writes = await host_for(tmp_path, monkeypatch, responses, stream=stream)
    try:
        await host.run_until_final(ROOT_TASK)
        bridge = host.engine.current.require_loaded().bindings.compass
        owner = bridge.owner
        task_id, started, directory = owner.task_id, owner.wall_started, owner.directory
        first_evidence = {e.sequence for e in owner.core.evidence.snapshot()}
        assert (await host.run_until_final("Why did you choose this implementation?")).text == "explanation"
        assert (await host.run_until_final("continue")).text == "continued"
        assert len(probe.extractions) == 1
        assert owner.task == ROOT_TASK and owner.calls == 6
        assert len([e for e in events(owner) if e["event"] == "submission_check"]) == 1
        result = await host.run_until_final(FOLLOWUP)
        assert result.text == "checked update"
        assert len(probe.extractions) == 2
        assert ROOT_TASK in probe.extractions[-1] and FOLLOWUP in probe.extractions[-1]
        assert owner.task == ROOT_TASK
        assert FOLLOWUP in json.dumps(owner.core.goals.serialize())
        assert ROOT_TASK in json.dumps(owner.core.goals.serialize())
        result = await host.run_until_final("Fix the typo in the same implementation.")
        assert result.text == "checked typo"
        assert bridge.owner is owner and (owner.task_id, owner.wall_started, owner.directory) == (
            task_id,
            started,
            directory,
        )
        assert first_evidence <= {e.sequence for e in owner.core.evidence.snapshot()}
        assert reads == ["a.py"] and writes == ["a.py", "a.py"]
        assert owner.calls == len(client.requests) == 13
        assert len([e for e in events(owner) if e["event"] == "submission_check"]) == 3
        assert [x["text"] for x in owner.user_messages] == [
            "Why did you choose this implementation?",
            "continue",
            FOLLOWUP,
            "Fix the typo in the same implementation.",
        ]
        assert owner.requirements_through == 3
        assert not any("Compass workflow" in x["text"] or "pre-submission" in x["text"] for x in owner.user_messages)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("stored", [False, True])
async def test_followup_retry_keeps_original_task_and_records_input_once(tmp_path, monkeypatch, stream, stored):
    host, client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [*initial_responses(), MockResponse(text="continued")],
        stream=stream,
        stored=stored,
        failures={5},
    )
    try:
        await host.run_until_final(ROOT_TASK)
        owner = host.engine.current.require_loaded().bindings.compass.owner
        task_id = owner.task_id
        assert (await host.run_until_final("continue")).text == "continued"
        assert owner.task_id == task_id and owner.calls == 6 and len(client.requests) == 6
        assert [x["text"] for x in owner.user_messages] == ["continue"]
        assert len(probe.extractions) == 1
        assert client.requests[4][0] == client.requests[5][0]
        assert len([e for e in events(owner) if e["event"] == "submission_check"]) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_native_model_rebuild_restores_active_task_for_next_turn(tmp_path, monkeypatch):
    second = RetryClient([MockResponse(text="explanation")])
    host, _client, probe, _, _ = await host_for(tmp_path, monkeypatch, initial_responses(), extra_clients=[second])
    try:
        await host.run_until_final(ROOT_TASK)
        old = host.engine.current.require_loaded().bindings.compass
        old_task, old_time = old.owner.task_id, old.owner.wall_started
        await host.engine._on_set_model_profile(SetModelProfile(profile_id="second"))
        bridge = host.engine.current.require_loaded().bindings.compass
        assert old.model.closed and bridge is not old
        assert (await host.run_until_final("Explain the fix")).text == "explanation"
        assert bridge.owner.task == ROOT_TASK and bridge.owner.task_id == old_task
        assert bridge.owner.wall_started == old_time and bridge.owner.calls == 5
        assert len(probe.extractions) == 1 and bridge.owner.flags.handoff_seen
        assert bridge.model.profile.id == "second"
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_native_new_session_starts_a_separate_task(tmp_path, monkeypatch):
    second = RetryClient(initial_responses())
    host, _client, probe, _, _ = await host_for(tmp_path, monkeypatch, initial_responses(), extra_clients=[second])
    try:
        await host.run_until_final(ROOT_TASK)
        old = host.engine.current.require_loaded().bindings.compass.owner
        old_task, old_session = old.task_id, old.session_id
        await host.engine._on_new_session(SessionNew())
        await host.run_until_final("A different task: " + ROOT_TASK)
        new = host.engine.current.require_loaded().bindings.compass.owner
        assert new.task_id != old_task and new.session_id != old_session and new.directory != old.directory
        assert new.calls == 4 and old.calls == 4
        assert new.task.startswith("A different task") and not new.user_messages
        assert len(probe.extractions) == 2
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["calls", "wall"])
async def test_followup_keeps_call_cap_but_has_no_ui_wall_limit(tmp_path, monkeypatch, kind):
    host, client, _, _, _ = await host_for(
        tmp_path, monkeypatch, [*initial_responses(), MockResponse(text="continued")]
    )
    try:
        await host.run_until_final(ROOT_TASK)
        owner = host.engine.current.require_loaded().bindings.compass.owner
        if kind == "calls":
            owner.calls = MAX_CALLS
        else:
            owner.started_at = time.monotonic() - MAX_SECONDS - 1
        owner.save()
        if kind == "calls":
            with pytest.raises(RuntimeError, match="Compass trial budget exhausted"):
                await host.run_until_final("continue")
            assert len(client.requests) == 4 and owner.calls == MAX_CALLS
        else:
            assert (await host.run_until_final("continue")).text == "continued"
            assert len(client.requests) == 5 and owner.calls == 5
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_failed_requirement_update_blocks_edits_and_is_not_a_readonly_skip(tmp_path, monkeypatch):
    host, _client, _probe, _, writes = await host_for(
        tmp_path, monkeypatch, [*initial_responses(), update(), write(), MockResponse(text="done")]
    )
    try:
        await host.run_until_final(ROOT_TASK)
        owner = host.engine.current.require_loaded().bindings.compass.owner
        # Fail at the existing core API boundary; avoid exercising its retry sleep in this adapter test.
        owner.core.start = AsyncMock(side_effect=RuntimeError("synthetic update failure"))
        with pytest.raises(RuntimeError, match="not a valid Compass treatment"):
            await host.run_until_final(FOLLOWUP)
        assert writes == [] and owner.requirements_pending and not owner.flags.handoff_seen
        assert not any(e["event"] == "compass_skipped" for e in events(owner))
        assert owner.task == ROOT_TASK
    finally:
        await host.shutdown()


async def bare_bridge(tmp_path):
    bridge = InteractiveCompass(ModelProfile(id="test", name="Test"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    bindings = SimpleNamespace(backend=SimpleNamespace(history_state={}, run=AsyncMock(return_value=success())))
    injection = InjectionMiddleware()
    bridge.bind(SimpleNamespace(middleware=[]), bindings, injection)
    request = RunRequest(
        [Message("user", [ROOT_TASK])], RunIntent.FRESH, InvocationOrigin("turn", "session", "one", None)
    )
    await bridge.begin(request)
    return bridge, bindings, injection, request


@pytest.mark.asyncio
async def test_midturn_user_note_is_recorded_but_compass_and_synthetic_messages_are_not(tmp_path):
    bridge, _bindings, injection, _request = await bare_bridge(tmp_path)
    try:
        owner = bridge.owner
        # No start: capture queued notes without maintenance/model calls.
        user = Message(
            "user",
            [FOLLOWUP],
            additional_properties={
                HistoryMarkerKind.INJECTED_KEY: True,
                HistoryMarkerKind.INJECTION_ID_KEY: "user-note",
            },
        )
        own = Message(
            "user",
            ["old Brief"],
            additional_properties={
                HistoryMarkerKind.INJECTED_KEY: True,
                HistoryMarkerKind.INJECTION_ID_KEY: "compass:old",
            },
        )
        synthetic = Message(
            "user",
            ["continue"],
            additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True, HistoryMarkerKind.INJECTED_KEY: True},
        )
        messages = [Message("user", [ROOT_TASK]), user, own, synthetic]

        async def provider(ctx):
            for observe in ctx.request_message_observers:
                observe(ctx.messages)
            return ChatResponse(messages=[Message("assistant", ["ok"])])

        pipeline = ChatMiddlewarePipeline(bridge.chat, injection)
        for _ in range(2):
            await pipeline.execute(ChatContext(None, messages, {}, stream=False), provider)
        assert [x["text"] for x in owner.user_messages] == [FOLLOWUP]
        assert len(owner.core.user_inputs.pending) == 1
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_json_restore_preserves_active_task_on_new_invocation(tmp_path, version):
    first, bindings, _, request = await bare_bridge(tmp_path)
    second = InteractiveCompass(ModelProfile(id="test", name="Test"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    try:
        first.owner.flags.started = first.owner.flags.handoff_seen = first.owner.flags.rules_seen = True
        first.owner.flags.handoff_requested = True
        first.owner.calls = 21
        first.owner.save()
        saved = json.loads(json.dumps(bindings.backend.history_state))
        if version == 1:
            for key in (
                "task_id",
                "session_id",
                "accepted_inputs",
                "user_messages",
                "requirements_through",
                "check_invocation_id",
                "requirements_pending",
            ):
                saved[STATE].pop(key, None)
            saved[STATE]["version"] = 1
        bindings.backend.history_state = saved
        second.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
        followup = replace(
            request, messages=[Message("user", ["continue"])], origin=InvocationOrigin("turn", "session", "two", None)
        )
        await second.begin(followup)
        assert second.owner.calls == 21 and second.owner.task_id == "one"
        assert second.owner.task == ROOT_TASK and second.owner.flags.handoff_seen
        assert second.owner.wall_started == first.owner.wall_started
        assert [x["text"] for x in second.owner.user_messages] == ["continue"]
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_failed_update_can_resume_without_new_handoff_or_budget(tmp_path, monkeypatch):
    host, client, _probe, reads, writes = await host_for(
        tmp_path,
        monkeypatch,
        [
            *initial_responses(),
            update(),
            handoff(),
            write(),
            MockResponse(text="failed"),
            update(),
            write(),
            MockResponse(text="fixed"),
            MockResponse(text="checked"),
        ],
    )
    try:
        await host.run_until_final(ROOT_TASK)
        bridge = host.engine.current.require_loaded().bindings.compass
        owner = bridge.owner
        start = owner.core.start
        owner.core.start = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        with pytest.raises(RuntimeError, match="not a valid Compass treatment"):
            await host.run_until_final(FOLLOWUP)
        assert not writes and owner.requirements_pending
        assert owner.core.start.await_count == 1  # start tool must not bypass the pending update
        owner.core.start = start
        result = await host.run_until_final("Retry the requirement update")
        assert result.text == "checked" and writes == ["a.py"] and reads == ["a.py"]
        assert bridge.owner is owner and not owner.requirements_pending and owner.flags.handoff_seen
        assert ROOT_TASK in json.dumps(owner.core.goals.serialize())
        assert FOLLOWUP in json.dumps(owner.core.goals.serialize())
        assert owner.calls == len(client.requests) == 12
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_requirement_tool_rejects_missing_task_no_new_input_and_parallel_batch(tmp_path):
    bridge, _, _, _request = await bare_bridge(tmp_path)
    try:
        assert "First explore" in await bridge.update_requirements()
        owner = bridge.owner
        owner.flags.started = owner.flags.handoff_seen = True
        owner.batch_size = 1
        assert "No new user input" in await bridge.update_requirements()
        owner.record_input("one", FOLLOWUP)
        owner.batch_size = 2
        assert "alone" in await bridge.update_requirements()
        assert owner.flags.handoff_seen and owner.flags.started and not owner.requirements_pending
        assert owner.calls == 0
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_begin_filters_synthetic_inputs_and_legacy_replay_does_not_add_original_as_followup(tmp_path):
    bridge, bindings, _, request = await bare_bridge(tmp_path)
    try:
        owner = bridge.owner
        owner.flags.started = owner.flags.handoff_seen = True
        owner.save()
        saved = bindings.backend.history_state[STATE]
        saved["version"] = 1
        saved.pop("accepted_inputs")
        owner.accepted_inputs.clear()
        synthetic = Message("user", ["synthetic"], additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True})
        own = Message(
            "user",
            ["workflow"],
            additional_properties={
                HistoryMarkerKind.INJECTED_KEY: True,
                HistoryMarkerKind.INJECTION_ID_KEY: "compass-workflow:old",
            },
        )
        await bridge.begin(replace(request, messages=[*request.messages, synthetic, own]))
        assert not owner.user_messages and not owner.core.user_inputs.pending
        assert len(owner.accepted_inputs) == 1
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_extraction_fallback_does_not_make_revision_valid(tmp_path, monkeypatch):
    host, _client, _probe, _, writes = await host_for(
        tmp_path, monkeypatch, [*initial_responses(), update(), write(), MockResponse(text="done")]
    )
    try:
        await host.run_until_final(ROOT_TASK)
        owner = host.engine.current.require_loaded().bindings.compass.owner
        monkeypatch.setattr(
            "compass.core.session.extract_issue_structure",
            AsyncMock(side_effect=RuntimeError("synthetic provider failure")),
        )
        with pytest.raises(RuntimeError, match="not a valid Compass treatment"):
            await host.run_until_final(FOLLOWUP)
        assert not writes and not owner.flags.started and owner.requirements_pending
        assert any(c.tag == "extraction" for c in owner.core.goals.snapshot().conclusions)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_policy_failure_preserves_actual_usage_and_tool_effects(tmp_path):
    from chrys.orchestration.invoker.contracts import Failed, FailureCategory, FailureDisposition, StopCause
    from compass.runtime.records import InvalidCompassRun

    bridge, bindings, _, request = await bare_bridge(tmp_path)
    bindings.report_policy_failure = AsyncMock()
    try:
        bridge.owner.flags.write_requested = True
        bridge.owner.save()
        original = bindings.backend.run.return_value
        result = await bridge.run(request)
        assert isinstance(result, Failed) and isinstance(result.exception, InvalidCompassRun)
        assert (
            result.handle == original.handle and result.usage == original.usage and result.effects == original.effects
        )
        assert result.stop == StopCause.FAILED and result.continuation is None
        assert result.category == FailureCategory.DEFINITIVE and result.disposition == FailureDisposition.TERMINAL
        bindings.report_policy_failure.assert_awaited_once_with(request, result.exception)
        assert bindings.backend.run.await_count == 1
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_queued_user_input_is_recorded_on_first_consumption_even_without_next_request(tmp_path):
    bridge, _, injection, request = await bare_bridge(tmp_path)
    try:
        injection.queue(FOLLOWUP, injection_id="user-live")
        reminder = Message(
            "user",
            ["host reminder"],
            additional_properties={
                HistoryMarkerKind.INJECTED_KEY: True,
                HistoryMarkerKind.SYSTEM_REMINDERS_KEY: [{"kind": "test"}],
            },
        )

        async def provider(ctx):
            for observe in ctx.request_message_observers:
                observe(ctx.messages)
            return ChatResponse(messages=[Message("assistant", ["ok"])])

        await ChatMiddlewarePipeline(bridge.chat, injection).execute(
            ChatContext(None, [*request.messages, reminder], {}, stream=False), provider
        )
        assert [x["text"] for x in bridge.owner.user_messages] == [FOLLOWUP]
        assert len(bridge.owner.core.user_inputs.pending) == 1
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_first_edit_inside_submission_check_does_not_trigger_another_check_on_question(tmp_path, monkeypatch):
    host, client, _probe, _, writes = await host_for(
        tmp_path,
        monkeypatch,
        [
            read(),
            handoff(),
            MockResponse(text="implemented"),
            write(),
            MockResponse(text="checked"),
            MockResponse(text="explanation"),
        ],
    )
    try:
        assert (await host.run_until_final(ROOT_TASK)).text == "checked"
        owner = host.engine.current.require_loaded().bindings.compass.owner
        assert owner.flags.check_done and writes == ["a.py"]
        assert (await host.run_until_final("Why?")).text == "explanation"
        assert len(client.requests) == 6
        assert len([e for e in events(owner) if e["event"] == "submission_check"]) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_failed_requirement_update_inside_submission_check_still_fails_the_turn(tmp_path, monkeypatch):
    host, _client, _probe, _, _writes = await host_for(
        tmp_path,
        monkeypatch,
        [*initial_responses(), write(), MockResponse(text="done"), update(), MockResponse(text="check ended")],
    )
    try:
        await host.run_until_final(ROOT_TASK)
        owner = host.engine.current.require_loaded().bindings.compass.owner
        owner.core.start = AsyncMock(side_effect=RuntimeError("synthetic update failure"))
        with pytest.raises(RuntimeError, match="not a valid Compass treatment"):
            await host.run_until_final(FOLLOWUP)
        assert owner.requirements_pending and not owner.flags.started
        assert host.engine.current.require_loaded().bindings.state.run_failed
    finally:
        await host.shutdown()
