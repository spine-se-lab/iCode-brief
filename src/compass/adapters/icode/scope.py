"""Which iCode invocation a Compass hook is running in."""

from __future__ import annotations

from chrys.orchestration.invoker.origin import current_invocation_origin


def is_non_main_invocation() -> bool:
    """Only explicit child origins bypass main policy; absent identity still fails closed."""
    origin = current_invocation_origin.get()
    return origin is not None and origin.kind in ("sub_agent", "workflow_node", "side_question")
