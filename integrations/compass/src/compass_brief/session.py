"""Task maintenance, bounded Briefs and session recovery, independent of the host."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path

from compass_brief.briefing.evidence import render_previously_read
from compass_brief.briefing.freshness import evidence_status
from compass_brief.briefing.notifications import BriefNotifications
from compass_brief.briefing.policy import DeliveryFacts, should_deliver, weak_requirements
from compass_brief.briefing.render import (
    CANONICAL_DETAIL_ENTRY,
    attention_lines,
    render_active_brief,
    tension_lines,
)
from compass_brief.claims.inspection import ClaimInspector
from compass_brief.claims.maintenance import ClaimMaintenance
from compass_brief.claims.models import evidence_id
from compass_brief.claims.store import ClaimDelta, ClaimStore
from compass_brief.conversation.statements import StatementBuffer
from compass_brief.events import BriefUpdated, ClaimsUpdated, EvidenceUpdated, GoalUpdated, MaintenanceStatusUpdated
from compass_brief.evidence.observation import ToolObservation, record_observation
from compass_brief.evidence.tracker import EvidenceTracker
from compass_brief.intervention.maintenance import MaintenanceStatus, MaintenanceTriggerConfig, MaintenanceTriggerState
from compass_brief.model_calls.policy import CallSettings, safe_failure
from compass_brief.state import STATE_KEY, decode_state
from compass_brief.task_contract.extraction import extract_issue_structure, generate_issue_layer, requirement_entries
from compass_brief.task_contract.tracker import GoalTracker

logger = logging.getLogger(__name__)
# Tests and errors coalesce over this many evidence steps. With 3, 0.2.20 iCode
# trials ran one maintenance per 4-6 tool steps and produced 125-425 claims each.
_URGENT_STEPS = 8


def _session_activity(method):
    @wraps(method)
    async def tracked(self, *args, **kwargs):
        async with self.activity():
            return await method(self, *args, **kwargs)

    return tracked


class CompassSession:
    def __init__(
        self,
        *,
        artifacts,
        events,
        model,
        session_id=None,
        directory=None,
        repo_path=lambda: "",
        brief_budget_chars=15000,
        maintenance_config=None,
        maintenance_interval=24,
        call_settings=None,
        detail_entry=CANONICAL_DETAIL_ENTRY,
        detail_renderer=None,
        task_reference_context=lambda task: "",
    ):
        self._task_reference_context = task_reference_context
        self._artifacts = artifacts
        self._tasks = set()
        self._setup_lock = asyncio.Lock()
        self._observation_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._maintenance_running = False
        self._maintenance_reason = ""
        self._maintenance_error = ""
        self._maintenance_config = maintenance_config or MaintenanceTriggerConfig()
        self._maintenance_interval = maintenance_interval
        self._call_settings = call_settings or CallSettings()
        self._manual = False
        self.statements = StatementBuffer()
        self.user_inputs = StatementBuffer()
        self.notifications = BriefNotifications()
        self._extracted_issue = ""
        self._pending_contract = ""
        self._events, self._model, self._session_id = events, model, session_id
        self._directory, self._repo_path = directory, repo_path
        self._brief_budget_chars = brief_budget_chars
        # How this host reaches the read-only detail query. The Brief points at
        # it, so a host that cannot execute the canonical tool must say so here.
        self._detail_entry = detail_entry
        self._detail_renderer = detail_renderer
        self.goals = GoalTracker()
        self.evidence = EvidenceTracker(artifacts=artifacts)
        self.claims = ClaimStore(artifacts=artifacts)
        self.evidence.set_session_dir(directory)
        self.claims.set_session_dir(directory)
        self._trigger = MaintenanceTriggerState(
            self._maintenance_config, min_steps=maintenance_interval, urgent_steps=_URGENT_STEPS
        )
        self._runner = ClaimMaintenance(
            event_bus=events,
            store=self.claims,
            evidence_tracker=self.evidence,
            goal_tracker=self.goals,
            session_id=session_id,
            brief_budget_chars=brief_budget_chars,
            call_settings=self._call_settings,
        )

    @asynccontextmanager
    async def activity(self):
        """Include a host tool invocation in transition/fork synchronization."""
        task = asyncio.current_task()
        nested = task in self._tasks
        self._tasks.add(task)
        try:
            yield
        finally:
            if not nested:
                self._tasks.discard(task)

    def bind_directory(self, directory):
        self._directory = directory
        self.evidence.set_session_dir(directory)
        self.claims.set_session_dir(directory)

    @_session_activity
    async def observe(self, observation: ToolObservation, *, schedule_maintenance: bool = True):
        """Interpret actual execution, append evidence, then apply maintenance policy.

        Interval counts recorded evidence sequences across all workers. Per-tool
        invocation steps are provenance only; model requests don't advance it.

        ``schedule_maintenance=False`` records without starting a model call, for a
        host whose turn is already ending: nothing would read the result. The
        window still advances and pending work stays pending, so if the run does
        continue, the ordinary policy triggers as usual.
        """
        async with self._observation_lock:
            items = await record_observation(self.evidence, observation)
            await self._events.publish(EvidenceUpdated(items=items, session_id=self._session_id))
            await self.evidence_recorded(items, schedule_maintenance=schedule_maintenance)
            return items[-1]

    async def evidence_recorded(self, items, *, schedule_maintenance: bool = True):
        """Apply policy to a ledger snapshot; repeated/out-of-order notifications are no-ops."""
        decision = self._trigger.observe_pending(items)
        if decision.trigger and schedule_maintenance:
            self._maintenance_reason = decision.reason
            newest = max(items, key=lambda item: item.sequence)
            await self._on_evidence(newest, newest.sequence)
        else:
            await self.publish_maintenance_status()
        self._prepare_delivery()

    def _prepare_delivery(self, *, maintained=False):
        self.notifications.update(
            self._current_claims(),
            requirement_entries(self.goals.snapshot()),
            step=max((item.sequence for item in self.evidence.snapshot()), default=0),
            text=self.brief_for_request(),
            maintained=maintained,
        )

    def delivery_intent(self):
        """Read the last triggered intent. Rendering alone never creates one.

        Hosts retain/deduplicate this identity in the active persisted branch;
        an unsent intent remains recoverable after a checkpoint or interruption.
        """
        return self.notifications.serialize()["intent"]

    async def _on_evidence(self, record, step):
        await self.maintain(phase=record.phase)

    async def maintain(self, **kwargs):
        """Propose, validate and commit Claims as one coordinated operation."""
        await self._maintain(**kwargs)
        return self._brief()

    def record_assistant_reply(self, *, message_id, text, order, tool_call_ids=(), status="complete"):
        """Capture a finalized host reply, without advancing Evidence or scheduling.

        The host excludes its own injected/maintenance messages. The anchor is
        provenance (observations available at capture), never proof of the text.
        """
        return self.statements.record(
            message_id=message_id,
            text=text,
            order=order,
            tool_call_ids=tool_call_ids,
            status=status,
            evidence_anchor=max((item.sequence for item in self.evidence.snapshot()), default=0),
        )

    def record_user_input(self, *, message_id, text, order):
        """Make a follow-up visible to maintenance without replacing the task.

        Goal/R/K reconstruction remains the explicit start(revise=True) API;
        a question is not automatically a request to clear the task contract.
        """
        return self.user_inputs.record(
            message_id=message_id,
            text=text,
            order=order,
            source="user",
            evidence_anchor=max((item.sequence for item in self.evidence.snapshot()), default=0),
        )

    @_session_activity
    async def request_maintenance(self, *, defer=False):
        self._manual = True
        await self.publish_maintenance_status()
        if not defer:
            self._manual = False
            await self.maintain(drain_conversation=True)

    async def prepare_request(self, task="", *, recipient="model", phase=""):
        """Handle explicit pending work, then independently decide Brief delivery."""
        if task and not self.goals.snapshot().description:
            await self.start(task)
        if self._manual:
            self._manual = False
            await self.maintain(drain_conversation=True)
        return self.brief_for_request(recipient=recipient, phase=phase)

    def delivery_facts(self) -> DeliveryFacts:
        """Report what the ledger says, so a host can decide whether to remind.

        Existing readers are not this signal: ``brief_for_request()`` only reads,
        ``should_deliver()`` only checks whether a task and recipient exist, and
        ``BriefUpdated`` also fires for ordinary refreshes.
        """
        snapshot = self._current_claims()
        evidence = self.evidence.snapshot()
        latest = max((item.sequence for item in evidence), default=0)
        overdue = weak_requirements(snapshot, requirement_entries(self.goals.snapshot()), current_step=latest)
        return DeliveryFacts(
            revision=snapshot.state.revision,
            maintenance_ok=snapshot.state.last_maintenance_ok and not self._maintenance_error,
            pending_evidence=max(0, latest - snapshot.state.processed_evidence_seq),
            degraded=snapshot.state.maintenance_degraded_after_step is not None,
            escalations=tuple((identifier, severity) for _age, identifier, severity, _text in overdue),
        )

    def brief_for_request(self, *, recipient="model", phase=""):
        """Read existing context without triggering a model call or changing state."""
        if should_deliver(bool(self.goals.snapshot().description), recipient):
            return self._brief(phase)
        return ""

    async def delegation_completed(self, output, *, phase=""):
        if self._maintenance_config.on_phase_end:
            # A worker report is explicitly an agent statement, never tool Evidence.
            await self.maintain(phase=phase, output=output, stage_end=True)

    @_session_activity
    async def record_agent_statement(self, identity: str, text: str, *, phase: str = "", step: int = 0) -> None:
        """Record visible assistant text with a stable host message identity."""
        if text.strip():
            self.record_assistant_reply(message_id=identity, text=text, order=step)

    @property
    def pending_statement_chars(self) -> int:
        return sum(len(item["text"]) - item["offset"] for item in self.statements.pending)

    def final_review(self) -> tuple[str, ...]:
        """Items worth one last look before an answer is final, from the ledger only.

        Unresolved conflicts and all currently weak coverage, including unassessed
        requirements and recent changes. No model is called and no Evidence is
        added: committed claims are checked against recorded pending mutations
        without consuming that window. Weak coverage means the evidence is thin,
        never that the code is wrong.
        """
        evidence = self.evidence.snapshot()
        snapshot = self._current_claims()
        active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
        lines = [
            *tension_lines(active, snapshot.conflict_notes, unresolved_only=True),
            *attention_lines(
                snapshot,
                requirement_entries(self.goals.snapshot()),
                current_step=max((item.sequence for item in evidence), default=0),
                minimum_age=0,
            ),
        ]
        return tuple(lines)

    async def exploration_context(self, budget: int = 60_000, per_item: int = 8_000) -> str:
        """Recorded tool output so far, newest first within ``budget`` characters, for grounding a task contract."""
        parts, used = [], 0
        for record in reversed(self.evidence.snapshot()):
            output = (await self.evidence.recorded_output(record))[:per_item]
            text = f"[{evidence_id(record.sequence)} {record.kind}] {record.summary or record.tool_name}\n{output}"
            if used + len(text) > budget:
                break
            parts.append(text)
            used += len(text)
        return "\n\n".join(reversed(parts))

    async def inspect(self, refs=()):
        inspector = ClaimInspector(
            self.claims, self.evidence, self.goals, self._directory, self._detail_entry, self._detail_renderer
        )
        result = await inspector.inspect_claims(list(refs))
        return result.replace("inspect_claims", self._detail_entry) if not refs else result

    @_session_activity
    async def start(
        self, issue: str, *, input_contract: bool = False, revise: bool = False, repository_context: str = ""
    ) -> None:
        if not issue.strip():
            raise ValueError("task text must not be empty")
        async with self._setup_lock:
            # Evidence before any task is exploration, not a task being replaced.
            replacing = bool(self.goals.snapshot().description) and bool(self.evidence.snapshot())
            if replacing and not revise and not (input_contract and not self.claims.snapshot().claims):
                raise ValueError("start a new session before replacing a task with recorded evidence")
            if self._extracted_issue == issue.strip():
                return
            extraction = None
            extraction_failure = ""
            model = self._model()
            if model is not None:
                try:
                    extraction = await extract_issue_structure(
                        model,
                        issue,
                        timeout_seconds=self._call_settings.timeout_seconds,
                        call_settings=self._call_settings,
                        repository_context=repository_context,
                        reference_context=self._task_reference_context(issue),
                    )
                except Exception as exc:
                    extraction_failure = safe_failure(exc)
                    logger.warning("Task extraction failed; using bounded contract fallback: %s", safe_failure(exc))
            goal = generate_issue_layer(issue, extraction=extraction, extraction_fallback=extraction is None)
            if extraction_failure:
                goal = replace(
                    goal,
                    conclusions=tuple(
                        replace(item, content=f"extraction=regex_fallback ({extraction_failure})")
                        if item.tag == "extraction"
                        else item
                        for item in goal.conclusions
                    ),
                )
            if revise:
                previous = self.claims.snapshot().state
                self.notifications = BriefNotifications()
                await self.claims.commit(
                    ClaimDelta(),
                    phase="task-update",
                    evidence_delta_ids=frozenset(),
                    processed_evidence_seq=previous.processed_evidence_seq,
                    processed_step=previous.processed_step,
                    replace_requirement_coverage=True,
                    append_brief=False,
                )
            await self.goals.replace(goal)
            self._runner.issue_text = issue  # V1.14: contract revisions must quote the task text
            self._extracted_issue = issue.strip() if extraction is not None else ""
            self._pending_contract = issue if extraction is None else ""
            await self._events.publish(GoalUpdated(state=goal, session_id=self._session_id))
            if revise:
                # Revised R/K identities must not retain the previous task's
                # coverage in consumers that keep their own Claims projection.
                snapshot = self.claims.snapshot()
                await self._events.publish(
                    ClaimsUpdated(state=snapshot.state, items=snapshot.claims, session_id=self._session_id)
                )
            await self.publish_maintenance_status()

    def _current_claims(self):
        """Project pending mutations without consuming evidence or rewriting the ledger."""
        processed = self.claims.snapshot().state.processed_evidence_seq
        return self.claims.snapshot_for_review(
            frozenset(evidence_id(item.sequence) for item in self.evidence.snapshot() if item.sequence > processed),
            repo_path=self._repo_path(),
        )

    def _brief(self, phase: str = "") -> str:
        return render_active_brief(
            self._current_claims(),
            self.goals.snapshot(),
            self.evidence.snapshot(),
            phase=phase,
            repo_path=self._repo_path(),
            brief_budget_chars=self._brief_budget_chars,
            detail_entry=self._detail_entry,
            detail_renderer=self._detail_renderer,
        )

    @_session_activity
    async def _maintain(
        self, *, phase: str = "", output: str = "", stage_end: bool = False, coverage=None, drain_conversation=True
    ) -> None:
        async with self._maintenance_lock:
            if output.strip():
                identity = f"phase:{len(self.statements.seen)}"
                await self.record_agent_statement(identity, output, phase=phase)
            model = self._model()
            if model is None:
                self._maintenance_error = "model_unavailable"
                await self.publish_maintenance_status()
                return
            evidence = self.evidence.snapshot()
            step = evidence[-1].sequence if evidence else 0
            statement_batch = self.statements.batch()
            user_batch = self.user_inputs.batch()
            before_revision = self.claims.snapshot().state.revision
            self._trigger.maintenance_scheduled(step)
            self._maintenance_running = True
            self._maintenance_error = ""
            try:
                await self.publish_maintenance_status()
                await self.claims.mark_maintenance_attempt(step)
                deadline = time.monotonic() + self._call_settings.total_timeout_seconds
                if self._pending_contract:
                    await self._recover_contract(model, deadline)
                await self._runner.maintain(
                    model,
                    phase=phase,
                    phase_output=output,
                    stage_end=stage_end,
                    repo_path=self._repo_path(),
                    mechanical_requirement_coverage=coverage or {},
                    agent_statements=statement_batch or (((step, output),) if output else ()),
                    user_messages=user_batch,
                    # Ordinary maintenance also drains long reasoning under the same
                    # total deadline and batch cap; it need not wait for turn end.
                    conversation_batches=self._conversation_batches if drain_conversation or stage_end else None,
                    on_conversation_committed=self._acknowledge_conversation,
                    model_name=getattr(model, "model_id", ""),
                    deadline=deadline,
                )
                current = self.claims.snapshot().state
                if current.last_maintenance_ok and current.revision > before_revision:
                    self._prepare_delivery(maintained=True)
            except asyncio.CancelledError:
                self._maintenance_error = "cancelled"
                raise
            except Exception as exc:
                self._maintenance_error = safe_failure(exc)
                raise
            finally:
                self._maintenance_running = False
                self._maintenance_reason = ""
                await self.publish_maintenance_status()

    async def _recover_contract(self, model, deadline):
        # One retry per existing maintenance window, sharing its total deadline.
        # Keep public R/K identities stable if Claims already refer to fallback IDs.
        async with self._setup_lock:
            if not self._pending_contract:
                return
            try:
                extraction = await extract_issue_structure(
                    model, self._pending_contract,
                    timeout_seconds=self._call_settings.timeout_seconds,
                    call_settings=replace(self._call_settings, attempts=1),
                    repository_context=await self.exploration_context(), deadline=deadline,
                    reference_context=self._task_reference_context(self._pending_contract),
                )
            except Exception as exc:
                logger.warning("Task contract recovery deferred: %s", safe_failure(exc))
                return
            goal = generate_issue_layer(self._pending_contract, extraction=extraction)
            previous = self.goals.snapshot()
            if self.claims.snapshot().claims:
                clarified = {(item.tag, " ".join(item.content.split()).casefold())
                             for item in goal.conclusions if item.reference_derived}
                kept = tuple(replace(item, reference_derived=True)
                             if (item.tag, " ".join(item.content.split()).casefold()) in clarified else item
                             for item in previous.conclusions if item.tag != "extraction")
                known = {(item.tag, " ".join(item.content.split()).casefold()) for item in kept}
                added = tuple(item for item in goal.conclusions if item.tag in {"criteria", "constraint"}
                              and (item.tag, " ".join(item.content.split()).casefold()) not in known)
                goal = replace(goal, conclusions=kept + added)
            await self.goals.replace(goal)
            self._extracted_issue, self._pending_contract = self._pending_contract.strip(), ""
            await self._events.publish(GoalUpdated(state=goal, session_id=self._session_id))

    def _conversation_batches(self):
        return self.statements.batch(), self.user_inputs.batch()

    def _acknowledge_conversation(self, agent_statements, user_messages):
        # The runner knows which committed batch actually included these
        # excerpts. A cursor comparison alone is unsafe with concurrent tools.
        self.statements.acknowledge(tuple(item for item in agent_statements if isinstance(item, dict)))
        self.user_inputs.acknowledge(user_messages)

    def maintenance_status(self) -> MaintenanceStatus:
        """Project authoritative progress once, so frontends need no scheduling rules."""
        state = self.claims.snapshot().state
        latest = max((item.sequence for item in self.evidence.snapshot()), default=0)
        pending = max(0, latest - state.processed_evidence_seq)
        decision = self._trigger.preview()
        error = self._maintenance_error or state.last_maintenance_error
        phases = (
            (self._maintenance_running, "running"),
            (self._manual, "queued"),
            (bool(error) or not state.last_maintenance_ok, "failed"),
            (pending > 0 or self.pending_statement_chars > 0, "waiting"),
        )
        phase = next((name for applies, name in phases if applies), "idle")
        return MaintenanceStatus(
            pending_evidence=pending,
            pending_statement_chars=self.pending_statement_chars,
            phase=phase,
            reason=self._maintenance_reason if self._maintenance_running else decision.reason,
            remaining=decision.remaining,
            error=error,
        )

    async def publish_maintenance_status(self) -> None:
        await self._events.publish(
            MaintenanceStatusUpdated(state=self.maintenance_status(), session_id=self._session_id)
        )
        await self.publish_brief()

    async def publish_brief(self) -> None:
        """Publish current working information from the same model-facing renderer."""
        snapshot = self._current_claims()
        active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
        evidence = self.evidence.snapshot()
        latest = evidence[-1].sequence if evidence else 0
        await self._events.publish(
            BriefUpdated(
                text=self.brief_for_request(),
                tensions=tuple(tension_lines(active, snapshot.conflict_notes)),
                # UI counts the complete known coverage; only model Briefs use
                # the four-file budget. Do not present a truncated count as total.
                already_read=tuple(render_previously_read(evidence, root=self._repo_path(), max_files=len(evidence))),
                attention=tuple(
                    attention_lines(snapshot, requirement_entries(self.goals.snapshot()), current_step=latest)
                ),
                freshness=evidence_status(snapshot, evidence) if evidence else "",
                session_id=self._session_id,
            )
        )

    async def before_fork(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def before_session_transition(self) -> None:
        await self.before_fork()

    def contribute_session_state(self) -> dict:
        if (
            not self.goals.snapshot().description
            and not self.evidence.snapshot()
            and not self.claims.snapshot().claims
            and not self.statements.seen
            and not self.user_inputs.seen
        ):
            return {STATE_KEY: None}
        return {
            STATE_KEY: {
                "schema_version": 1,
                "artifact_directory": str(self._directory) if self._directory is not None else None,
                "goal": self.goals.serialize(),
                "pending_contract": self._pending_contract,
                "evidence": self.evidence.serialize(),
                "claims": self.claims.serialize(),
                "assistant_statements": self.statements.serialize(),
                "user_inputs": self.user_inputs.serialize(),
                "brief_notifications": self.notifications.serialize(),
            }
        }

    async def restore_session_state(self, state: Mapping) -> None:
        values = decode_state(state.get(STATE_KEY))
        pending_contract = (state.get(STATE_KEY) or {}).get("pending_contract", "")
        if not isinstance(pending_contract, str):
            raise ValueError("invalid pending task contract")
        statements = StatementBuffer((state.get(STATE_KEY) or {}).get("assistant_statements"))
        user_inputs = StatementBuffer((state.get(STATE_KEY) or {}).get("user_inputs"))
        notifications = BriefNotifications((state.get(STATE_KEY) or {}).get("brief_notifications"))
        await self.before_fork()
        await self._restore_artifact_references(state.get(STATE_KEY), values[1])
        await self.goals.restore(values[0])
        await self.evidence.restore(values[1])
        await self.claims.restore(values[2])
        self.statements = statements
        self.user_inputs = user_inputs
        self.notifications = notifications
        self._pending_contract = pending_contract
        self._manual = False
        self._maintenance_running = False
        self._maintenance_reason = ""
        self._maintenance_error = ""
        self._extracted_issue = ""
        cursor = self.claims.snapshot().state.last_maintenance_attempt_step
        self._trigger = MaintenanceTriggerState(
            self._maintenance_config,
            min_steps=self._maintenance_interval,
            urgent_steps=_URGENT_STEPS,
            last_maintenance_step=cursor,
        )
        self._trigger.replay_unmaintained(self.evidence.snapshot())
        await self._events.publish(GoalUpdated(state=self.goals.snapshot(), session_id=self._session_id))
        await self._events.publish(EvidenceUpdated(items=self.evidence.snapshot(), session_id=self._session_id))
        snapshot = self.claims.snapshot()
        await self._events.publish(
            ClaimsUpdated(
                state=snapshot.state,
                items=snapshot.claims,
                latest_brief=snapshot.latest_brief,
                session_id=self._session_id,
            )
        )
        await self.publish_maintenance_status()

    async def _restore_artifact_references(self, envelope, evidence):
        """A fork owns a copy of referenced raw outputs, not writable parent files.

        Old snapshots without a location retain the existing host-copy behavior.
        This copies only bounded, canonical evidence files, not arbitrary paths,
        the host conversation, or an entire historical data directory.
        """
        source = (envelope or {}).get("artifact_directory")
        if not source or self._directory is None or Path(source) == self._directory:
            return
        source = Path(source)
        if not source.is_absolute():
            raise ValueError("Invalid Compass artifact directory")

        def copy():
            for item in evidence or ():
                relative = item.get("raw_path")
                if not relative:
                    continue
                if relative != f"evidence/raw/E-{item['sequence']:03d}.txt":
                    raise ValueError("Invalid Compass raw evidence reference")
                with self._artifacts.open_verified_binary(source / relative) as stream:
                    content = stream.read(65536 + 256)
                destination = self._directory / relative
                if destination.exists():
                    with self._artifacts.open_verified_binary(destination) as stream:
                        if stream.read() != content:
                            raise ValueError("Conflicting evidence at restored data location")
                else:
                    self._artifacts.write_text(destination, content.decode("utf-8"))

        await asyncio.to_thread(copy)
