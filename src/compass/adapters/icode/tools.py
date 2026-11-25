"""Compass tools shared by the main-session and headless adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated

from chrys.extensions import Extension, ToolCall, ToolContext, ToolResult, TurnContext
from compass.provenance import dump

from .information import EVIDENCE_KINDS
from .scope import is_non_main_invocation
from .tool_session import tool_session

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from compass.evaluation.run_state import RunState


def make_extension(owner: RunState) -> Extension:
    """Independent evaluation tools bound to one RunState; never discovered by iCode."""
    extension = Extension()

    @extension.tool(requires_approval=False)
    async def start_compass_brief(
        ctx: ToolContext,
        exploration_summary: Annotated[str, "Repository findings, paths and unresolved assumptions"],
    ) -> str:
        """Finish repository exploration and initialize Compass. Call alone before editing."""
        return await owner.handoff(exploration_summary)

    @extension.tool(requires_approval=False)
    async def inspect_compass_brief(
        ctx: ToolContext,
        refs: Annotated[list[str], "Requirement, claim or evidence IDs; empty for overview"],
    ) -> str:
        """Read full Compass records. This is context, not new repository evidence."""
        return await owner.core.inspect(refs)

    return extension


def make_tool_extension() -> Extension:
    """The product interface: tools, observation and automatic Brief feedback."""
    tools = Extension()

    @tools.context
    async def compass_tools(ctx: TurnContext) -> str | None:
        if is_non_main_invocation():
            return None
        return (
            "Use inspect_compass_brief(refs) to look up Goal, Requirement, Constraint, Claim or Evidence by ID; "
            "pass [] for the current Brief. "
            "For coding tasks, call update_compass_requirements() at the start, after repository exploration, "
            "and when the user adds or changes requirements. "
            "Call update_compass_brief() after new evidence and again after the final tests, before reporting "
            "completion; it refreshes outdated requirements from captured user text and returns the current Brief, "
            "ordinary tool results also trigger maintenance when due and carry its Brief back automatically."
        )

    @tools.tool(requires_approval=False)
    async def inspect_compass_brief(
        ctx: ToolContext,
        refs: Annotated[
            list[str], "IDs: goal, R1 requirement, K1 constraint, C-001 claim, E-001 evidence; [] for Brief"
        ],
    ) -> str:
        """Query Compass records by ID. Read-only: no extraction or maintenance call."""
        return await tool_session(ctx).inspect(refs)

    @tools.tool(requires_approval=False)
    async def update_compass_requirements(ctx: ToolContext) -> str:
        """Create or revise Goal/Requirements/Constraints from captured user text.

        Call initially, after repository exploration, and whenever the user adds or changes requirements. Original user
        messages are captured by the host extension, not rewritten as arguments.
        Repeated calls skip extraction when user text and repository context are
        unchanged. Existing claims and evidence are preserved.
        """
        return await tool_session(ctx).update_requirements()

    @tools.tool(requires_approval=False)
    async def update_compass_brief(ctx: ToolContext) -> str:
        """Evaluate the core maintenance policy and return the current Brief.

        Missing or outdated Goal/Requirements are first refreshed from captured user text.
        Call after tests and before reporting completion. Revised requirements
        are reassessed using recorded evidence; unprocessed test results do not
        wait for the ordinary batch window. Unchanged requirements with no new
        evidence reuse the current Brief. Initialization alone does not force maintenance.
        """
        return await tool_session(ctx).update_brief()

    @tools.after_tool
    async def observe_execution(call: ToolCall, result: ToolResult) -> None:
        if is_non_main_invocation():
            return
        try:
            await tool_session(call.context).observe(call, result)
        except Exception as exc:
            # Observation must never hide the host's actual tool result.
            logger.warning("Compass evidence capture failed (%s); original result retained", type(exc).__name__)

    @tools.after_tool(timeout=250)
    async def maintain_after_execution(call: ToolCall, result: ToolResult) -> str | None:
        if is_non_main_invocation() or result.rejected or call.kind not in EVIDENCE_KINDS:
            return None
        try:
            # Finish inside the host hook timeout: a host timeout withholds the
            # original result. External cancellation still propagates normally.
            async with asyncio.timeout(240):
                outcome = await tool_session(call.context).gate.automatic_brief()
            if outcome is None:
                return None
            logger.info(
                "Compass automatic Brief returned: reason=%s revision=%s processed=%s latest=%s",
                outcome["reason"],
                outcome["revision"],
                outcome["processed_evidence_seq"],
                outcome["latest_evidence_seq"],
            )
            return result.text + "\n\n[Compass automatic check]\n" + dump(outcome)
        except Exception as exc:
            logger.warning("Compass automatic maintenance failed (%s); original result retained", type(exc).__name__)
            return result.text + (
                "\n\n[Compass automatic check unavailable; the original tool result is preserved. "
                "Evidence remains pending; call update_compass_brief() to retry.]"
            )

    return tools


# The independent evaluation adapter still uses make_extension(owner) above.
# Normal discovery exposes only this tool interface, never the evaluation gate.
extension = make_tool_extension()
