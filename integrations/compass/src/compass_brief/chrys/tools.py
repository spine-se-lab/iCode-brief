# Copyright (c) 2026 Chrys. All rights reserved.

"""Tool declarations; the session Feature intercepts calls against live state."""

from typing import Annotated

from chrys.service.tools.kinds import tool


@tool(kind="compass_brief")
async def inspect_compass_brief(
    refs: Annotated[list[str], "C-001, E-001, R1, K1, or goal; empty returns current Brief."] = [],  # noqa: B006
) -> str:
    """Read the current task's claims and their execution evidence."""
    return "Error: Compass Brief is not bound to this session."


@tool(kind="compass_brief")
async def start_compass_brief(
    issue: Annotated[str, "Full user task; omit when the original task is already bound."] = "",
    exploration_summary: Annotated[
        str, "Exploration findings: relevant entry points/files, behavior and boundaries, and unresolved assumptions."
    ] = "",
) -> str:
    """Finish repository exploration and generate G/R/K plus Brief. Call alone and review the result before editing."""
    return "Error: Compass Brief is not bound to this session."


@tool(kind="compass_brief")
async def revise_compass_brief(
    issue: Annotated[str, "Updated full task, requirements and constraints supplied by the user."],
) -> str:
    """Apply a user task correction, preserving evidence and claims; re-evaluate coverage."""
    return "Error: Compass Brief is not bound to this session."


@tool(kind="compass_brief")
async def maintain_compass_brief() -> str:
    """Maintain claims from already observed execution evidence and return the current Brief."""
    return "Error: Compass Brief is not bound to this session."
