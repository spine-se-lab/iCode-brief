"""Extension discovery, invocation isolation, migration, delivery and audit regressions."""

import asyncio
import json
from contextvars import copy_context
from dataclasses import replace

import pytest

from chrys.extensions import ToolContext, ToolError
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.platform.files import SecureFileError
from chrys.kernel import ChatResponse, Message
from chrys.kernel.middleware import ChatContext, ChatMiddlewarePipeline
from chrys.orchestration.invoker.origin import current_invocation_origin
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.extensions import discover_extensions, load_extensions
from compass.adapters.icode.interactive import InteractiveBriefChat
from compass.adapters.icode.middleware import BriefChat, PendingBrief, capture
from compass.adapters.icode.migrate import migrated_profile
from compass.adapters.icode.scope import current_runtime, invocation_scope
from compass.adapters.icode.storage import Artifacts
from compass.core.briefing.policy import DeliveryFacts
from compass.provenance import SOURCE_ROOT, source_constants
from compass.runtime.records import InvalidCompassRun
from compass.runtime.run_state import RunState
from compass.runtime.workflow import require_handoff
from tests.integration.compass.test_integration import FakeMaintenance, observation
from tests.support.secure_files import plant_owner_only_bytes


def state(tmp_path):
    return RunState(
        "Fix empty input", tmp_path, tmp_path, source_constants(SOURCE_ROOT), FakeMaintenance(), artifacts=Artifacts()
    )


@pytest.mark.asyncio
async def test_installed_extension_has_exactly_three_tools_and_no_global_runtime():
    catalog = discover_extensions()
    assert "compass" in catalog.sources
    loaded = (await load_extensions(["compass"]))[0]
    assert loaded.problem == ""
    assert [t.name for t in loaded.extension.tools] == [
        "start_compass_brief",
        "inspect_compass_brief",
        "update_compass_requirements",
    ]
    with pytest.raises(ToolError, match="active Compass main"):
        current_runtime(ToolContext.for_testing(cwd=".", session_id="s"))


@pytest.mark.asyncio
async def test_simultaneous_invocations_do_not_share_runtime():
    barrier = asyncio.Barrier(2)

    async def invoke(identity):
        runtime = object()
        origin = InvocationOrigin("turn", "same-session", identity, None)
        token = current_invocation_origin.set(origin)
        try:
            with invocation_scope(runtime, origin):
                await barrier.wait()
                assert current_runtime(ToolContext.for_testing(cwd=".", session_id="same-session")) is runtime
        finally:
            current_invocation_origin.reset(token)

    await asyncio.gather(invoke("first"), invoke("second"))


@pytest.mark.parametrize("kind", ["sub_agent", "workflow_node", "side_question", "other_turn", "other_session"])
def test_nested_or_foreign_invocation_cannot_borrow_main_scope(kind):
    main = InvocationOrigin("turn", "s", "main", None)
    actual = (
        replace(main, invocation_id="other")
        if kind == "other_turn"
        else replace(main, session_id="other")
        if kind == "other_session"
        else InvocationOrigin(kind, "s", "child", main)
    )
    token = current_invocation_origin.set(actual)
    try:
        with invocation_scope(object(), main), pytest.raises(ToolError):
            current_runtime(ToolContext.for_testing(cwd=".", session_id=actual.session_id))
    finally:
        current_invocation_origin.reset(token)


def test_copied_context_expires_after_run():
    origin = InvocationOrigin("turn", "s", "main", None)
    token = current_invocation_origin.set(origin)
    try:
        with invocation_scope(object(), origin):
            inherited = copy_context()
        with pytest.raises(ToolError):
            inherited.run(current_runtime, ToolContext.for_testing(cwd=".", session_id="s"))
    finally:
        current_invocation_origin.reset(token)


@pytest.mark.parametrize("enabled", [True, False])
def test_profile_migration_is_explicit_idempotent_and_preserves_other_values(enabled):
    original = {
        "name": "Mine",
        "extensions": ["other"],
        "metadata": {"compass_enabled": enabled, "note": 3},
        "unknown": "keep",
    }
    result = migrated_profile(original)
    assert result["extensions"] == (["other", "compass"] if enabled else ["other"])
    assert result["metadata"] == {"note": 3}
    assert result["unknown"] == "keep" and original["metadata"]["compass_enabled"] is enabled
    assert migrated_profile(result) == result


def test_migration_does_not_enable_compass_for_acp():
    with pytest.raises(ValueError, match="ACP"):
        migrated_profile({"metadata": {"compass_enabled": True}, "acp": {"command": "external"}})


