"""Internal bridge from normally discovered tools to one active main invocation."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.extensions import ToolContext, ToolError
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.invoker.origin import current_invocation_origin

if TYPE_CHECKING:
    from .interactive import InteractiveCompass


@dataclass
class InvocationScope:
    runtime: InteractiveCompass
    origin: InvocationOrigin
    active: bool = True


_ACTIVE: ContextVar[InvocationScope | None] = ContextVar("compass_invocation", default=None)


@contextmanager
def invocation_scope(runtime: InteractiveCompass, origin: InvocationOrigin) -> Generator[None]:
    scope = InvocationScope(runtime, origin)
    token = _ACTIVE.set(scope)
    try:
        yield
    finally:
        # Child tasks inherit ContextVars; invalidate their lease as well.
        scope.active = False
        _ACTIVE.reset(token)


def is_non_main_invocation() -> bool:
    """Only explicit child origins bypass main policy; absent identity still fails closed."""
    origin = current_invocation_origin.get()
    return origin is not None and origin.kind in ("sub_agent", "workflow_node", "side_question")


def current_runtime(context: ToolContext) -> InteractiveCompass:
    scope = _ACTIVE.get()
    origin = current_invocation_origin.get()
    if (
        scope is None
        or not scope.active
        or origin is None
        or origin.kind != "turn"
        or not scope.origin.same_invocation(origin)
        or context.session_id != origin.session_id
    ):
        raise ToolError("Compass tools require the active Compass main invocation; sub-agents are not supported.")
    return scope.runtime
