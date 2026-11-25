"""Main-build wiring kept in Compass; the host supplies its existing resources."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chrys.foundation.models.invocations import InvocationOrigin
    from chrys.kernel import ContextProvider
    from chrys.orchestration.invoker.resources import PreparedAgent
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.tools.packs import ExtensionParts


async def prepare(
    model: ModelProfile,
    extensions: ExtensionParts,
    prepared: PreparedAgent,
    report_usage: Callable[[Mapping[str, Any]], None] | None,
    origin: Callable[[], InvocationOrigin],
) -> ContextProvider:
    """Attach Compass to one main build; the host calls this only when the profile enables it.

    Workspace, session and storage come from the public TurnContext. Cleanup stays
    owned by PreparedAgent, including rollback when any later host-build step fails.
    Child builds do not call this function.
    """
    from .information import PublicStatements
    from .tool_session import ToolCompass

    if "compass" not in extensions.details:
        raise ValueError("Compass was selected but its extension failed to assemble")
    context = next(context for context in extensions.contexts if context.extension_id == "compass")
    where = context.turn_context("")
    compass = ToolCompass(model, where.cwd, where.session_data_dir, report_usage, session_id=where.session_id)
    await prepared.own_or_release(compass.aclose)
    extensions.contexts[extensions.contexts.index(context)] = replace(
        context,
        declaration=replace(context.declaration, func=compass.context_callback(context.declaration.func, origin)),
    )
    compass.bind()
    return PublicStatements(compass.information, origin)
