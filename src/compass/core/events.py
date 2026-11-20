# Copyright (c) 2026 Chrys. All rights reserved.

"""What the Claim Layer announces, in its own vocabulary.

Plain payloads, not host event classes: the core must be able to say what it
committed on a target whose event base class it has never heard of. An adapter
wraps these for the host it serves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from compass.core.claims.models import ClaimRecord, ClaimState
from compass.core.intervention.maintenance import MaintenanceStatus


@dataclass(frozen=True, slots=True)
class ClaimsUpdated:
    """The authoritative active Claim Table changed after maintenance."""

    session_id: str | None = None
    state: ClaimState = field(default_factory=ClaimState)
    items: tuple[ClaimRecord, ...] = ()
    latest_brief: str = ""
    source: str = "main"
    brief_chars: int = 0
    active_count: int = 0
    grounding_counts: dict[str, int] = field(default_factory=dict)
    uncovered_requirements: tuple[str, ...] = ()
    budget_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimsBriefOverflow:
    """Claim Brief exceeded its configured budget and was degraded or folded."""

    session_id: str | None = None
    phase: str = ""
    brief_chars: int = 0
    active_count: int = 0
    actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaintenanceFailed:
    """One maintenance window failed; committed Claims may now be stale.

    Carried as the layer's own record rather than a host warning class, so the
    adapter decides how its Coding Agent surfaces it.
    """

    code: str = "claim_maintenance_failed"
    message: str = ""
    session_id: str | None = None
    step: int = 0
    retry_failed: bool = False
    error: str = ""
    detail: Any = None


@dataclass(frozen=True, slots=True)
class GoalUpdated:
    state: Any = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceUpdated:
    items: tuple = ()
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceStatusUpdated:
    state: MaintenanceStatus = field(default_factory=MaintenanceStatus)
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class BriefUpdated:
    """Current model Brief preview and complete current tension projection.

    Not a history of sent requests or a versioned Brief interchange schema.
    """

    text: str = ""
    tensions: tuple[str, ...] = ()
    already_read: tuple[str, ...] = ()
    attention: tuple[str, ...] = ()
    freshness: str = ""
    session_id: str | None = None
