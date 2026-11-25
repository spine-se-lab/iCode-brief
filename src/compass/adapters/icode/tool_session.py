"""Session-owned Compass tools; no main-turn wrapping, edit gate or extra pass."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from chrys.extensions import ToolCall, ToolContext, ToolError, ToolResult, TurnContext
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.invoker.origin import current_invocation_origin
from compass.gate.controller import STATE as STATE
from compass.gate.controller import CompassGate, GateError
from compass.provenance import dump

from .information import InformationInput
from .interactive_model import SelectedModel
from .storage import Artifacts

if TYPE_CHECKING:
    from chrys.service.profiles.models.schema import ModelProfile

_SESSIONS: WeakValueDictionary[tuple[Path, str], ToolCompass] = WeakValueDictionary()


def tool_session(context: ToolContext) -> ToolCompass:
    """Resolve the session tool runtime without replacing the host run loop."""
    origin = current_invocation_origin.get()
    if origin is None or origin.kind != "turn" or origin.session_id != context.session_id:
        raise ToolError("Compass tools require the current main session.")
    directory = context.session_data_dir
    runtime = _SESSIONS.get((directory, context.session_id or "")) if directory else None
    if runtime is None or runtime.closed:
        raise ToolError("Compass is not attached to this session.")
    return runtime


def command_session(directory: Path | None, session_id: str | None) -> ToolCompass | None:
    """Human-command lookup by the same extension directory and session identity."""
    runtime = _SESSIONS.get((directory, session_id or "")) if directory else None
    return runtime if runtime is not None and not runtime.closed else None


class ToolCompass:
    def __init__(
        self,
        profile: ModelProfile,
        cwd: Path,
        directory: Path | None,
        report_usage: Callable[[Mapping[str, Any]], None] | None = None,
        *,
        session_id: str | None,
    ) -> None:
        if directory is None:
            raise ValueError("Compass requires a persistent session directory")
        self.directory = directory
        self.session_id = session_id or ""
        self.cwd = Path(cwd)
        self.model = SelectedModel(profile, report_usage)
        self.artifacts = Artifacts()
        self.gate = CompassGate(
            model=self.model,
            artifacts=self.artifacts,
            directory=directory,
            cwd=self.cwd,
            session_id=self.session_id,
            phase="icode",
        )
        self.information = InformationInput(self.gate, self.artifacts, self.model.profile)
        self.closed = False
        self._previous: ToolCompass | None = None

    def bind(self) -> None:
        self.gate.bind()
        self._previous = _SESSIONS.get((self.directory, self.session_id))
        _SESSIONS[(self.directory, self.session_id)] = self

    def context_callback(
        self,
        callback: Callable[[TurnContext], Awaitable[str | None]],
        origin: Callable[[], InvocationOrigin],
    ) -> Callable[[TurnContext], Awaitable[str | None]]:
        """Bind capture to this main build only; context runs before tool invocation scope."""

        async def capture(context: TurnContext) -> str | None:
            self.information.user_input(context.prompt, origin(), context)
            return await callback(context)

        return capture

    def capture_user_input(self, text: str, origin: InvocationOrigin) -> None:
        """Keep host-supplied original text, once per native user turn (including retries)."""
        self.information.user_input(text, origin)

    async def observe(self, call: ToolCall, result: ToolResult) -> None:
        self.information.tool_result(call, result, current_invocation_origin.get())

    async def inspect(self, refs: list[str]) -> str:
        try:
            return await self.gate.inspect(refs)
        except GateError as exc:
            raise ToolError(str(exc)) from exc

    async def update_requirements(self) -> str:
        try:
            return await self.gate.update_requirements()
        except GateError as exc:
            raise ToolError(str(exc)) from exc

    async def update_brief(self) -> str:
        try:
            return dump(await self.gate.update_brief())
        except GateError as exc:
            raise ToolError(str(exc)) from exc

    async def aclose(self) -> None:
        async with self.gate.lock:
            self.closed = True
            self.gate.closed = True
            key = (self.directory, self.session_id)
            if _SESSIONS.get(key) is self:
                if self._previous is not None and not self._previous.closed:
                    _SESSIONS[key] = self._previous
                else:
                    del _SESSIONS[key]
            await self.model.aclose()
