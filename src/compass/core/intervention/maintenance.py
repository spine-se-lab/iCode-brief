# Copyright (c) 2026 Chrys. All rights reserved.

"""Deterministic event-driven scheduling for Claim maintenance."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from compass.core.evidence.models import EvidenceRecord


@dataclass(frozen=True, slots=True)
class MaintenanceTriggerConfig:
    """Profile-controlled event kinds that may start Claim maintenance."""

    on_test_result: bool = True
    on_error: bool = True
    on_change_burst: bool = True
    on_phase_end: bool = True


@dataclass(frozen=True, slots=True)
class MaintenanceTriggerDecision:
    """One replayable scheduling decision for an evidence event."""

    trigger: bool = False
    reason: str = ""
    remaining: int = 0
    waiting_reason: str = ""


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    """Host-neutral progress; pending counts use successful, not attempted work."""

    pending_evidence: int = 0
    pending_statement_chars: int = 0
    phase: str = "idle"
    reason: str = ""
    remaining: int = 0
    error: str = ""


_PRIORITY = {"bootstrap": 0, "max_steps": 1, "observation_accumulate": 2, "change_burst": 3, "error": 5, "test": 6}


class MaintenanceTriggerState:
    """Coalesce evidence events into bounded, non-empty maintenance windows."""

    def __init__(
        self,
        config: MaintenanceTriggerConfig,
        *,
        change_burst_quiet_steps: int = 3,
        observation_accumulate: int = 20,
        max_steps: int = 40,
        min_steps: int = 24,
        startup_steps: int = 4,
        urgent_steps: int = 3,
        last_maintenance_step: int = 0,
    ) -> None:
        self._config = config
        self._change_burst_quiet_steps = max(1, change_burst_quiet_steps)
        self._observation_accumulate = max(1, observation_accumulate)
        self._max_steps = max(1, max_steps)
        self._min_steps = max(1, min_steps)
        self._startup_steps = max(0, startup_steps)
        self._urgent_steps = min(self._min_steps, max(1, urgent_steps))
        self._last_maintenance_step: int | None = last_maintenance_step if last_maintenance_step > 0 else None
        self._last_change_step: int | None = None
        self._last_observed_step = last_maintenance_step
        self._change_burst_active = False
        self._observations = 0
        self._pending_reasons: set[str] = set()

    def maintenance_scheduled(self, step: int) -> None:
        """Start a cooldown for maintenance scheduled outside ``observe``.

        Phase-end and explicit frontend maintenance bypass evidence-triggered
        decisions, while rebuild/restore constructs a fresh trigger around a
        persisted Claim cursor.  All three are real maintenance boundaries and
        must reset the same coalescing state.  The cursor is monotonic so an
        older concurrent schedule cannot erase newer pending evidence.
        """
        if step <= 0:
            return
        if self._last_maintenance_step is not None and step <= self._last_maintenance_step:
            return
        if step < self._last_observed_step:
            return
        self._last_maintenance_step = step
        self._last_observed_step = max(self._last_observed_step, step)
        self._pending_reasons.clear()
        self._observations = 0
        self._change_burst_active = False
        self._last_change_step = None

    def observe(self, record: EvidenceRecord, step: int) -> MaintenanceTriggerDecision:
        """Consume one evidence record and return whether maintenance is due."""
        step = max(1, step)
        if step <= self._last_observed_step:
            return MaintenanceTriggerDecision()
        self._last_observed_step = step
        reason = self._consume(record, step)
        return self._decide(step, reason)

    def observe_pending(self, records: Iterable[EvidenceRecord]) -> MaintenanceTriggerDecision:
        """Consume every unseen tracker record, then decide at the newest step.

        Evidence sequence assignment is serialized by the tracker, but event
        publication is not: a callback for E-002 may finish before E-001's
        callback.  Each callback receives the tracker's immutable snapshot, so
        consuming its unseen suffix in sequence order makes the first callback
        authoritative and every delayed callback an idempotent no-op.
        """
        newest_step = self._consume_unseen(records)
        if newest_step is None:
            return MaintenanceTriggerDecision()
        return self._decide(newest_step, "")

    def replay_unmaintained(self, records: Iterable[EvidenceRecord]) -> None:
        """Rebuild pending coalescing state without scheduling historical work.

        A persisted maintenance-attempt step is the durable reset boundary.
        Evidence after it may contain an urgent event, an observation prefix,
        or an open change burst that was still inside ``min_steps`` when the
        session stopped.  Replaying those records preserves that state while
        deferring any decision until the next live observation.
        """
        self._consume_unseen(records)

    def _consume_unseen(self, records: Iterable[EvidenceRecord]) -> int | None:
        """Replay an unordered snapshot suffix and return its newest step."""
        newest_step: int | None = None
        for record in sorted(records, key=lambda item: item.sequence):
            step = max(1, record.sequence)
            if step <= self._last_observed_step:
                continue
            self._last_observed_step = step
            newest_step = step
            reason = self._consume(record, step)
            if reason:
                self._remember_pending(reason)
        return newest_step

    def _consume(self, record: EvidenceRecord, step: int) -> str:
        """Update coalescing state for one monotonic evidence event."""
        reason = ""
        if record.kind == "change":
            if (
                self._config.on_change_burst
                and self._change_burst_active
                and self._last_change_step is not None
                and step - self._last_change_step >= self._change_burst_quiet_steps
            ):
                reason = "change_burst"
            self._change_burst_active = True
            self._last_change_step = step
        else:
            if self._config.on_change_burst and self._change_burst_active:
                reason = "change_burst"
            self._change_burst_active = False
            self._last_change_step = None
            if record.kind == "observation":
                self._observations += 1

        if record.kind == "test" and self._config.on_test_result:
            reason = "test"
        elif record.kind == "error" and self._config.on_error:
            reason = "error"
        elif not reason and self._observations >= self._observation_accumulate:
            reason = "observation_accumulate"
        elif not reason and self._steps_since_maintenance(step) >= self._max_steps:
            reason = "max_steps"
        return reason

    def _decide(self, step: int, reason: str) -> MaintenanceTriggerDecision:
        if reason:
            self._remember_pending(reason)
        decision = self.preview()
        if decision.trigger:
            self.maintenance_scheduled(step)
        return decision

    def preview(self) -> MaintenanceTriggerDecision:
        """Evaluate named windows without consuming events or starting work.

        Event recognition above and timing here are deliberately separate.
        Eligible rules win by priority; a higher-priority *waiting* rule cannot
        block a due rule. No Claim count is involved in ending bootstrap.
        """
        step = self._last_observed_step
        gap = self._steps_since_maintenance(step)
        if gap <= 0:
            return MaintenanceTriggerDecision()
        due = {reason: self._minimum_for(reason) - gap for reason in self._pending_reasons}
        due["max_steps"] = self._max_steps - gap
        if self._last_maintenance_step is None and self._startup_steps:
            due["bootstrap"] = self._startup_steps - gap
        ready = [reason for reason, remaining in due.items() if remaining <= 0]
        if ready:
            return MaintenanceTriggerDecision(trigger=True, reason=max(ready, key=_PRIORITY.__getitem__))
        next_reason = min(due, key=lambda reason: (due[reason], -_PRIORITY[reason]))
        return MaintenanceTriggerDecision(
            reason=next_reason, remaining=due[next_reason], waiting_reason="collecting_evidence"
        )

    def _minimum_for(self, reason: str) -> int:
        """Urgent outcomes merge over a short window; ordinary work is batched."""
        windows = {"test": self._urgent_steps, "error": self._urgent_steps, "max_steps": self._max_steps}
        return windows.get(reason, self._min_steps)

    def _steps_since_maintenance(self, step: int) -> int:
        return step if self._last_maintenance_step is None else step - self._last_maintenance_step

    def _remember_pending(self, reason: str) -> None:
        self._pending_reasons.add(reason)
