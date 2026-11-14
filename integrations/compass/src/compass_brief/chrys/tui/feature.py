# Copyright (c) 2026 Chrys. All rights reserved.

"""Chrys TUI adapter wiring the on-demand Brief sidebar.

Backend events arrive here and leave as panel projections; panel actions arrive
here and leave as backend events. Neither direction knows about the other, which
is what lets the panels render without a running engine and the engine run
without a terminal.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from chrys.app.tui.features import FeaturePanelSpec
from chrys.app.tui.widgets.chrome.commands import SlashCommandDef
from chrys.foundation.i18n import msg

from compass_brief.chrys.events import (
    BriefUpdated,
    ClaimMaintenanceRequested,
    ClaimsUpdated,
    EvidenceUpdated,
    GoalUpdated,
    MaintenanceStatusUpdated,
)
from compass_brief.chrys.tui.panels import (
    ACTION_MAINTAIN,
    BriefViewState,
    ClaimViewState,
    GoalAndClaimsPanel,
    GoalAndClaimsViewState,
    GoalViewState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.app.tui.features import FeatureActionRequested, TuiFeatureHost, TuiFeatureView

__all__ = ["ClaimLayerTuiFeature"]

_GOAL_AND_CLAIMS_KEY = "compass-brief"
_BRIEF_HELP = msg("compass.brief.command.description", fallback="Open the Compass Brief sidebar")


class ClaimLayerTuiFeature:
    """Declare the Claim Layer's tabs and translate both directions."""

    name = "compass_brief"
    event_types = (GoalUpdated, EvidenceUpdated, ClaimsUpdated, MaintenanceStatusUpdated, BriefUpdated)

    def __init__(self):
        self._session_id = None
        self._claims = ClaimViewState()

    @property
    def panels(self) -> tuple[FeaturePanelSpec, ...]:
        """Retain projections while hidden; only /brief reveals the working view."""
        return (
            FeaturePanelSpec(
                key=_GOAL_AND_CLAIMS_KEY, title="Brief", create=GoalAndClaimsPanel, visible_by_default=False
            ),
        )

    def create_command(self, host: TuiFeatureHost) -> SlashCommandDef:
        """Opening a view is independent of maintenance and model calls."""
        return SlashCommandDef(
            name="brief",
            description=_BRIEF_HELP.bind(),
            action=lambda _argument: host.focus_feature_tab(f"tab-{_GOAL_AND_CLAIMS_KEY}"),
            allow_while_running=True,
        )

    async def handle_action(self, host: TuiFeatureHost, event: FeatureActionRequested) -> None:
        """Turn one panel action into the backend effect it asked for."""
        if event.action == ACTION_MAINTAIN:
            await host.publish_feature_event(ClaimMaintenanceRequested(session_id=host.feature_session_id))

    async def handle_event(
        self,
        event: object,
        view: TuiFeatureView,
        debug: Callable[[str, str], None],
    ) -> None:
        """Route one backend event to the panel that renders it."""
        session_id = getattr(event, "session_id", None)
        if session_id != self._session_id:
            self._session_id = session_id
            self._claims = ClaimViewState()
            # None means "keep this half" in the panel's partial-update API.
            # A session boundary must explicitly replace *both* halves.
            view.set_feature_state(
                _GOAL_AND_CLAIMS_KEY,
                GoalAndClaimsViewState(goal=GoalViewState(), claims=self._claims, brief=BriefViewState()),
            )
        if isinstance(event, BriefUpdated):
            view.set_feature_state(
                _GOAL_AND_CLAIMS_KEY,
                GoalAndClaimsViewState(
                    brief=BriefViewState(
                        text=event.text,
                        tensions=event.tensions,
                        already_read=event.already_read,
                        attention=event.attention,
                        freshness=event.freshness,
                    )
                ),
            )
        elif isinstance(event, GoalUpdated):
            view.set_feature_state(
                _GOAL_AND_CLAIMS_KEY,
                GoalAndClaimsViewState(
                    goal=GoalViewState(
                        description=event.state.description,
                        conclusions=event.state.conclusions,
                    )
                ),
            )
        elif isinstance(event, ClaimsUpdated):
            self._claims = replace(self._claims, state=event.state, items=event.items)
            view.set_feature_state(
                _GOAL_AND_CLAIMS_KEY,
                GoalAndClaimsViewState(claims=self._claims),
            )
        elif isinstance(event, EvidenceUpdated):
            self._claims = replace(self._claims, current_step=max((item.sequence for item in event.items), default=0))
            view.set_feature_state(_GOAL_AND_CLAIMS_KEY, GoalAndClaimsViewState(claims=self._claims))
        elif isinstance(event, MaintenanceStatusUpdated):
            self._claims = replace(self._claims, maintenance=event.state)
            view.set_feature_state(_GOAL_AND_CLAIMS_KEY, GoalAndClaimsViewState(claims=self._claims))
        else:
            debug("compass_brief", f"unrouted event {type(event).__name__}")
