"""Instance-local host composition. Never replaces iCode classes or functions."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.kernel.middleware import ChatMiddleware, FunctionMiddleware
    from chrys.orchestration.session_host import ChrysSessionHost
    from chrys.service.profiles.agents.schema import AgentProfile
    from compass.runtime.run_state import RunState

from chrys.foundation.tool_kinds import get_tool_kind
from chrys.service.extensions import bind_extension
from chrys.service.tools.packs import ToolBuild
from compass.runtime.records import TurnResult

from .middleware import BriefChat, ExplorationGuard
from .tools import make_extension


class HeadlessBackend:
    """One fresh headless session. Call attach after start and at each native turn start.

    Uses the pinned host's LoadedAgent seam. It is deliberately not a general
    TUI/sub-agent installer or a restored-session migration mechanism.
    """

    def __init__(self, owner: RunState, host: ChrysSessionHost, profile: AgentProfile) -> None:
        self.owner = owner
        self.host = host
        self.profile = profile
        self.extension = make_extension(owner)

    def attach(self) -> None:
        loaded = self.host.engine.current.require_loaded()
        agent = loaded.agent
        # Native LoadedAgent assembly supplies an ordered middleware sequence.
        chain = list(cast("Sequence[ChatMiddleware | FunctionMiddleware]", agent.middleware or []))
        existing = [item for item in chain if isinstance(item, BriefChat)]
        if existing:
            if len(existing) != 1 or existing[0].owner is not self.owner:
                raise RuntimeError("Runtime already belongs to another Compass run")
            return
        bound = bind_extension(
            "compass",
            self.extension,
            ToolBuild(
                tools=self.profile.tools,
                agent_name=self.profile.name,
                runtime=loaded.runtime,
                settings=self.host.engine.settings,
                session_id=self.host.session_id,
                session_dir=self.owner.directory,
            ),
        )
        tools = agent.default_options["tools"]
        names = {tool.name for tool in tools}
        if names.intersection(tool.name for tool in bound.tools):
            raise RuntimeError("Compass tool name collision")
        tools.extend(bound.tools)
        self.owner.tool_kinds.update({tool.name: get_tool_kind(tool) for tool in tools})
        agent.middleware = [ExplorationGuard(self.owner), BriefChat(self.owner, loaded.injection), *chain]

    async def start(self) -> None:
        await self.host.start()
        self.attach()

    async def run(self, message: str) -> TurnResult:
        self.attach()
        result = await self.host.run_until_final(message)
        return TurnResult(text=result.text, session_id=result.session_id)
