# Copyright (c) 2026 Chrys. All rights reserved.

"""Goal-state data model shared by service, orchestration, TUI and ACP layers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal, get_args

logger = logging.getLogger(__name__)

ConclusionStatus = Literal["confirmed", "refuted", "pending"]

CONCLUSION_STATUSES: Final[frozenset[str]] = frozenset(get_args(ConclusionStatus))
CONCLUSION_TAGS: Final[frozenset[str]] = frozenset(
    {
        "objective",
        "symptom",
        "reproduction",
        "criteria",
        "constraint",
        "extraction",
        "root_cause",
        "scope",
        "fix",
        "verification",
        "progress",
    }
)

MAX_CONCLUSIONS: Final[int] = 64
MAX_GOAL_TEXT_LENGTH: Final[int] = 1000
MAX_CONCLUSION_TEXT_LENGTH: Final[int] = 500


@dataclass(frozen=True, slots=True)
class GoalConclusion:
    """One conclusion point in the goal."""

    content: str
    """What was concluded, e.g. ``"UserService.login() calls AuthHelper which returns null"``."""

    status: ConclusionStatus = "pending"

    tag: str = ""
    """Semantic tag such as ``"objective"``, ``"criteria"``, ``"constraint"``,
    ``"progress"``, or empty for untagged."""

    evidence_refs: tuple[int, ...] = ()
    """Sequence numbers of tool-derived evidence relevant to this conclusion."""

    reference_derived: bool = False
    """Extraction identified an advisory clarification; not a verification result."""


@dataclass(frozen=True, slots=True)
class GoalState:
    """The current session goal — one active goal with accumulated conclusions."""

    description: str = ""
    """The high-level objective, e.g. ``"Fix the NPE in UserService.login()"``."""

    conclusions: tuple[GoalConclusion, ...] = ()


def parse_goal_state(raw: object) -> GoalState:
    """Tolerantly decode a persisted ``chrys_goal`` payload into :class:`GoalState`.

    Non-dict input yields the empty state; malformed fields degrade gracefully.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("Ignoring non-dict goal payload of type %s", type(raw).__name__)
        return GoalState()

    description = raw.get("description", "")
    if not isinstance(description, str):
        description = ""

    raw_conclusions = raw.get("conclusions")
    conclusions: list[GoalConclusion] = []
    if isinstance(raw_conclusions, list):
        for entry in raw_conclusions:
            if not isinstance(entry, dict):
                logger.warning("Skipping non-dict conclusion entry of type %s", type(entry).__name__)
                continue
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip():
                logger.warning("Skipping conclusion entry without usable content: %r", entry)
                continue
            status = entry.get("status", "pending")
            if not isinstance(status, str) or status not in CONCLUSION_STATUSES:
                logger.warning("Skipping conclusion entry with unknown status %r", status)
                continue
            tag = entry.get("tag", "")
            if not isinstance(tag, str):
                tag = ""
            if tag and tag not in CONCLUSION_TAGS:
                logger.warning("Skipping conclusion entry with unknown tag %r", tag)
                continue
            raw_refs = entry.get("evidence_refs")
            evidence_refs: tuple[int, ...] = ()
            if isinstance(raw_refs, list):
                evidence_refs = tuple(value for value in raw_refs if isinstance(value, int) and value > 0)

            conclusions.append(
                GoalConclusion(
                    content=content,
                    status=status,
                    tag=tag,
                    evidence_refs=evidence_refs,
                    reference_derived=entry.get("reference_derived") is True,
                )
            )

    return GoalState(description=description, conclusions=tuple(conclusions))


def serialize_goal_state(state: GoalState) -> dict[str, object]:
    """Encode *state* as the plain-dict ``chrys_goal`` payload."""
    return {
        "description": state.description,
        "conclusions": [
            {
                "content": c.content,
                "status": c.status,
                "tag": c.tag,
                **({"evidence_refs": list(c.evidence_refs)} if c.evidence_refs else {}),
                **({"reference_derived": True} if c.reference_derived else {}),
            }
            for c in state.conclusions
        ],
    }
