"""Human commands: reuse the two update tools, inspect snapshots without mutation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from compass.briefing.render import render_active_brief
from compass.claims.models import ClaimSnapshot, parse_claims
from compass.evidence.models import EvidenceRecord, parse_evidence
from compass.gate.controller import STATE
from compass.state import STATE_KEY, decode_state
from compass.task_contract.extraction import requirement_entries
from compass.task_contract.models import GoalState, parse_goal_state

if TYPE_CHECKING:
    from .tool_session import ToolCompass


@dataclass(frozen=True)
class CompassStatus:
    goal: str
    contracts: tuple[tuple[str, str], ...]
    claims: tuple[tuple[str, str], ...]
    evidence: tuple[tuple[str, str], ...]
    revision: int
    active_claims: int
    queued_observations: int
    pending_evidence: int
    phase: str
    reason: str
    error: str
    brief: str
    processed_evidence: int = 0
    remaining_steps: int = 0
    goal_state: GoalState = field(default_factory=GoalState)
    claim_snapshot: ClaimSnapshot = field(default_factory=ClaimSnapshot)
    evidence_records: tuple[EvidenceRecord, ...] = ()


def status_snapshot(runtime: ToolCompass) -> CompassStatus:
    """No ready/flush/restore/save: viewing cannot advance state or write artifacts."""
    gate = runtime.gate
    saved = gate.state().get(STATE) or {}
    if gate.session is not None:
        session = gate.session
        goal, evidence, claims = session.goals.snapshot(), session.evidence.snapshot(), session.claims.snapshot()
        status = session.maintenance_status()
        phase, reason, error = status.phase, status.reason, status.error
        decision = gate.maintenance_decision(session)
        if decision.trigger and not error:
            phase, reason = "ready", decision.reason
        remaining = decision.remaining
        brief = session.brief_for_request()
    else:
        checkpoint = saved.get("core") or (gate.state().get("compass_interactive") or {}).get("core") or {}
        raw_goal, raw_evidence, raw_claims = decode_state(checkpoint.get(STATE_KEY))
        goal, evidence, claims = parse_goal_state(raw_goal), parse_evidence(raw_evidence), parse_claims(raw_claims)
        phase, reason, error = "not_loaded", "", claims.state.last_maintenance_error
        brief = (
            render_active_brief(claims, goal, evidence) if goal.description or goal.conclusions else claims.latest_brief
        )
        remaining = 0
    return CompassStatus(
        goal_state=goal,
        claim_snapshot=claims,
        evidence_records=evidence,
        goal=goal.description,
        contracts=tuple(requirement_entries(goal)),
        claims=tuple((item.id, item.statement) for item in claims.claims),
        evidence=tuple((f"E-{item.sequence:03d}", item.summary) for item in evidence),
        revision=claims.state.revision,
        active_claims=len(claims.state.active_ids),
        queued_observations=len(saved.get("pending", [])),
        pending_evidence=max(
            0, max((item.sequence for item in evidence), default=0) - claims.state.processed_evidence_seq
        ),
        phase="busy" if gate.lock.locked() else phase,
        reason=reason,
        error=error,
        brief=brief,
        processed_evidence=claims.state.processed_evidence_seq,
        remaining_steps=remaining,
    )


async def maintain(runtime: ToolCompass) -> dict[str, Any]:
    """Always enter both tool operations. Their existing policies decide model work.

    The UI must bind this operation to an idle session; slash text is never a
    user requirement, and viewing the result never marks a Brief as delivered.
    """
    await runtime.update_requirements()
    return json.loads(await runtime.update_brief())


def brief_history(runtime: ToolCompass) -> tuple[tuple[int, str], ...]:
    """Read this session's immutable archives; an archive is not a delivery receipt."""
    directory = runtime.directory / "ledger" / "claims" / "briefs"
    entries = []
    for path in directory.glob("brief-*.txt"):
        match = re.fullmatch(r"brief-(\d+)\.txt", path.name)
        if match is None:
            continue
        with runtime.artifacts.open_verified_binary(path) as handle:
            entries.append((int(match[1]), handle.read().decode("utf-8")))
    return tuple(sorted(entries))
