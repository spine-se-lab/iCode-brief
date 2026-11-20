"""New offline cases for Compass with native iCode retries enabled."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.tool_kinds import set_tool_kind
from chrys.kernel import ChatResponseUpdate, Content, ResponseStream, tool
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from compass.adapters.icode.interactive import STATE
from compass.adapters.icode.interactive_model import SelectedModel
from compass.core.session import STATE_KEY
from tests.integration.compass.test_interactive import maintenance
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)


class RetryClient(MockChatClient):
    def __init__(self, responses, failures=(), *, failure_kind="disconnect"):
        super().__init__(responses=responses)
        self.failures = set(failures)
        self.failure_kind = failure_kind
        self.requests = []
        self.inspect = lambda: None
        self.ledgers = []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.requests.append(([m.text for m in messages], stream))
        self.ledgers.append(self.inspect())
        if len(self.requests) not in self.failures:
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

        async def fail():
            if self.failure_kind == "stall":
                await asyncio.Event().wait()
            raise ConnectionError("synthetic network disconnect")

        async def updates():
            if self.failure_kind == "shown":
                yield ChatResponseUpdate(contents=[Content.from_text("visible answer")], role="assistant")
            else:
                yield ChatResponseUpdate(
                    contents=[Content.from_text_reasoning(text="unfinished thought")], role="assistant"
                )
            await fail()

        return ResponseStream(updates()) if stream else fail()


async def setup_host(
    tmp_path, monkeypatch, *, stream=False, stored=False, failures=(), retries=2, failure_kind="disconnect"
):
    reads = []

    @tool
    async def read_file(path: str) -> str:
        """Read synthetic code."""
        reads.append(path)
        return "def f(items): return list(items)"

    set_tool_kind(read_file, "filesystem.read")
    client = RetryClient(
        [
            MockResponse(tool_calls=[("read_file", "r1", {"path": "a.py"})]),
            MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})]),
            MockResponse(text="implemented"),
            MockResponse(text="checked"),
        ],
        failures,
        failure_kind=failure_kind,
    )
    _patch_runtime(monkeypatch, [client], [read_file])
    monkeypatch.setattr(SelectedModel, "complete", maintenance)
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    settings, models = _make_settings_and_models()
    settings = replace(settings, max_transient_retries=retries)
    model = models.get("mock-profile")
    model.stream = stream
    model.chat_options = json.dumps({"store": stored})
    profile = _make_profile(approval_default="auto")
    profile.extensions.append("compass")
    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=_make_agent_registry(profile),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        surface=SessionSurface.TUI,
    )
    await host.start()
    bindings = host.engine.current.require_loaded().bindings
    client.inspect = lambda: deepcopy(bindings.backend.history_state[STATE])
    assert bindings._effective_max_retries() == retries
    if failure_kind == "stall":
        bindings._stream_attempt_timeout = 0.03
    return host, client, bindings, reads


def audits(bindings):
    return [
        json.loads(line)
        for line in (bindings.compass.owner.directory / "adapter-events.jsonl").read_text().splitlines()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_wire_disconnect_replays_same_brief_and_counts_failed_attempt(tmp_path, monkeypatch, stream):
    host, client, bindings, reads = await setup_host(tmp_path, monkeypatch, stream=stream, failures={3})
    try:
        result = await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert result.text == "checked"
        assert len(client.requests) == 5 and reads == ["a.py"]
        assert client.requests[2][0] == client.requests[3][0]
        assert sum("[Compass" in text for text in client.requests[3][0]) == 1
        assert not client.ledgers[2]["flags"]["handoff_seen"]
        assert not client.ledgers[3]["flags"]["handoff_seen"]
        assert client.ledgers[4]["flags"]["handoff_seen"]
        assert [x["calls"] for x in client.ledgers] == [1, 2, 3, 4, 5]
        assert len({x["wall_started"] for x in client.ledgers}) == 1
        assert bindings.compass.owner.calls == 5
        assert client.ledgers[2]["core"][STATE_KEY]["evidence"] == client.ledgers[3]["core"][STATE_KEY]["evidence"]
        assert len([x for x in audits(bindings) if x["event"] == "submission_check"]) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_service_whole_run_retry_retains_budget_and_workflow(tmp_path, monkeypatch, stream):
    host, client, bindings, reads = await setup_host(tmp_path, monkeypatch, stream=stream, stored=True, failures={1})
    try:
        result = await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert result.text == "checked"
        assert bindings.backend.service_session_storage_enabled
        assert len(client.requests) == 5 and reads == ["a.py"]
        assert client.requests[0][0] == client.requests[1][0]
        assert sum("Compass workflow" in text for text in client.requests[1][0]) == 1
        assert [x["calls"] for x in client.ledgers] == [1, 2, 3, 4, 5]
        assert len({x["wall_started"] for x in client.ledgers}) == 1
        assert bindings.compass.owner.calls == 5
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_service_retry_never_repeats_committed_read_and_handoff(tmp_path, monkeypatch, stream):
    host, client, bindings, reads = await setup_host(tmp_path, monkeypatch, stream=stream, stored=True, failures={3})
    try:
        with pytest.raises(RuntimeError):
            await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert len(client.requests) == 3 and reads == ["a.py"]
        assert bindings.compass.owner.flags.started
        assert not bindings.compass.owner.flags.handoff_seen
        assert bindings.backend.history_state[STATE]["calls"] == 3
        assert bindings.compass.chat.pending is None
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_native_retry_limit_is_honored_and_unfinished_brief_not_confirmed(tmp_path, monkeypatch, stream):
    host, client, bindings, reads = await setup_host(
        tmp_path, monkeypatch, stream=stream, failures={3, 4, 5, 6}, retries=1
    )
    try:
        with pytest.raises(RuntimeError):
            await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert len(client.requests) == 4 and reads == ["a.py"]
        assert bindings.compass.owner.calls == 4
        assert bindings.compass.owner.flags.started and not bindings.compass.owner.flags.handoff_seen
        assert not any(x["event"] == "brief_delivered" for x in audits(bindings))
        assert not bindings.compass.owner.flags.check_done
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_whole_run_rollback_keeps_brief_evidence_and_failed_attempt(tmp_path, monkeypatch, stream):
    from compass.adapters.icode.interactive import InteractiveCompass
    from compass.core.evidence.observation import ToolObservation

    begin = InteractiveCompass.begin

    async def resume_ready_task(self, request):
        await begin(self, request)
        if not self.owner.flags.started:
            # A task that explored and handed off before this backend pass.
            await self.owner.core.observe(
                ToolObservation(
                    tool_name="read_file",
                    tool_kind="filesystem.read",
                    arguments={"path": "a.py"},
                    result="def f(items): return list(items)",
                    working_dir=str(tmp_path),
                    tool_call_id="seed-read",
                ),
                schedule_maintenance=False,
            )
            self.owner.batch_size = 1
            await self.owner.handoff("a.py inspected")
            self.owner.calls = 40
            self.owner.save()

    monkeypatch.setattr(InteractiveCompass, "begin", resume_ready_task)
    host, client, bindings, reads = await setup_host(tmp_path, monkeypatch, stream=stream, stored=True, failures={1})
    client._responses = [MockResponse(text="implemented"), MockResponse(text="checked")]
    try:
        result = await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert result.text == "checked"
        assert len(client.requests) == 3 and reads == []
        assert client.requests[0][0] == client.requests[1][0]
        assert [x["calls"] for x in client.ledgers] == [41, 42, 43]
        assert not client.ledgers[1]["flags"]["handoff_seen"]
        assert client.ledgers[2]["flags"]["handoff_seen"]
        assert client.ledgers[0]["core"][STATE_KEY]["evidence"] == client.ledgers[1]["core"][STATE_KEY]["evidence"]
        assert bindings.compass.owner.calls == 43
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_budget_exhaustion_stops_native_retry_without_reset(tmp_path, monkeypatch, stored):
    from compass.adapters.icode.interactive import InteractiveCompass

    begin = InteractiveCompass.begin

    async def last_attempt(self, request):
        await begin(self, request)
        self.owner.calls = 249
        self.owner.save()

    monkeypatch.setattr(InteractiveCompass, "begin", last_attempt)
    host, client, bindings, _ = await setup_host(tmp_path, monkeypatch, stored=stored, failures={1, 2, 3}, retries=5)
    try:
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await host.run_until_final("task", timeout=10)
        assert len(client.requests) == 1
        assert bindings.compass.owner.calls == 250
        assert bindings.backend.history_state[STATE]["calls"] == 250
        assert not bindings.compass.owner.flags.check_done
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_native_retry_ignores_expired_ui_wall_clock(tmp_path, monkeypatch, stored):
    import time

    from compass.adapters.icode.interactive import InteractiveCompass
    from compass.runtime.budget import MAX_SECONDS

    begin = InteractiveCompass.begin

    async def long_idle(self, request):
        await begin(self, request)
        self.owner.started_at = time.monotonic() - MAX_SECONDS * 10
        self.owner.wall_started = time.time() - MAX_SECONDS * 10

    monkeypatch.setattr(InteractiveCompass, "begin", long_idle)
    host, client, bindings, _ = await setup_host(tmp_path, monkeypatch, stored=stored, failures={1}, retries=5)
    monkeypatch.setattr(bindings, "_BACKOFF_SCHEDULE", (0.02,))
    try:
        result = await host.run_until_final("task", timeout=3)
        assert result.text == "checked"
        assert len(client.requests) == 5
        assert bindings.compass.owner.calls == 5
        assert bindings.compass.owner.remaining() == float("inf")
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_stream_stall_falls_back_natively_without_losing_workflow(tmp_path, monkeypatch, stored):
    host, client, bindings, reads = await setup_host(
        tmp_path,
        monkeypatch,
        stream=True,
        stored=stored,
        failures={1, 2},
        retries=1,
        failure_kind="stall",
    )
    try:
        result = await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert result.text == "checked"
        assert [r[1] for r in client.requests[:3]] == [True, True, False]
        assert client.requests[0][0] == client.requests[1][0] == client.requests[2][0]
        assert len(client.requests) == 6 and reads == ["a.py"]
        assert bindings.compass.owner.calls == 6
        assert len({x["wall_started"] for x in client.ledgers}) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_visible_partial_answer_is_not_replayed(tmp_path, monkeypatch, stored):
    host, client, bindings, reads = await setup_host(
        tmp_path,
        monkeypatch,
        stream=True,
        stored=stored,
        failures={3},
        retries=2,
        failure_kind="shown",
    )
    try:
        with pytest.raises(RuntimeError):
            await host.run_until_final("Return an empty list for empty input.", timeout=10)
        assert len(client.requests) == 3 and reads == ["a.py"]
        assert bindings.compass.owner.calls == 3
        assert not bindings.compass.owner.flags.handoff_seen
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_cancel_during_retry_backoff_does_not_send_again(tmp_path, monkeypatch):
    host, client, bindings, _ = await setup_host(tmp_path, monkeypatch, failures={1}, retries=5)
    monkeypatch.setattr(bindings, "_BACKOFF_SCHEDULE", (60,))
    waiting = asyncio.Event()
    sleep = bindings._interruptible_sleep

    async def observe_wait(delay):
        waiting.set()
        return await sleep(delay)

    monkeypatch.setattr(bindings, "_interruptible_sleep", observe_wait)
    run = asyncio.create_task(host.run_until_final("task", timeout=5))
    try:
        async with asyncio.timeout(2):
            await waiting.wait()
        await host.cancel_current_turn()
        await asyncio.gather(run, return_exceptions=True)
        assert len(client.requests) == 1
        assert bindings.compass.owner.calls == 1
        assert not bindings.compass.owner.flags.check_done
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await host.shutdown()


@pytest.mark.asyncio
async def test_failed_attempt_budget_and_evidence_survive_serialized_rebuild(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from chrys.foundation.models.invocations import InvocationOrigin
    from chrys.kernel import Message
    from chrys.orchestration.invoker.contracts import RunIntent, RunRequest
    from chrys.service.agent_middleware.injection import InjectionMiddleware
    from chrys.service.state.serializers import deserialize_state, serialize_state
    from compass.adapters.icode.interactive import InteractiveCompass

    host, client, bindings, reads = await setup_host(tmp_path, monkeypatch, failures={3, 4}, retries=1)
    try:
        with pytest.raises(RuntimeError):
            await host.run_until_final("Return an empty list for empty input.", timeout=10)
        saved = deserialize_state(json.loads(json.dumps(serialize_state(bindings.backend.history_state))))
        ledger = saved[STATE]
        assert ledger["calls"] == 4 and len(client.requests) == 4 and reads == ["a.py"]
        bridge = InteractiveCompass(
            bindings.compass.model.profile, SimpleNamespace(cwd=str(tmp_path)), tmp_path / "rebuilt"
        )
        bridge.bind(
            SimpleNamespace(middleware=[]),
            SimpleNamespace(backend=SimpleNamespace(history_state=saved)),
            InjectionMiddleware(),
        )
        try:
            await bridge.begin(
                RunRequest(
                    [Message("user", ["task"])],
                    RunIntent.RETRY,
                    InvocationOrigin("turn", ledger["session_id"], ledger["invocation_id"], None),
                )
            )
            assert bridge.owner.calls == 4 and bridge.owner.wall_started == ledger["wall_started"]
            assert bridge.owner.flags.started and not bridge.owner.flags.handoff_seen
            assert bridge.owner.seen == set(ledger["seen"])
            assert (
                bridge.core.contribute_session_state()[STATE_KEY]["evidence"] == ledger["core"][STATE_KEY]["evidence"]
            )
        finally:
            await bridge.aclose()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_invalid_compass_treatment_is_not_network_retry(tmp_path, monkeypatch):
    host, client, bindings, _ = await setup_host(tmp_path, monkeypatch, retries=5)
    client._responses = [
        MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "no evidence"})]),
        MockResponse(text="I skipped the required exploration"),
    ]
    try:
        with pytest.raises(RuntimeError, match="not a valid Compass treatment"):
            await host.run_until_final("task", timeout=10)
        assert len(client.requests) == 2 and bindings.compass.owner.calls == 2
        assert not bindings.compass.owner.flags.check_done
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_background_poll_retry_keeps_original_brief_until_terminal_response(tmp_path, monkeypatch, stored):
    import chrys.kernel.loop as loop

    monkeypatch.setattr(loop, "CONTINUATION_POLL_INTERVAL_SECONDS", 0)
    host, client, bindings, reads = await setup_host(tmp_path, monkeypatch, stored=stored, failures={4})
    client._responses = [
        *client._responses[:2],
        MockResponse(text="waiting"),
        MockResponse(text="implemented"),
        MockResponse(text="checked"),
    ]
    original = client._mock_response

    async def pending_response(resp, model, finish, on_text):
        response = await original(resp, model, finish, None if resp.text == "waiting" else on_text)
        if resp.text == "waiting":
            response.continuation_token = {"response_id": "pending-brief"}
        return response

    monkeypatch.setattr(client, "_mock_response", pending_response)
    try:
        # Stored runs already did tool work, so their native gate may stop
        # a failed poll; in-place local wire polling can retry safely.
        if stored:
            with pytest.raises(RuntimeError):
                await host.run_until_final("Return an empty list for empty input.", timeout=10)
            assert len(client.requests) == 4
            assert not bindings.compass.owner.flags.handoff_seen
        else:
            result = await host.run_until_final("Return an empty list for empty input.", timeout=10)
            assert result.text == "checked"
            assert len(client.requests) == 6
            assert not client.ledgers[4]["flags"]["handoff_seen"]
            assert client.ledgers[5]["flags"]["handoff_seen"]
            assert bindings.compass.owner.calls == 6
        assert reads == ["a.py"]
        assert not client.ledgers[3]["flags"]["handoff_seen"]
    finally:
        await host.shutdown()
