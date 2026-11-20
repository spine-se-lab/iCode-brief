"""Per-run Compass state and observation mapping. Frozen Compass owns the policy."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compass.core.briefing.policy import DeliveryMark
from compass.core.session import CompassSession
from compass.ports import MaintenanceModel, SessionArtifactStore
from compass.provenance import dump, sha
from compass.runtime.budget import MAX_CALLS, MAX_SECONDS
from compass.runtime.records import BudgetExhausted


@dataclass
class WorkflowFlags:
    """Independent observations, not mutually exclusive workflow phases.

    Field names also define the persisted `flags` object. Add a default here
    when introducing a flag so older snapshots retain their previous behavior.
    """

    started: bool = False  # Core extraction and initial maintenance succeeded.
    handoff_seen: bool = False  # Current handoff/update Brief was confirmed.
    rules_seen: bool = False  # Submission rules have been delivered at least once.
    closing_seen: bool = False  # The late-budget reminder has been delivered.
    check_done: bool = False  # Submission check was handled, including low-budget skip.
    handoff_requested: bool = False  # Even a rejected start is not a read-only skip.
    write_requested: bool = False  # Even a blocked edit is not a read-only skip.


class RunState:
    def __init__(
        self,
        task: str,
        cwd: Path,
        directory: Path,
        constants: dict[str, str],
        model: MaintenanceModel | None = None,
        *,
        artifacts: SessionArtifactStore,
    ) -> None:
        self.task = task
        self.cwd = cwd
        self.directory = directory
        self.constants = constants
        self.calls = 0
        self.started_at = time.monotonic()
        self.flags = WorkflowFlags()
        self.mark = DeliveryMark()
        self.seen: set[str] = set()
        self.conflicts: set[str] = set()
        self.tool_kinds: dict[str, str | None] = {}
        self.batch_size = 0
        self.model = model
        self.artifacts = artifacts
        self.core = CompassSession(
            artifacts=artifacts,
            events=self,
            model=lambda: self.model,
            directory=directory / "compass",
            repo_path=lambda: str(cwd),
            detail_entry="inspect_compass_brief",
        )

    def audit(self, event: str, **fields: Any) -> None:
        path = self.directory / "adapter-events.jsonl"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600), "a") as f:
            f.write(dump({"event": event, "time": time.time(), "step": self.calls, **fields}) + "\n")

    async def publish(self, event: object) -> None:
        self.audit("core_event", type=type(event).__name__)

    def remaining(self) -> float:
        return MAX_SECONDS - (time.monotonic() - self.started_at)

    def check_budget(self) -> None:
        if self.calls >= MAX_CALLS or self.remaining() <= 0:
            self.audit(
                "budget_exhausted", calls=self.calls, cause="main_calls" if self.calls >= MAX_CALLS else "wall_time"
            )
            raise BudgetExhausted("Compass trial budget exhausted")

    def save(self) -> None:
        self.artifacts.write_text(self.directory / "compass-state.json", dump(self.core.contribute_session_state()))

    def record_write_attempt(self) -> None:
        self.flags.write_requested = True
        self.save()

    async def handoff(self, summary: str) -> str:
        self.flags.handoff_requested = True
        self.save()
        if self.batch_size != 1:
            return "Not started. Call start_compass_brief alone, then wait for the Brief."
        if not self.core.evidence.snapshot():
            return "Not started. First inspect the relevant repository source and tests."
        if not self.flags.started:
            await self.core.record_agent_statement(
                "exploration:" + sha(summary),
                "Exploration handoff (agent interpretation):\n" + summary,
                phase="exploration",
                step=self.calls,
            )
            await self.core.start(self.task, repository_context=await self.core.exploration_context())
            if any(c.tag == "extraction" for c in self.core.goals.snapshot().conclusions):
                raise RuntimeError("Compass contract extraction failed; coding not started")
            await self.core.maintain(phase="exploration-complete", drain_conversation=True)
            self.flags.started = True
            self.save()
        return "Compass started. Read the Brief on the next turn before editing."
