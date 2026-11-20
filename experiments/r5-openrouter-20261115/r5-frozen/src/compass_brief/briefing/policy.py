"""Brief delivery and attention rules, independent of maintenance scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field

from compass_brief.claims.coverage import is_weak_coverage


def should_deliver(goal_present: bool, recipient: str) -> bool:
    """Deliver existing context, without scheduling a model call."""
    if recipient not in {"model", "delegate"}:
        raise ValueError(f"Unknown Brief recipient: {recipient}")
    return goal_present


def weak_requirements(snapshot, requirements, *, current_step=None, minimum_age=24):
    """Business reminder levels; consumers choose how to display them."""
    step = max(snapshot.state.processed_step, current_step or 0)
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    overdue: list[tuple[int, str, str, str]] = []
    for requirement_id, content in requirements:
        coverage = snapshot.state.requirement_coverage.get(requirement_id)
        if not is_weak_coverage(coverage, active):
            continue
        # A never-assessed item has no timer in old/empty ledgers. Do not invent
        # a coverage row, or hide it at submission just because its row is absent.
        since = snapshot.state.weak_requirement_since.get(requirement_id, snapshot.state.processed_step)
        age = max(0, step - since)
        if age < minimum_age:
            continue
        severity = "URGENT" if age >= 48 else "ATTENTION" if age >= 24 else "REVIEW"
        overdue.append((age, requirement_id, severity, content))
    # Longest-weak first, ties in contract order. Sorting on the id too put R9
    # before R2 and R10 (string order) and decided which five a capped review showed.
    return tuple(sorted(overdue, key=lambda item: -item[0]))


@dataclass(frozen=True, slots=True)
class DeliveryFacts:
    """What the ledger currently says, for a host deciding whether to remind now.

    Read-only, and deliberately not a Brief: a readable Brief always exists once
    a task is known, so its existence can never be the reason to deliver one.
    """

    revision: int = 0
    """Claims revision. Advances only when maintenance commits, so it identifies
    one maintenance result and keeps the same one from being announced twice."""

    maintenance_ok: bool = True
    """False after a failed or degraded maintenance; its pending work is reported
    rather than announced as a new result."""

    pending_evidence: int = 0
    """Evidence recorded past the successfully processed cursor."""

    degraded: bool = False
    """A maintenance window failed and the core now renders the Brief with a
    possibly-stale warning. Distinct from ``maintenance_ok``, which is also false
    for a run that simply has no Maintenance model and so nothing gone stale."""

    escalations: tuple[tuple[str, str], ...] = ()
    """``(R/K id, severity)`` for coverage now at ATTENTION or URGENT."""


@dataclass(frozen=True, slots=True)
class DeliveryMark:
    """What a host has already put in front of the model."""

    revision: int = 0
    escalations: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    degraded: bool = False
    """The model has seen a Brief from the current run of failed maintenances."""

    def after(self, facts: DeliveryFacts) -> DeliveryMark:
        """Record these facts as delivered."""
        return DeliveryMark(
            revision=max(self.revision, facts.revision),
            escalations=self.escalations | set(facts.escalations),
            degraded=facts.degraded,
        )


def delivery_reason(facts: DeliveryFacts, mark: DeliveryMark) -> str:
    """Name the one trigger that justifies a reminder now, or ``""`` for none.

    Ordinary progress is not a trigger: not more Evidence, and not a Brief whose
    step numbers changed. A failed maintenance commits nothing, but it leaves the
    Claims the model already holds possibly stale, and the core renders the Brief
    to say so; that is announced once per run of failures, never as a new result.
    A host calls this per turn and merges whatever it finds into a single reminder.
    """
    if facts.maintenance_ok and facts.revision > mark.revision:
        return "maintenance"
    if facts.degraded and not mark.degraded:
        return "degraded"
    if set(facts.escalations) - mark.escalations:
        return "escalation"
    return ""
