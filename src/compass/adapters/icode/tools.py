"""Compass tools shared by the main-session and headless adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

from chrys.extensions import Extension, ToolCall, ToolContext, ToolError, TurnContext
from compass.runtime.prompts import INTERACTIVE_WORKFLOW

from .middleware import check_edit
from .scope import current_runtime, is_non_main_invocation

if TYPE_CHECKING:
    from compass.runtime.run_state import RunState

    from .interactive import InteractiveCompass


def make_extension(owner: RunState | InteractiveCompass | None = None, *, interactive: bool = False) -> Extension:
    extension = Extension()

    @extension.tool(requires_approval=False)
    async def start_compass_brief(
        ctx: ToolContext,
        exploration_summary: Annotated[str, "Repository findings, paths and unresolved assumptions"],
    ) -> str:
        """Finish repository exploration and initialize Compass. Call alone before editing."""
        runtime = owner if owner is not None else current_runtime(ctx)
        return await runtime.handoff(exploration_summary)

    @extension.tool(requires_approval=False)
    async def inspect_compass_brief(
        ctx: ToolContext,
        refs: Annotated[list[str], "Requirement, claim or evidence IDs; empty for overview"],
    ) -> str:
        """Read full Compass records. This is context, not new repository evidence."""
        runtime = owner if owner is not None else current_runtime(ctx)
        return await runtime.core.inspect(refs)

    if interactive:

        @extension.context
        async def workflow(ctx: TurnContext) -> str:
            """Stable turn context only; dynamic Briefs require per-request delivery."""
            if is_non_main_invocation():
                return ""
            # iCode may ask for child contexts before binding the child's origin.
            # Keep this instruction conditional even when preparation sees the parent.
            return (
                "Compass applies only to the main agent. If you are a sub-agent, workflow node or side-question "
                "invocation, use your ordinary tools normally; do not call Compass tools or follow the "
                "main-agent workflow below. For the main agent only:\n" + INTERACTIVE_WORKFLOW
            )

        @extension.before_tool
        async def guard_edit(call: ToolCall) -> None:
            # Reused profiles do not opt child invocations into the parent's Compass task.
            if is_non_main_invocation():
                return
            runtime = cast("InteractiveCompass", owner if owner is not None else current_runtime(call.context))
            if runtime.owner is None:
                raise ToolError("Compass turn not started; the tool did not run.")
            denial = check_edit(runtime.owner, name=call.name, kind=call.kind, arguments=call.arguments)
            if denial is not None:
                raise ToolError(denial)

        @extension.tool(requires_approval=False)
        async def update_compass_requirements(ctx: ToolContext) -> str:
            """Apply user-added/changed requirements to the active task. Call alone before editing.

            Uses recorded user text, never a model-written replacement task. Not needed for questions or 'continue'.
            """
            runtime = owner if owner is not None else current_runtime(ctx)
            return await cast("InteractiveCompass", runtime).update_requirements()

    return extension


# Normal entry-point discovery caches declarations, never a shared runtime.
extension = make_extension(interactive=True)