@pytest.mark.parametrize("chat_type", [BriefChat, InteractiveBriefChat])
def test_closing_message_never_confirms_a_brief(tmp_path, chat_type):
    owner = state(tmp_path)
    owner.flags.started = True
    owner.calls = 201
    chat = chat_type(owner, InjectionMiddleware())
    chat.pending = PendingBrief("closing only", DeliveryFacts(revision=9), {"R1"}, "closing-budget", "closing")
    chat.confirm_delivery()
    assert owner.flags.closing_seen and not owner.flags.handoff_seen
    assert owner.mark.revision == 0 and not owner.conflicts
    with pytest.raises(InvalidCompassRun):
        require_handoff(owner)
    events = [json.loads(line)["event"] for line in (tmp_path / "adapter-events.jsonl").read_text().splitlines()]
    assert events == ["closing_delivered"]


@pytest.mark.asyncio
async def test_late_handoff_delivers_real_brief_before_closing(tmp_path):
    owner = state(tmp_path)
    owner.tool_kinds["read_file"] = "filesystem.read"
    await capture(owner, observation(), maintain=False)
    owner.batch_size = 1
    await owner.handoff("Read a.py")
    owner.calls = 201
    injection = InjectionMiddleware()
    chat = BriefChat(owner, injection)
    sent = []

    async def provider(context):
        sent.append("\n".join(message.text for message in context.messages))
        for observe in context.request_message_observers:
            observe(context.messages)
        return ChatResponse(messages=[Message("assistant", ["done"])])

    pipeline = ChatMiddlewarePipeline(chat, injection)
    await pipeline.execute(ChatContext(None, [Message("user", ["go"])], {}, stream=False), provider)
    require_handoff(owner)
    assert not owner.flags.closing_seen
    await pipeline.execute(ChatContext(None, [Message("user", ["go"])], {}, stream=False), provider)
    assert owner.flags.closing_seen
    events = [json.loads(line) for line in (tmp_path / "adapter-events.jsonl").read_text().splitlines()]
    delivered = [e["event"] for e in events if e["event"] in {"brief_delivered", "closing_delivered"}]
    assert delivered == ["brief_delivered", "closing_delivered"]
    assert sent[0] != sent[1]


def test_audit_append_uses_platform_security_and_preserves_existing_lines(tmp_path):
    owner = state(tmp_path)
    owner.audit("first", value="中文")
    owner.audit("second")
    path = tmp_path / "adapter-events.jsonl"
    assert [json.loads(line)["event"] for line in path.read_text().splitlines()] == ["first", "second"]
    assert path.read_bytes().endswith(b"\n")


def test_audit_rejects_symlink_target(tmp_path):
    from tests.support.symlinks import symlink_or_skip

    target = tmp_path / "target"
    plant_owner_only_bytes(target, b"unchanged")
    symlink_or_skip(tmp_path / "adapter-events.jsonl", target)
    with pytest.raises((SecureFileError, OSError)):
        state(tmp_path).audit("must not append")
    assert target.read_text() == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["legacy", "missing", "duplicate_id", "tool_collision"])
async def test_enabled_but_unusable_compass_fails_before_model_requests(tmp_path, monkeypatch, problem):
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.kernel import tool
    from chrys.orchestration.session_host import ChrysSessionHost
    from chrys.service import extensions
    from chrys.service.llm.mock import MockChatClient, MockResponse
    from chrys.service.state.store import JsonFileStateStore
    from tests.orchestration._session_host_helpers import (
        _make_agent_registry,
        _make_profile,
        _make_settings_and_models,
        _patch_runtime,
    )
    from tests.support.extensions import install_file_extension

    @tool
    def start_compass_brief() -> str:
        """An unrelated tool occupying the name."""
        return "unrelated"

    client = MockChatClient(responses=[MockResponse(text="must not run")])
    _patch_runtime(monkeypatch, [client], [start_compass_brief] if problem == "tool_collision" else [])
    settings, models = _make_settings_and_models()
    profile = _make_profile()
    profile.extensions.append("compass")
    if problem == "legacy":
        profile.metadata["compass_enabled"] = True
    elif problem == "missing":
        monkeypatch.setattr(extensions, "_distribution_extensions", list)
    elif problem == "duplicate_id":
        install_file_extension(monkeypatch, "compass")
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
        with pytest.raises(ValueError, match=r"Migrate the old Compass|failed to assemble"):
            await host.run_until_final("go")
        assert client.call_count == 0
    finally:
        await host.shutdown()


def test_migration_command_previews_then_updates_only_the_requested_profile(tmp_path, monkeypatch, capsys):
    import sys

    import yaml

    from compass.adapters.icode.migrate import main

    path = tmp_path / "agent.yaml"
    original = "metadata:\n  compass_enabled: true\n  note: private-marker\nname: Mine\n"
    Artifacts.write_text(path, original)
    monkeypatch.setattr(sys, "argv", ["migrate", str(path)])
    main()
    assert path.read_text() == original
    assert "Migration available" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["migrate", str(path), "--apply"])
    main()
    assert "private-marker" not in capsys.readouterr().out
    assert yaml.safe_load(path.read_text()) == {
        "metadata": {"note": "private-marker"},
        "name": "Mine",
        "extensions": ["compass"],
    }
    migrated = path.read_bytes()
    main()
    assert path.read_bytes() == migrated
    assert "No migration needed" in capsys.readouterr().out
