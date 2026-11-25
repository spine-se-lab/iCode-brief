"""Compass remains main-only without blocking ordinary child tools."""

import pytest

from chrys.foundation.events.types import UserMessage
from chrys.service.llm.mock import MockResponse
from tests.orchestration.sub_agents._pipeline import _explore_result, _make_ctx, _sub_tool_call


@pytest.mark.asyncio
async def test_native_child_with_compass_runs_ordinary_tool_but_rejects_compass_tool(tmp_path, agent_engine):
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("wait a moment"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(tool_calls=[("update_compass_brief", "brief", {})]),
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
        assert "current main session" in results[0]
        assert results[1] == "Slept for 0 seconds."
        assert _explore_result(ctx).result == "Child done."
        assert not list(tmp_path.rglob("adapter-input.jsonl"))
    finally:
        await ctx.cleanup()
