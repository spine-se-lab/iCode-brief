"""Host-independent tool gate: task initialization, observations and Brief decisions."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from compass.evidence.observation import ToolObservation, record_observation
from compass.gate.maintenance import MaintenanceTriggerDecision
from compass.persistence.session_store import SessionStore
from compass.ports import MaintenanceModel, SessionArtifactStore
from compass.provenance import SOURCE_ROOT, verify_source
from compass.session import CompassSession
from compass.task_contract.extraction import requirement_entries

STATE = "compass_tools"
_CONTRACT_POLICY_VERSION = 5
_COMPASS_INVOCATION_RE = re.compile(
    r"(?:please\s+)?(?:call|use|invoke)\s+"
    r"(?:inspect_compass_brief|update_compass_requirements|update_compass_brief)\s*(?:\(\))?"
    r"(?:\s+(?:now|once))?(?:\s+(?:to\s+)?(?:update(?:\s+and\s+return)?|return|show)"
    r"(?:\s+the)?(?:\s+current)?\s+brief)?|"
    r"(?:请|请先|再|然后请)?(?:调用|使用|执行)\s*"
    r"(?:inspect_compass_brief|update_compass_requirements|update_compass_brief)\s*(?:\(\))?"
    r"\s*(?:(?:更新(?:并返回)?|返回|查看|显示)(?:当前)?\s*Brief)?",
    re.IGNORECASE,
)


def task_messages(inputs: list[dict[str, Any]]) -> tuple[str, ...]:
    """Separate only complete, unqualified Compass invocation clauses.

    Original messages remain in user_inputs. Unknown wording, qualifications,
    tasks about a tool, and tool-only messages are preserved verbatim.
    """
    texts = []
    for item in inputs:
        parts = re.split(r"(?<=[，,；;。！？!?\n])|(?<=\.)(?=\s|$)", item["text"])  # noqa: RUF001
        kept = [
            part
            for part in parts
            if not _COMPASS_INVOCATION_RE.fullmatch(part.strip().rstrip("，,；;。.!?！？"))  # noqa: RUF001
        ]
        text = "".join(kept).strip().rstrip("，,；;").strip()  # noqa: RUF001
        texts.append(text or item["text"])
    return tuple(texts)


class GateError(ValueError):
    """A Compass operation cannot proceed with the available session input."""


class CompassGate:
    def __init__(
        self,
        *,
        model: MaintenanceModel,
        artifacts: SessionArtifactStore,
        directory: Path,
        cwd: Path,
        session_id: str,
        phase: str = "compass",
    ) -> None:
        verify_source(SOURCE_ROOT)
        self.model = model
        self.artifacts = artifacts
        self.directory = directory
        self.cwd = cwd
        self.session_id = session_id
        self.phase = phase
        self.store = SessionStore(directory, artifacts)
        self.lock = asyncio.Lock()
        self.closed = False
        self.session: CompassSession | None = None
        self._checkpoint: object = None
        self._state: dict[str, Any] | None = None

    def bind(self) -> None:
        saved = self.store.load()
        self._state = {STATE: saved} if saved is not None else {}

    def capture_user_input(self, message_id: str, text: str) -> None:
        if not text.strip():
            return
        saved = self.state().setdefault(STATE, {"version": 1, "core": None, "pending": []})
        inputs = saved.setdefault("user_inputs", [])
        if any(item["id"] == message_id for item in inputs):
            return
        inputs.append({"id": message_id, "text": text})
        self.store.save(saved)

    def observe(self, observation: ToolObservation) -> None:
        # Queue only: ordinary host tools never wait for a maintenance model call.
        saved = self.state().setdefault(STATE, {"version": 1, "core": None, "pending": []})
        saved.setdefault("pending", []).append(asdict(observation))
        self.store.save(saved)

    def state(self) -> dict[str, Any]:
        if self._state is None or self.closed:
            raise GateError("Compass session is closed or not attached.")
        return self._state

    async def ready(self) -> CompassSession:
        saved = self.state().get(STATE)
        if self.session is not None and saved is self._checkpoint:
            return self.session
        session = CompassSession(
            artifacts=self.artifacts,
            events=self,
            model=lambda: self.model,
            session_id=self.session_id,
            directory=self.directory / "ledger",
            repo_path=lambda: str(self.cwd),
            detail_entry="inspect_compass_brief",
        )
        if saved is not None and saved.get("version") != 1:
            raise GateError("Unsupported Compass tool state version.")
        if saved is not None and saved.get("core") is not None:
            await session.restore_session_state(saved["core"])
        # Older checkpoints can already have revised requirements with cleared
        # coverage. Recover that pending assessment once, without rewriting evidence.
        if saved is not None and "requirements_pending" not in saved:
            claims = session.claims.snapshot()
            saved["requirements_pending"] = bool(claims.claims and not claims.state.requirement_coverage)
        self.session = session
        self._checkpoint = saved
        return session

    def save(self) -> None:
        if self.session is None:
            return
        pending = (self.state().get(STATE) or {}).get("pending", [])
        saved = {
            **(self.state().get(STATE) or {}),
            "version": 1,
            "core": self.session.contribute_session_state(),
            "pending": pending,
        }
        self.state()[STATE] = saved
        self._checkpoint = saved
        self.store.save(saved)

    async def publish(self, event: object) -> None:
        # Core still emits events; this step has no sidebar subscriber yet.
        pass

    async def inspect(self, refs: list[str]) -> str:
        async with self.lock:
            session = await self.ready()
            await self.flush_observations(session)
            return await session.inspect(refs)

    @staticmethod
    def requirements_missing(session: CompassSession) -> bool:
        goal = session.goals.snapshot()
        return not goal.description.strip() or not any(ref.startswith("R") for ref, _ in requirement_entries(goal))

    def requirements_outdated(self, session: CompassSession) -> bool:
        inputs = (self.state().get(STATE) or {}).get("user_inputs", [])
        issue = "\n\n--- User follow-up ---\n\n".join(task_messages(inputs))
        return bool(inputs) and (
            session._extracted_input is None
            or session._extracted_input[0] != issue.strip()
            or (self.state().get(STATE) or {}).get("contract_policy_version") != _CONTRACT_POLICY_VERSION
        )

    async def _extract_requirements(self, session: CompassSession) -> None:
        """Use the same captured task for explicit updates and missing-contract initialization."""
        inputs = (self.state().get(STATE) or {}).get("user_inputs", [])
        if not inputs:
            raise GateError("No captured user input. Start a main-session turn before updating requirements.")
        issue = "\n\n--- User follow-up ---\n\n".join(task_messages(inputs))
        repository_context = await session.exploration_context()
        # The gate decides whether extraction is needed; the session retains its
        # defensive deduplication for independent evaluation callers.
        current_policy = (self.state().get(STATE) or {}).get("contract_policy_version") == _CONTRACT_POLICY_VERSION
        if (
            current_policy
            and not self.requirements_missing(session)
            and session._extracted_input == (issue.strip(), repository_context)
        ):
            return
        revising = bool(session.goals.snapshot().description)
        try:
            await session.start(
                issue,
                revise=revising,
                repository_context=repository_context,
                force_reextract=not current_policy,
                user_messages=task_messages(inputs),
                require_extraction=revising,
            )
        except ValueError as exc:
            raise GateError(str(exc)) from exc
        if session._extracted_input is not None:
            self.state()[STATE]["contract_policy_version"] = _CONTRACT_POLICY_VERSION
        if revising:
            self.state()[STATE]["requirements_pending"] = True

    async def update_requirements(self) -> str:
        async with self.lock:
            session = await self.ready()
            await self.flush_observations(session)
            try:
                await self._extract_requirements(session)
                return await session.inspect(["goal"])
            finally:
                self.save()

    async def flush_observations(self, session: CompassSession) -> None:
        for order, item in enumerate((self.state().get(STATE) or {}).get("user_inputs", [])):
            session.record_user_input(message_id=item["id"], text=item["text"], order=order)
        pending = (self.state().get(STATE) or {}).get("pending", [])
        while pending:
            await record_observation(session.evidence, ToolObservation(**pending[0]))
            del pending[0]
            self.save()
        # Reuse the session's replay/preview pair without advancing a due window.
        # observe(..., schedule_maintenance=False) would consume such a window.
        session._trigger.replay_unmaintained(session.evidence.snapshot())
        await session.publish_maintenance_status()
        self.save()

    def maintenance_decision(self, session: CompassSession) -> MaintenanceTriggerDecision:
        """Explicit Brief checks reconsider contracts and completed tests promptly.

        Passive observations still only queue work. Ordinary evidence retains
        the original batching windows; none of these rules asserts task success.
        """
        evidence = session.evidence.snapshot()
        if evidence and (self.state().get(STATE) or {}).get("requirements_pending"):
            return MaintenanceTriggerDecision(trigger=True, reason="requirements_changed")
        claim_state = session.claims.snapshot().state
        if (
            evidence
            and session.claims.snapshot().claims
            and set(dict(requirement_entries(session.goals.snapshot()))) - claim_state.requirement_coverage.keys()
        ):
            return MaintenanceTriggerDecision(trigger=True, reason="requirements_unassessed")
        processed = claim_state.processed_evidence_seq
        if any(
            item.sequence > processed and (item.kind == "test" or item.action_class in {"test", "new_test"})
            for item in evidence
        ):
            return MaintenanceTriggerDecision(trigger=True, reason="test_result")
        return session._trigger.preview()

    async def automatic_brief(self) -> dict[str, Any] | None:
        """Check observed work without depending on the agent choosing a tool.

        The same lock serializes explicit and automatic checks. Failed windows
        retain evidence and use the existing cooldown before automatic retries.
        """
        async with self.lock:
            session = await self.ready()
            await self.flush_observations(session)
            state = session.claims.snapshot().state
            changed = not self.requirements_missing(session) and self.requirements_outdated(session)
            if not changed:
                if not self.maintenance_decision(session).trigger:
                    return None
                latest = max((item.sequence for item in session.evidence.snapshot()), default=0)
                if latest <= state.last_maintenance_attempt_step:
                    return None
                if not state.last_maintenance_ok and not session._trigger.preview().trigger:
                    return None
            return await self._update_brief(session)

    async def update_brief(self) -> dict[str, Any]:
        async with self.lock:
            session = await self.ready()
            return await self._update_brief(session)

    async def _update_brief(self, session: CompassSession) -> dict[str, Any]:
        """Update with the caller holding the gate lock."""
        try:
            await self.flush_observations(session)
            initialized = self.requirements_missing(session)
            refreshed = initialized or self.requirements_outdated(session)
            if refreshed:
                await self._extract_requirements(session)
                if self.requirements_missing(session):
                    raise GateError(
                        "Could not initialize Goal and Requirements; any reported symptoms remain as background in Goal. Brief maintenance was not run."
                    )
            decision = self.maintenance_decision(session)
            before = session.claims.snapshot().state.revision
            if decision.trigger:
                await session.maintain(
                    phase=self.phase,
                    reassess_requirements=(
                        decision.reason in {"requirements_changed", "requirements_unassessed"}
                        or bool(
                            set(dict(requirement_entries(session.goals.snapshot())))
                            - session.claims.snapshot().state.requirement_coverage.keys()
                        )
                    ),
                )
            state = session.claims.snapshot().state
            after = state.revision
            if decision.trigger and state.last_maintenance_ok and after > before:
                self.state()[STATE]["requirements_pending"] = False
            outcome = "updated" if after > before else "unchanged"
            return {
                "outcome": outcome,
                "maintenance_due": decision.trigger,
                "requirements_initialized": initialized,
                "requirements_refreshed": refreshed,
                "reason": decision.reason,
                "remaining_evidence_steps": decision.remaining,
                "revision": after,
                "processed_evidence_seq": state.processed_evidence_seq,
                "latest_evidence_seq": max((item.sequence for item in session.evidence.snapshot()), default=0),
                "maintenance_error": session.maintenance_status().error,
                "brief": session.brief_for_request(),
            }
        finally:
            self.save()

    async def aclose(self) -> None:
        async with self.lock:
            self.closed = True
