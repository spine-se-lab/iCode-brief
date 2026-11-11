# Copyright (c) 2026 Chrys. All rights reserved.

"""Chrys bus events carrying the Claim Layer's own payloads.

The core says what happened in its own vocabulary
(:mod:`compass_brief.events`); the host bus only accepts ``Event``
subclasses. These are that translation, and nothing else: each one wraps one
core payload without reinterpreting it.

The host never imports these. The Engine adapter publishes them and the TUI
adapter declares the ones it renders through ``TuiFeature.event_types``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chrys.foundation.events.types import Event

from compass_brief.claims.models import ClaimRecord, ClaimState
from compass_brief.evidence.models import EvidenceRecord
from compass_brief.intervention.maintenance import MaintenanceStatus
from compass_brief.task_contract.models import GoalState

__all__ = [
    "BriefUpdated",
    "ClaimMaintenanceRequested",
    "ClaimsBriefOverflow",
    "ClaimsUpdated",
    "EvidenceUpdated",
    "GoalUpdated",
    "MaintenanceFailed",
    "MaintenanceStatusUpdated",
]


@dataclass
class GoalUpdated(Event):
    """The session Goal was replaced (full state, never a diff)."""

    state: GoalState = field(default_factory=GoalState)
    source: str = "main"


@dataclass
class EvidenceUpdated(Event):
    """The session evidence ledger was appended to (full list)."""

    items: tuple[EvidenceRecord, ...] = ()
    source: str = "main"


@dataclass
class ClaimsUpdated(Event):
    """The authoritative active Claim Table changed after maintenance."""

    state: ClaimState = field(default_factory=ClaimState)
    items: tuple[ClaimRecord, ...] = ()
    latest_brief: str = ""
    source: str = "main"
    brief_chars: int = 0
    active_count: int = 0
    grounding_counts: dict[str, int] = field(default_factory=dict)
    uncovered_requirements: tuple[str, ...] = ()
    budget_actions: tuple[str, ...] = ()


@dataclass
class ClaimsBriefOverflow(Event):
    """The Claim Brief exceeded its budget and was degraded or folded."""

    phase: str = ""
    brief_chars: int = 0
    active_count: int = 0
    actions: tuple[str, ...] = ()


@dataclass
class MaintenanceFailed(Event):
    """One maintenance window failed; committed Claims may now be stale."""

    code: str = "claim_maintenance_failed"
    message: str = ""
    step: int = 0
    retry_failed: bool = False
    error: str = ""


@dataclass
class ClaimMaintenanceRequested(Event):
    """A user asked for a maintenance window now, ahead of trigger policy.

    Frontend to backend. Trigger policy exists to keep windows bounded, not to
    stop a user who wants the Claim Table brought up to date before reading it.
    """


@dataclass
class MaintenanceStatusUpdated(Event):
    """Read-only scheduling progress from the host-independent session."""

    state: MaintenanceStatus = field(default_factory=MaintenanceStatus)


@dataclass
class BriefUpdated(Event):
    text: str = ""
    tensions: tuple[str, ...] = ()
    already_read: tuple[str, ...] = ()
    attention: tuple[str, ...] = ()
    freshness: str = ""
