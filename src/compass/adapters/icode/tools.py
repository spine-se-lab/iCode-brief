"""Compass tools shared by the main-session and headless adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

from chrys.extensions import Extension, ToolContext

if TYPE_CHECKING:
    from compass.runtime.run_state import RunState

    from .interactive import InteractiveCompass


def make_extension(owner: RunState | InteractiveCompass, *, interactive: bool = False) -> Extension:
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

    if interactive:

        @extension.tool(requires_approval=False)
        async def update_compass_requirements(ctx: ToolContext) -> str:
            """Apply user-added/changed requirements to the active task. Call alone before editing.

            Uses recorded user text, never a model-written replacement task. Not needed for questions or 'continue'.
            """
            return await cast("InteractiveCompass", owner).update_requirements()

    return extension
