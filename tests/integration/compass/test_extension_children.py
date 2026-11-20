"""Compass remains main-only without blocking ordinary child tools."""

from types import SimpleNamespace

import pytest

from chrys.extensions import ToolCall, ToolContext, ToolError, TurnContext
from chrys.foundation.events.types import UserMessage
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.invoker.origin import current_invocation_origin
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.scope import current_runtime, invocation_scope
from compass.adapters.icode.tools import extension
from tests.orchestration.sub_agents._pipeline import _explore_result, _make_ctx, _sub_tool_call


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sub_agent", "workflow_node", "side_question"])
@pytest.mark.parametrize("tool_kind", ["filesystem.read", "filesystem.write", "shell"])
async def test_non_main_tools_skip_compass_without_borrowing_parent(tmp_path, kind, tool_kind):
    main = InvocationOrigin("turn", "s", "main", None)
    child = InvocationOrigin(kind, "s", "child", main)
    context = ToolContext.for_testing(tmp_path, session_id="s")
    parent_state = SimpleNamespace(owner=None)
    token = current_invocation_origin.set(child)
    try:
        with invocation_scope(parent_state, main):
            await extension.before_tool_hooks[0].func(
                ToolCall(name="ordinary_tool", kind=tool_kind, call_id="call", arguments={}, context=context)
            )
            assert await extension.contexts[0].func(TurnContext.for_testing(tmp_path, session_id="s")) == ""
            with pytest.raises(ToolError, match="active Compass main"):
                current_runtime(context)
            assert parent_state.owner is None
    finally:
        current_invocation_origin.reset(token)


@pytest.mark.asyncio
async def test_native_child_with_compass_runs_ordinary_tool_but_rejects_compass_tool(tmp_path, agent_engine):
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("wait a moment"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(tool_calls=[("start_compass_brief", "brief", {"exploration_summary": "child task"})]),
            MockResponse(tool_calls=[("sleep", "sleep", {"seconds": 0, "reason": "wait"})]),
            MockResponse(text="Child done."),
        ],
        sub_builtins=["sleep"],
        sub_extensions=["compass"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()
        results = [
            c.result for m in ctx.sub_client.call_history[-1][0] for c in m.contents if c.type == "function_result"
        ]
        assert len(results) == 2
        assert "active Compass main" in results[0]
        assert results[1] == "Slept for 0 seconds."
        assert _explore_result(ctx).result == "Child done."
        sent = "\n".join(m.text or "" for messages, _ in ctx.sub_client.call_history for m in messages)
        # Context preparation precedes child-origin binding in this host version.
        assert "If you are a sub-agent" in sent
        assert "do not call Compass tools or follow the main-agent workflow below" in sent
        assert not list(tmp_path.rglob("adapter-events.jsonl"))
    finally:
        await ctx.cleanup()
