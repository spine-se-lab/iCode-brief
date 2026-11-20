"""Native extension hooks/context/storage, with synthetic providers and no network."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from chrys.extensions import ToolCall, ToolContext, ToolError
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.tool_kinds import set_tool_kind
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY, tool_result_metadata_is_rejected
from chrys.kernel import Message, tool
from chrys.orchestration.invoker.contracts import RunIntent, RunRequest
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from compass.adapters.icode.interactive import STATE, InteractiveCompass
from compass.adapters.icode.middleware import ExplorationGuard, capture
from compass.adapters.icode.tools import extension
from compass.core.session import STATE_KEY
from compass.provenance import sha
from tests.integration.compass.test_integration import FakeMaintenance, observation
from tests.integration.compass.test_multiturn import handoff, host_for, read, write
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)
from tests.support.secure_files import plant_owner_only_bytes


@pytest.mark.asyncio
async def test_native_guard_blocks_early_write_then_allows_confirmed_brief(tmp_path, monkeypatch):
    host, client, _, reads, writes = await host_for(
        tmp_path,
        monkeypatch,
        [write(), read(), handoff(), write(), MockResponse(text="done"), MockResponse(text="checked")],
    )
    try:
        result = await host.run_until_final("Fix empty input", timeout=10)
        bindings = host.engine.current.require_loaded().bindings
        owner = bindings.compass.owner
        assert result.text == "checked" and writes == ["a.py"] and reads == ["a.py"]
        assert owner.calls == 6 and owner.flags.handoff_seen
        assert not any(isinstance(m, ExplorationGuard) for m in host.engine.current.require_loaded().agent.middleware)
        results = [c for m in client.call_history[-1][0] for c in m.contents if c.type == "function_result"]
        blocked = next(c for c in results if "Not executed" in str(c.result))
        assert tool_result_metadata_is_rejected(blocked.additional_properties[TOOL_RESULT_METADATA_KEY])
        assert len(owner.core.evidence.snapshot()) == 2  # read + actual write, never the held edit
        assert bindings.compass.directory.parts[-2:] == ("extension-data", "compass")
        assert owner.directory.parent == bindings.compass.directory
        first = "\n".join(m.text for m in client.call_history[0][0])
        assert '<extension_context extension="compass" name="workflow">' in first
        assert first.count("Compass workflow:") == 1
        assert not any("compass-workflow:" in str(m.additional_properties) for m in client.call_history[-1][0])
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_approval_edited_shell_arguments_are_guarded_again(tmp_path, monkeypatch):
    executed = []

    @tool
    async def shell(command: str) -> str:
        """Synthetic shell, records commands without executing them."""
        executed.append(command)
        return "synthetic"

    set_tool_kind(shell, "shell")
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("shell", "s1", {"command": "python -c 'print(1)'"})]),
            MockResponse(text="done"),
        ]
    )
    _patch_runtime(monkeypatch, [client], [shell])
    settings, models = _make_settings_and_models()
    profile = _make_profile(approval_default="require")
    profile.extensions.append("compass")
    host = ChrysSessionHost(
        profile_name=profile.name,
        settings=settings,
        agent_registry=_make_agent_registry(profile),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(tmp_path),
        surface=SessionSurface.TUI,
        approval_mode=ApprovalMode.MANUAL,
        allow_user_interaction=True,
    )
    approvals = []

    async def approve(request):
        approvals.append(request.args)
        await host.event_bus.publish(
            ApprovalResponse(
                request_id=request.request_id,
                approved=True,
                modified_args={"command": "rm a.py"},
            )
        )

    await host.event_bus.subscribe(ApprovalRequest, approve)
    try:
        failure = None
        try:
            await host.run_until_final("Inspect a.py", timeout=10)
        except RuntimeError as exc:
            failure = exc
        owner = host.engine.current.require_loaded().bindings.compass.owner
        assert approvals == [{"command": "python -c 'print(1)'"}] and executed == []
        assert owner.flags.write_requested and not owner.flags.handoff_seen
        assert owner.calls == 2 and not owner.core.evidence.snapshot()
        assert failure is not None and "handoff" in str(failure)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_extension_guard_without_main_runtime_fails_closed(tmp_path):
    call = ToolCall(
        name="read_file",
        kind="filesystem.read",
        call_id="r",
        arguments={"path": "a.py"},
        context=ToolContext.for_testing(tmp_path, session_id="unsupported"),
    )
    with pytest.raises(ToolError, match="active Compass main"):
        await extension.before_tool_hooks[0].func(call)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("collision", [False, True])
async def test_legacy_checkpoint_copies_evidence_preserves_budget_and_keeps_originals(tmp_path, version, collision):
    legacy = tmp_path / "session" / "compass"
    new_root = tmp_path / "session" / "extension-data" / "compass"
    profile = ModelProfile(id="fake", name="Fake")
    runtime = SimpleNamespace(cwd=str(tmp_path))
    bindings = SimpleNamespace(backend=SimpleNamespace(history_state={}))
    first = InteractiveCompass(profile, runtime, legacy)
    first.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    request = RunRequest(
        [Message("user", ["Fix empty input"])], RunIntent.FRESH, InvocationOrigin("turn", "s", "one", None)
    )
    await first.begin(request)
    owner = first.owner
    owner.model = FakeMaintenance()
    owner.tool_kinds = {"read_file": "filesystem.read"}
    messages = observation()
    messages[1].contents[0].result = "synthetic source line\n" * 1000
    await capture(owner, messages, maintain=False)
    owner.batch_size = 1
    await owner.handoff("Read a.py")
    owner.calls = 41
    owner.flags.handoff_seen = True
    owner.mark = replace(owner.mark, revision=3)
    owner.save()
    await first.aclose()
    saved = json.loads(json.dumps(bindings.backend.history_state))
    saved[STATE]["version"] = version
    envelope = saved[STATE]["core"][STATE_KEY]
    relative = envelope["evidence"][0]["raw_path"]
    assert relative
    original = owner.directory / "compass" / relative
    original_bytes = original.read_bytes()
    audit = (owner.directory / "adapter-events.jsonl").read_bytes()
    new_file = new_root / sha("one")[:24] / "compass" / relative
    if collision:
        new_file.parent.mkdir(parents=True, mode=0o700)
        plant_owner_only_bytes(new_file, b"different evidence")
    bindings.backend.history_state = saved
    second = InteractiveCompass(profile, runtime, new_root)
    second.bind(SimpleNamespace(middleware=[]), bindings, InjectionMiddleware())
    followup = replace(
        request, messages=[Message("user", ["continue"])], origin=InvocationOrigin("turn", "s", "two", None)
    )
    try:
        if collision:
            with pytest.raises(ValueError, match="Conflicting evidence"):
                await second.begin(followup)
            assert new_file.read_bytes() == b"different evidence"
        else:
            await second.begin(followup)
            assert second.owner.directory.parent == new_root
            assert second.owner.calls == 41 and second.owner.mark.revision == 3
            assert second.owner.flags.handoff_seen and second.owner.seen == owner.seen
            assert new_file.read_bytes() == original_bytes
            assert bindings.backend.history_state[STATE]["core"][STATE_KEY]["artifact_directory"] == str(
                new_file.parents[2]
            )
        assert original.read_bytes() == original_bytes
        assert (owner.directory / "adapter-events.jsonl").read_bytes() == audit
    finally:
        await second.aclose()
