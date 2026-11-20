"""Offline behavior checks using real frozen Compass and the current native iCode host."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chrys.foundation.tool_kinds import set_tool_kind
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY
from chrys.kernel import ChatResponse, Content, Message, tool
from chrys.kernel.middleware import ChatContext, ChatMiddlewarePipeline, FunctionInvocationContext
from chrys.service.agent_middleware.injection import InjectionMiddleware
from compass.adapters.icode.headless import HeadlessBackend
from compass.adapters.icode.middleware import BriefChat, ExplorationGuard, capture
from compass.adapters.icode.openrouter import MaintenanceModel, ModelConfig
from compass.adapters.icode.storage import Artifacts
from compass.adapters.icode.tools import make_extension
from compass.ports import ModelReply
from compass.provenance import SOURCE_ROOT, source_constants
from compass.runtime.budget import MAX_CALLS
from compass.runtime.records import RunRequest
from compass.runtime.run_state import RunState
from compass.runtime.workflow import CompassRun


class FakeMaintenance:
    async def complete(self, *, response_schema, **kwargs):
        if response_schema["title"] == "IssueRequirementExtraction":
            data = {
                "requirements": [{"id": "R1", "text": "Return an empty list for empty input."}],
                "constraints": [],
                "signature_changes": [],
            }
        else:
            data = {k: [] for k in ("new_claims", "augment", "merge", "conflicts", "contradiction_scan", "deactivate")}
        return ModelReply(json.dumps(data))


@pytest.fixture
def owner(tmp_path):
    return RunState(
        "Return an empty list for empty input.",
        tmp_path,
        tmp_path,
        source_constants(SOURCE_ROOT),
        FakeMaintenance(),
        artifacts=Artifacts(),
    )


def observation(identity="r1", metadata=None):
    return [
        Message("assistant", [Content.from_function_call(identity, "read_file", arguments={"path": "a.py"})]),
        Message(
            "tool",
            [
                Content.from_function_result(
                    identity, result="def f(): pass", additional_properties={TOOL_RESULT_METADATA_KEY: metadata or {}}
                )
            ],
        ),
    ]


@pytest.mark.asyncio
async def test_evidence_handoff_delivery_and_duplicate_observer(owner):
    owner.tool_kinds["read_file"] = "filesystem.read"
    await capture(owner, observation(), maintain=False)
    owner.batch_size = 2
    assert "alone" in await owner.handoff("a.py inspected")
    owner.batch_size = 1
    await owner.handoff("a.py inspected")
    injection = InjectionMiddleware()
    pipeline = ChatMiddlewarePipeline(BriefChat(owner, injection), injection)
    sent = []

    async def provider(ctx):
        sent.append(ctx.messages)
        for observer in ctx.request_message_observers:
            observer(ctx.messages)
        return ChatResponse(messages=[Message("assistant", ["done"])])

    await pipeline.execute(ChatContext(None, [Message("user", ["go"])], {}, stream=False), provider)
    assert owner.flags.handoff_seen and owner.flags.rules_seen and owner.calls == 1
    assert owner.constants["SUBMISSION_RULES"] in "\n".join(m.text for m in sent[0])
    await capture(owner, observation(), maintain=False)
    await capture(owner, observation("blocked", {"compass_control": True}), maintain=False)
    assert len(owner.core.evidence.snapshot()) == 1
    owner.calls = MAX_CALLS - 1
    await pipeline.execute(ChatContext(None, [], {}, stream=False), provider)
    with pytest.raises(RuntimeError, match="budget"):
        await pipeline.execute(ChatContext(None, [], {}, stream=False), provider)
    assert len(sent) == 2 and owner.calls == MAX_CALLS


@pytest.mark.asyncio
async def test_guard_and_extension_owners_are_separate(owner, tmp_path):
    ctx = FunctionInvocationContext(SimpleNamespace(name="write_file", chrys_kind="filesystem.write"), {"path": "a.py"})
    call = AsyncMock()
    await ExplorationGuard(owner).process(ctx, call)
    call.assert_not_awaited()
    second = RunState("other", tmp_path, tmp_path, owner.constants, artifacts=Artifacts())
    owner.handoff, second.handoff = AsyncMock(return_value="first"), AsyncMock(return_value="second")
    from chrys.extensions import ToolContext

    context = ToolContext.for_testing(tmp_path)
    assert await make_extension(owner).tools[0].func(context, "a") == "first"
    assert await make_extension(second).tools[0].func(context, "b") == "second"
    owner.handoff.assert_awaited_once_with("a")
    second.handoff.assert_awaited_once_with("b")


@pytest.mark.asyncio
@pytest.mark.parametrize("calls,fail,expected", [(10, False, 2), (236, False, 1), (10, True, 1)])
async def test_submission_and_errors_preserve_session(owner, calls, fail, expected):
    owner.flags.started = owner.flags.handoff_seen = owner.flags.rules_seen = True
    owner.calls = calls
    owner.core.maintain = AsyncMock()
    host = SimpleNamespace(
        start=AsyncMock(), run=AsyncMock(side_effect=RuntimeError("synthetic") if fail else None, return_value="done")
    )
    run = CompassRun(owner, host)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic"):
            await run.run()
    else:
        assert await run.run() == "done"
    assert host.run.await_count == expected
    if expected == 2:
        assert owner.constants["SUBMISSION_CHECK"] in host.run.await_args_list[1].args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("late_start", [False, True])
async def test_missing_handoff_rejected(owner, late_start):
    owner.flags.started = owner.flags.rules_seen = late_start
    host = SimpleNamespace(start=AsyncMock(), run=AsyncMock(return_value="done"))
    run = CompassRun(owner, host)
    with pytest.raises(RuntimeError, match="not consumed"):
        await run.run()


@pytest.mark.asyncio
async def test_maintenance_actual_serialized_request(owner):
    import httpx
    from openai import AsyncOpenAI

    bodies = []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "created": 0,
                "model": ModelConfig().model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
            },
        )

    model = MaintenanceModel("offline-placeholder", owner.audit)
    await model.aclose()
    model.client = AsyncOpenAI(
        api_key="offline-placeholder",
        base_url="http://127.0.0.1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await model.complete(
            system_prompt="s",
            user_prompt="u",
            response_schema={"type": "object"},
            max_output_tokens=100,
            timeout_seconds=5,
        )
    finally:
        await model.aclose()
    assert bodies[0]["reasoning"] == {"enabled": False}
    assert bodies[0]["provider"]["only"] == ["deepseek"]
    profile = ModelConfig().profile()
    assert json.loads(profile["chat_options"])["extra_body"]["reasoning"] == {"effort": "max"}
    assert profile["max_output_tokens"] == 64000 and profile["http_max_retries"] == 2


@pytest.mark.asyncio
async def test_real_host_exploration_brief_and_second_turn(owner, monkeypatch, tmp_path):
    from chrys.orchestration.session_host import ChrysSessionHost
    from chrys.service.llm.mock import MockChatClient, MockResponse
    from chrys.service.state.store import JsonFileStateStore
    from tests.orchestration._session_host_helpers import (
        _make_agent_registry,
        _make_profile,
        _make_settings_and_models,
        _patch_runtime,
    )

    @tool
    async def read_file(path: str) -> str:
        """Read the synthetic public source."""
        return "def f(items): return list(items)"

    set_tool_kind(read_file, "filesystem.read")
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "r1", {"path": "a.py"})]),
            MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})]),
            MockResponse(tool_calls=[("read_file", "r2", {"path": "a.py"})]),
            MockResponse(text="implemented"),
            MockResponse(text="checked"),
        ]
    )
    _patch_runtime(monkeypatch, [client], [read_file])
    settings, models = _make_settings_and_models()
    profile = _make_profile()

    def attach():
        backend.attach()

    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=_make_agent_registry(profile),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        on_turn_started=attach,
    )
    backend = HeadlessBackend(owner, host, profile)
    run = CompassRun(owner, backend)
    try:
        result = await run.run()
        assert result.text == "checked"
        assert owner.calls == 5 and owner.flags.handoff_seen and owner.flags.check_done
        loaded = host.engine.current.require_loaded()
        backend.attach()
        assert sum(isinstance(m, BriefChat) for m in loaded.agent.middleware) == 1
        assert [t.name for t in loaded.agent.default_options["tools"]].count("start_compass_brief") == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_entrypoint_with_real_profiles_and_builtin_tools(tmp_path, monkeypatch):
    from chrys.orchestration.engine.build import builder
    from chrys.service.llm.mock import MockChatClient, MockResponse
    from compass import cli

    # No test tool packs: load the real profile and native read_file tool.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("def f(items): return list(items)\n")
    directory = tmp_path / "run"
    directory.mkdir()
    config = cli.prepare(directory)
    monkeypatch.setenv("CHRYS_CONFIG_DIR", str(config))
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-placeholder")
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "r1", {"path": str(workspace / "a.py")})]),
            MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})]),
            MockResponse(text="done"),
            MockResponse(text="checked"),
        ]
    )

    async def create_client(_profile, **kwargs):
        return client

    class Maintenance(FakeMaintenance):
        def __init__(self, key, audit):
            self.closed = False

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(builder, "create_client", create_client)
    monkeypatch.setattr(cli, "MaintenanceModel", Maintenance)
    result = await cli.execute(
        RunRequest("Return an empty list for empty input.", workspace, directory),
        source_constants(SOURCE_ROOT),
        "offline-placeholder",
    )
    assert result.text == "checked"
    assert client.call_count == 4
    events = [json.loads(line) for line in (directory / "adapter-events.jsonl").read_text().splitlines()]
    assert any(e["event"] == "brief_delivered" for e in events)
    assert any(e["event"] == "submission_check" and e["held"] for e in events)


@pytest.mark.asyncio
async def test_main_reasoning_max_reaches_serialized_http_body():
    import httpx
    from openai import AsyncOpenAI

    from chrys.service.llm.chat_completions import DeepSeekChatCompletionsClient
    from chrys.service.profiles.models.options import effective_chat_options
    from chrys.service.profiles.models.schema import ModelProfile

    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "created": 1,
                "model": ModelConfig().model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncOpenAI(api_key="offline-placeholder", base_url="http://127.0.0.1/v1", http_client=http)
        client = DeepSeekChatCompletionsClient(model=ModelConfig().model, sdk_client=sdk)
        try:
            await client.get_response(
                [Message("user", ["test"])],
                stream=False,
                options=effective_chat_options(ModelProfile(**ModelConfig().profile())),
            )
        finally:
            await sdk.close()
    assert bodies[0]["reasoning"] == {"effort": "max"}
    assert bodies[0]["provider"] == {"only": ["deepseek"], "allow_fallbacks": False, "require_parameters": True}
    assert bodies[0].get("max_tokens", bodies[0].get("max_completion_tokens")) == 64000


def test_result_contract_and_frozen_integrity(owner):
    from compass.runtime.records import RunResult

    assert len(json.loads((SOURCE_ROOT / "provenance.json").read_text())["original"]["files_sha256"]) == 72
    with pytest.raises(ValueError, match="final message"):
        RunResult(status="completed", text="", session_id="x")
    with pytest.raises(ValueError, match="error type"):
        RunResult(status="failed")
    assert RunResult(status="failed", error_type="BudgetExhausted").to_dict()["status"] == "failed"


@pytest.mark.asyncio
async def test_wall_deadline_and_cancel_save_state(owner):
    import asyncio

    owner.remaining = lambda: 0

    async def start():
        await asyncio.Future()

    backend = SimpleNamespace(start=start, run=AsyncMock())
    with pytest.raises(TimeoutError):
        await CompassRun(owner, backend).run()
    assert (owner.directory / "compass-state.json").is_file()
    backend.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_reused_provider_call_ids_keep_distinct_evidence_and_ignore_orphans(owner):
    from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY

    owner.tool_kinds["read_file"] = "filesystem.read"
    first, second = observation("reused"), observation("reused")
    for number, exchange in enumerate((first, second), 1):
        exchange[1].contents[0].additional_properties[ANALYTICS_ITEM_ID_KEY] = f"{number:032x}"
    # Same provider ID and identical result text are still two real tool executions.
    await capture(owner, first + second, maintain=False)
    assert len(owner.seen) == 2
    before = len(owner.core.evidence.snapshot())
    await capture(owner, first + second, maintain=False)
    assert len(owner.core.evidence.snapshot()) == before
    orphan = observation("reused")[1:]
    orphan[0].contents[0].additional_properties[ANALYTICS_ITEM_ID_KEY] = "f" * 32
    await capture(owner, orphan, maintain=False)
    assert "f" * 32 not in owner.seen
