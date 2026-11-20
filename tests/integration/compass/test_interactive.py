"""New interactive Compass cases only; native hosts and widgets, offline models."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.tool_kinds import set_tool_kind
from chrys.kernel import AgentSession, ChatResponse, Message, tool
from chrys.orchestration.invoker.contracts import RunIntent, RunRequest
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from compass.adapters.icode.interactive import STATE, InteractiveCompass
from compass.adapters.icode.interactive_model import SelectedModel
from compass.adapters.icode.middleware import BriefChat
from compass.ports import ModelReply
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)
from tests.support.tui_helpers import WidgetApp


class CompassWidgetApp(TuiVariableDefaultsMixin, WidgetApp):
    pass


async def maintenance(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
    if response_schema["title"] == "IssueRequirementExtraction":
        data = {
            "requirements": [{"id": "R1", "text": "Return an empty list for empty input."}],
            "constraints": [],
            "signature_changes": [],
        }
    else:
        data = {k: [] for k in ("new_claims", "augment", "merge", "conflicts", "contradiction_scan", "deactivate")}
    return ModelReply(json.dumps(data))


@pytest.mark.asyncio
async def test_compass_extension_roundtrip_and_off_preserves_other_metadata(tmp_path):
    from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel
    from tests.app.tui.screens.test_agent_config_extensions import _switches
    from tests.support.waiting import wait_for

    profile = AgentProfile(name="Mine", metadata={"other": "keep"})
    app = CompassWidgetApp(lambda: ToolsConfigPanel(profile.tools, extensions=profile.extensions))
    async with app.run_test(size=(100, 60)) as pilot:
        await wait_for(lambda: "compass" in _switches(app), pilot=pilot, timeout=10, description="Compass extension")
        toggle = _switches(app)["compass"]
        assert toggle.value is False
        toggle.value = True
        profile.extensions = app.query_one(ToolsConfigPanel).get_config().extensions
        path = save_profile(profile, target_dir=tmp_path)
        restored = load_profile_from_yaml(path)
        assert restored.metadata == {"other": "keep"}
        assert restored.extensions == ["compass"]
        toggle.value = False
        assert app.query_one(ToolsConfigPanel).get_config().extensions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_native_main_session_explores_brief_then_submission_check(tmp_path, monkeypatch, stream):
    @tool
    async def read_file(path: str) -> str:
        """Read a synthetic source file."""
        return "def f(items): return list(items)"

    set_tool_kind(read_file, "filesystem.read")
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "r1", {"path": "a.py"})]),
            MockResponse(tool_calls=[("start_compass_brief", "h1", {"exploration_summary": "a.py inspected"})]),
            MockResponse(text="implemented"),
            MockResponse(text="checked"),
        ]
    )
    _patch_runtime(monkeypatch, [client], [read_file])
    monkeypatch.setattr(SelectedModel, "complete", maintenance)
    settings, models = _make_settings_and_models()
    models.get("mock-profile").stream = stream
    profile = _make_profile()
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
    try:
        result = await host.run_until_final("Return an empty list for empty input.")
        assert result.text == "checked"
        loaded = host.engine.current.require_loaded()
        bridge = loaded.bindings.compass
        assert bridge.owner.calls == 4
        assert bridge.owner.flags.handoff_seen and bridge.owner.flags.check_done
        assert bridge.model.profile.id == "mock-profile"
        assert sum(isinstance(m, BriefChat) for m in loaded.agent.middleware) == 1
        assert [t.name for t in loaded.agent.default_options["tools"]].count("start_compass_brief") == 1
        assert loaded.bindings.backend.history_state[STATE]["calls"] == 4
        assert any("Compass workflow" in m.text for m in client.call_history[0][0])
        assert any("Compass" in m.text for m in client.call_history[2][0])
        assert client.call_count == 4
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_disabled_main_session_has_no_compass_and_no_extra_calls(tmp_path, monkeypatch):
    client = MockChatClient(responses=[MockResponse(text="ordinary")])
    _patch_runtime(monkeypatch, [client], [])
    settings, models = _make_settings_and_models()
    profile = _make_profile()
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
        result = await host.run_until_final("hello")
        assert result.text == "ordinary" and client.call_count == 1
        loaded = host.engine.current.require_loaded()
        assert loaded.bindings.compass is None
        assert "start_compass_brief" not in [t.name for t in loaded.agent.default_options["tools"]]
        assert STATE not in loaded.bindings.backend.history_state
        assert not list((tmp_path / "sessions").rglob("compass"))
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_maintenance_uses_selected_connection_and_preserves_profile(monkeypatch):
    from compass.adapters.icode import interactive_model

    profile = ModelProfile(
        id="selected",
        name="UI choice",
        provider="deepseek-openai",
        model_id="private-alias",
        base_url="https://openrouter.ai/api/v1",
        api_key="{{TEST_MODEL_KEY}}",
        chat_options=json.dumps({"extra_body": {"provider": {"only": ["deepseek"]}, "reasoning": {"effort": "max"}}}),
    )
    original = deepcopy(profile)
    client = SimpleNamespace(
        get_response=AsyncMock(
            return_value=ChatResponse(
                messages=[Message("assistant", ['{"ok":true}'])],
                finish_reason="stop",
                usage_details={"input_token_count": 4},
            )
        ),
        aclose=AsyncMock(),
    )
    create = AsyncMock(return_value=client)
    monkeypatch.setattr(interactive_model, "create_client", create)
    usage = []
    model = SelectedModel(profile, usage.append)
    try:
        response = await model.complete(
            system_prompt="s",
            user_prompt="u",
            response_schema={"type": "object"},
            max_output_tokens=200,
            timeout_seconds=5,
        )
        assert response.text == '{"ok":true}'
        assert create.await_args.args[0] == original
        options = client.get_response.await_args.kwargs["options"]
        assert options["extra_body"]["provider"] == {"only": ["deepseek"]}
        assert options["extra_body"]["reasoning"] == {"enabled": False}
        assert options["max_tokens"] == 200 and options["temperature"] == 0
        assert profile == original
        assert usage == [{"input_token_count": 4}]
    finally:
        await model.aclose()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebuild_restores_same_turn_budget_and_switches_model(tmp_path):
    runtime = SimpleNamespace(cwd=str(tmp_path))
    session = AgentSession()
    bindings = SimpleNamespace(backend=SimpleNamespace(session=session, history_state={}))
    request = RunRequest([Message("user", ["task"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "t", None))
    first = InteractiveCompass(ModelProfile(id="a", name="A", model_id="A"), runtime, tmp_path)
    first.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    await first.begin(request)
    first.owner.calls = 42
    first.owner.seen.add("same-evidence")
    first.owner.flags.handoff_seen = True
    first.owner.save()
    await first.aclose()
    from chrys.service.state.serializers import deserialize_state, serialize_state

    bindings.backend.history_state = deserialize_state(
        json.loads(json.dumps(serialize_state(bindings.backend.history_state)))
    )
    assert bindings.backend.history_state[STATE]["calls"] == 42
    second = InteractiveCompass(ModelProfile(id="b", name="B", model_id="B"), runtime, tmp_path)
    second.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    try:
        await second.begin(request)
        assert second.owner.calls == 42 and second.owner.seen == {"same-evidence"}
        assert second.owner.flags.handoff_seen
        assert second.model.profile.model_id == "B"
        assert second.owner.wall_started == first.owner.wall_started
        await second.begin(
            RunRequest([Message("user", ["new task"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "t2", None))
        )
        assert second.owner.calls == 0 and second.owner.task == "new task"
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_full_agent_settings_save_button_persists_compass(monkeypatch):
    from textual.widgets import Button

    from chrys.foundation.platform import get_platform
    from tests.app.tui.screens import _agent_config_support as support
    from tests.app.tui.screens._agent_config_support import (
        _activate_agent_config_tab,
        _wait_for_hydrated,
        make_profile,
        open_agent_config,
        registry_with,
    )
    from tests.app.tui.screens.test_agent_config_extensions import _switches
    from tests.support.tui_helpers import PlaceholderHostApp
    from tests.support.waiting import wait_for

    class ConfigHost(TuiVariableDefaultsMixin, PlaceholderHostApp):
        pass

    monkeypatch.setattr(support, "PlaceholderHostApp", ConfigHost)
    registry = registry_with(make_profile("Mine", display_name="Mine", description="Coding"))
    async with open_agent_config(registry, current_profile="Mine") as (screen, pilot):
        await _activate_agent_config_tab(screen, pilot, "tools")
        await wait_for(lambda: "compass" in _switches(screen), pilot=pilot, timeout=10, description="Compass extension")
        _switches(screen)["compass"].value = True
        save = screen.query_one("#ac-save", Button)
        await wait_for(lambda: not save.disabled, pilot=pilot, timeout=10, description="Compass enables Save")
        save.press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)
    assert registry.get("Mine").extensions == ["compass"]
    saved = load_profile_from_yaml(get_platform().config_dir / "agents" / "Mine.yaml")
    assert saved.extensions == ["compass"]
    assert "compass_enabled" not in saved.metadata


@pytest.mark.asyncio
async def test_native_model_switch_rebuild_and_compass_off(tmp_path, monkeypatch):
    from chrys.foundation.events.types import SetModelProfile

    clients = [MockChatClient(responses=[MockResponse(text="unused")]) for _ in range(3)]
    _patch_runtime(monkeypatch, clients, [])
    settings, models = _make_settings_and_models()
    models.register(ModelProfile(id="second", name="Second", provider="mock", model_id="second"))
    profile = _make_profile()
    profile.extensions.append("compass")
    registry = _make_agent_registry(profile)
    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=registry,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        surface=SessionSurface.TUI,
    )
    try:
        await host.start()
        old = host.engine.current.require_loaded().bindings.compass
        assert old.model.profile.id == "mock-profile"
        await host.engine._on_set_model_profile(SetModelProfile(profile_id="second"))
        new = host.engine.current.require_loaded().bindings.compass
        assert new is not old and old.model.closed
        assert new.model.profile.id == "second"
        assert (
            sum(
                t.name == "start_compass_brief"
                for t in host.engine.current.require_loaded().agent.default_options["tools"]
            )
            == 1
        )
        profile.extensions.remove("compass")
        await host.engine._on_set_model_profile(SetModelProfile(profile_id="mock-profile"))
        assert host.engine.current.require_loaded().bindings.compass is None
        assert new.model.closed
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid", "failed", "cancelled", "budget"])
async def test_interactive_failure_budget_and_cancellation_do_not_retry(tmp_path, mode):
    import asyncio

    from chrys.foundation.models.invocations import PassHandle
    from chrys.orchestration.invoker.contracts import Failed, FailureDisposition, Ok, StopCause, UsageDelta
    from chrys.orchestration.invoker.evidence import ZERO_COUNT, PassEvidence

    bridge = InteractiveCompass(ModelProfile(id="a", name="A"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    request = RunRequest([Message("user", ["task"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "t", None))
    # A genuine backend success, before Compass's handoff validity decision.
    outcome = Ok(
        handle=PassHandle("t", "p"),
        usage=UsageDelta(),
        effects=PassEvidence("t", "p", ZERO_COUNT, ZERO_COUNT, ZERO_COUNT, False),
        stop=StopCause.COMPLETED,
        continuation=None,
        segments=(),
    )
    run = AsyncMock(return_value=outcome)
    bindings = SimpleNamespace(backend=SimpleNamespace(history_state={}, run=run), report_policy_failure=AsyncMock())
    injection = InjectionMiddleware()
    injection.queue("keep user input", injection_id="user-note")
    bridge.bind(SimpleNamespace(middleware=[]), bindings, injection)
    await bridge.begin(request)
    if mode == "invalid":
        bridge.owner.flags.handoff_requested = True
    if mode == "cancelled":
        run.side_effect = asyncio.CancelledError()
    elif mode == "failed":
        # Non-Ok outcomes are passed through untouched, with no second pass.
        run.return_value = Failed(
            handle=outcome.handle,
            usage=outcome.usage,
            effects=outcome.effects,
            stop=StopCause.FAILED,
            continuation=None,
            disposition=FailureDisposition.CALLER_DECISION,
            error="synthetic transport failure",
        )
    elif mode == "budget":
        bridge.owner.calls = 250

        async def hit_budget(_request):
            bridge.owner.check_budget()

        run.side_effect = hit_budget
    try:
        if mode == "failed":
            assert await bridge.run(request) is run.return_value
        elif mode == "invalid":
            result = await bridge.run(request)
            assert isinstance(result, Failed)
            assert result.effects == outcome.effects and result.usage == outcome.usage
            bindings.report_policy_failure.assert_awaited_once()
        else:
            error = asyncio.CancelledError if mode == "cancelled" else RuntimeError
            with pytest.raises(error):
                await bridge.run(request)
        assert run.await_count == 1
        assert not bridge.owner.flags.check_done
        assert STATE in bindings.backend.history_state
    finally:
        await bridge.aclose()
    assert injection.pending_by_id("user-note") is not None
    assert bridge.invocation_id == "t"


@pytest.mark.asyncio
@pytest.mark.parametrize("calls, expected_passes", [(10, 2), (235, 2), (236, 1), (250, 1)])
async def test_submission_check_budget_threshold_unchanged(tmp_path, calls, expected_passes):
    from chrys.foundation.models.invocations import PassHandle
    from chrys.orchestration.invoker.contracts import Ok, StopCause, UsageDelta
    from chrys.orchestration.invoker.evidence import ZERO_COUNT, PassEvidence

    bridge = InteractiveCompass(ModelProfile(id="a", name="A"), SimpleNamespace(cwd=str(tmp_path)), tmp_path)
    request = RunRequest([Message("user", ["task"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "t", None))
    outcome = Ok(
        handle=PassHandle("t", "p"),
        usage=UsageDelta(),
        effects=PassEvidence("t", "p", ZERO_COUNT, ZERO_COUNT, ZERO_COUNT, False),
        stop=StopCause.COMPLETED,
        continuation=None,
        segments=(),
    )
    run = AsyncMock(return_value=outcome)
    bridge.bind(
        SimpleNamespace(middleware=[]),
        SimpleNamespace(backend=SimpleNamespace(history_state={}, run=run)),
        InjectionMiddleware(),
    )
    await bridge.begin(request)
    bridge.owner.calls = calls
    bridge.owner.flags.started = bridge.owner.flags.handoff_seen = bridge.owner.flags.rules_seen = True
    bridge.owner.core.maintain = AsyncMock()
    try:
        assert await bridge.run(request) is outcome
        assert run.await_count == expected_passes and bridge.owner.flags.check_done
        if expected_passes == 2:
            continuation = run.await_args.args[0]
            assert continuation.intent is RunIntent.CONTINUE
            assert continuation.messages[0].text.startswith(bridge.constants["SUBMISSION_CHECK"])
        if calls == 250:
            bridge.owner.core.maintain.assert_not_awaited()
    finally:
        await bridge.aclose()
