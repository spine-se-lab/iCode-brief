"""Opt-in main-session Compass policy, owned by the native build/turn lifecycle."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import current_turn_start, is_continuation_message, is_injected_message, opens_turn
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.kernel import ChatResponse, Message
from chrys.kernel.middleware import ChatContext, ChatMiddleware, FunctionMiddleware
from chrys.orchestration.invoker.contracts import Failed, FailureCategory, FailureDisposition, Ok, RunIntent, StopCause
from chrys.service.extensions import bind_extension
from compass.core.briefing.policy import DeliveryMark
from compass.provenance import SOURCE_ROOT, dump, sha, source_constants
from compass.runtime.budget import MAX_CALLS
from compass.runtime.prompts import INTERACTIVE_WORKFLOW, REQUIREMENTS_UPDATE
from compass.runtime.records import InvalidCompassRun
from compass.runtime.run_state import RunState, WorkflowFlags
from compass.runtime.workflow import prepare_submission_check, require_handoff

from .interactive_model import SelectedModel
from .middleware import BriefChat, ExplorationGuard, PendingBrief, item_identity
from .storage import Artifacts
from .tools import make_extension

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.kernel import Agent, FunctionTool
    from chrys.orchestration.engine.run.bindings import TurnBindings
    from chrys.orchestration.invoker.contracts import InvocationOutcome, RunRequest
    from chrys.service.agent_middleware.injection import InjectionMiddleware
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.tools.packs import ToolBuild
    from compass.core.session import CompassSession
    from compass.ports import MaintenanceModel, SessionArtifactStore


class UserInput(TypedDict):
    message_id: str
    text: str
    order: int


STATE = "compass_interactive"


def user_input(message: Message) -> bool:
    props = message.additional_properties or {}
    injection_id = str(props.get(HistoryMarkerKind.INJECTION_ID_KEY, ""))
    return (
        (opens_turn(message) or is_injected_message(message))
        and not is_continuation_message(message)
        and not props.get(HistoryMarkerKind.KEY)
        and not props.get(HistoryMarkerKind.SYSTEM_REMINDERS_KEY)
        and not injection_id.startswith(("compass:", "compass-workflow:"))
    )


class InteractiveRunState(RunState):
    def __init__(
        self,
        task: str,
        cwd: Path,
        directory: Path,
        constants: dict[str, str],
        model: MaintenanceModel | None = None,
        *,
        artifacts: SessionArtifactStore,
        state: Callable[[], dict[str, Any]],
        invocation_id: str,
        task_id: str | None = None,
        session_id: str = "",
    ) -> None:
        super().__init__(task, cwd, directory, constants, model, artifacts=artifacts)
        self.state = state
        self.invocation_id = invocation_id
        self.wall_started = time.time()
        self.task_id = task_id or invocation_id
        self.session_id = session_id
        self.accepted_inputs: set[str] = set()
        self.user_messages: list[UserInput] = []
        self.requirements_through = 0
        self.check_invocation_id: str | None = None
        self.requirements_pending = False

    def remaining(self) -> float:
        """UI tasks have no wall deadline; shared checks still enforce the call cap."""
        return float("inf")

    def save(self) -> None:
        super().save()
        self.state()[STATE] = {
            "version": 2,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "accepted_inputs": sorted(self.accepted_inputs),
            "user_messages": list(self.user_messages),
            "requirements_through": self.requirements_through,
            "check_invocation_id": self.check_invocation_id,
            "requirements_pending": self.requirements_pending,
            "invocation_id": self.invocation_id,
            "task": self.task,
            "cwd": str(self.cwd),
            "calls": self.calls,
            "wall_started": self.wall_started,
            "flags": asdict(self.flags),
            "mark": {
                "revision": self.mark.revision,
                "escalations": sorted(self.mark.escalations),
                "degraded": self.mark.degraded,
            },
            "seen": sorted(self.seen),
            "conflicts": sorted(self.conflicts),
            "batch_size": self.batch_size,
            "core": self.core.contribute_session_state(),
        }

    async def restore(self, saved: Mapping[str, Any]) -> None:
        if saved.get("version") not in (1, 2) or saved.get("cwd") != str(self.cwd):
            raise RuntimeError("Compass state version or workspace mismatch")
        self.task_id = saved.get("task_id", saved["invocation_id"])
        self.invocation_id = saved["invocation_id"]
        self.accepted_inputs = set(saved.get("accepted_inputs", []))
        self.user_messages = list(saved.get("user_messages", []))
        self.requirements_through = saved.get("requirements_through", 0)
        self.check_invocation_id = saved.get("check_invocation_id")
        self.requirements_pending = saved.get("requirements_pending", False)
        self.calls = saved["calls"]
        self.wall_started = saved["wall_started"]
        self.started_at = time.monotonic() - max(0, time.time() - self.wall_started)
        self.flags = WorkflowFlags(
            **{field.name: saved["flags"].get(field.name, False) for field in fields(WorkflowFlags)}
        )
        self.mark = DeliveryMark(
            revision=saved["mark"]["revision"],
            escalations=frozenset(tuple(pair) for pair in saved["mark"]["escalations"]),
            degraded=saved["mark"]["degraded"],
        )
        self.seen = set(saved["seen"])
        self.conflicts = set(saved["conflicts"])
        self.batch_size = saved["batch_size"]
        await self.core.restore_session_state(saved["core"])

    def record_input(self, identity: str, text: str, *, initial: bool = False) -> None:
        if identity in self.accepted_inputs or not text.strip():
            return
        self.accepted_inputs.add(identity)
        if not initial:
            entry: UserInput = {"message_id": identity, "text": text, "order": len(self.user_messages) + 1}
            self.user_messages.append(entry)
            self.core.record_user_input(**entry)
            self.audit("user_followup", message_id=identity)
        self.save()

    def arm_submission_check(self) -> None:
        if self.check_invocation_id != self.invocation_id:
            # One check per editing turn, including edits made inside its check.
            self.flags.check_done = False
            self.check_invocation_id = self.invocation_id

    def record_write_attempt(self) -> None:
        self.arm_submission_check()
        super().record_write_attempt()


class InteractiveBriefChat(BriefChat):
    """Native retries replay injections; only complete responses confirm delivery.

    Request attempts spend budget even when they fail. Historical evidence and
    maintenance remain monotonic: native whole-run retry refuses to roll back
    committed tool work or shown answers. No Compass retry loop or history reset.
    """

    owner: InteractiveRunState
    observed: PendingBrief | None = None

    def record_injected_inputs(self, messages: Sequence[Message]) -> None:
        for message in messages[current_turn_start(messages) :]:
            if is_injected_message(message) and user_input(message):
                self.owner.record_input("injected:" + item_identity(message), message.text)

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        self.owner.check_budget()
        self.record_injected_inputs(context.messages)
        if self.owner.flags.started and self.owner.core.user_inputs.pending:
            await self.owner.core.maintain(phase="user-followup", drain_conversation=True)
            self.owner.save()
        # Polls resume the response whose create carried the Brief. A fresh
        # request must independently observe the replayed bytes.
        if not (context.options or {}).get("continuation_token"):
            self.observed = None
        await super().process(context, call_next)

    def observe_request(self, messages: Sequence[Message]) -> None:
        # Native injection drains after our process hook. Record newly queued user
        # input on the actual wire request too, including a turn's final call.
        self.record_injected_inputs(messages)
        if self.pending and any(self.pending.text in m.text for m in messages):
            self.observed = self.pending

    def start_request(self) -> None:
        super().start_request()
        # A failed request must survive native rollback/rebuild in the ledger.
        self.owner.save()

    async def record_response(self, response: ChatResponse) -> ChatResponse:
        if response.continuation_token is not None:
            return response
        if self.pending is not None and self.observed is self.pending:
            self.confirm_delivery()
            self.observed = None
        return await super().record_response(response)


class InteractiveCompass:
    """An active task survives user turns; native new-session state starts another task."""

    def __init__(
        self,
        profile: ModelProfile,
        runtime: SessionEnvironment,
        session_dir: Path | None,
        report_usage: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if session_dir is None:
            raise ValueError("Compass requires a persistent session directory")
        self.directory = Path(session_dir) / "compass"
        self.cwd = Path(runtime.cwd)
        self.model = SelectedModel(profile, report_usage)
        self.owner: InteractiveRunState | None = None
        self.chat: InteractiveBriefChat | None = None
        self.workflow_id: str | None = None
        self.tools: list[FunctionTool] = []
        self.constants = source_constants(SOURCE_ROOT)

    @property
    def core(self) -> CompassSession:
        if self.owner is None:
            raise RuntimeError("Compass turn not started")
        return self.owner.core

    async def handoff(self, summary: str) -> str:
        if self.owner is None:
            raise RuntimeError("Compass turn not started")
        if self.owner.requirements_pending:
            return "Not started. Retry update_compass_requirements to finish the pending requirement update."
        return await self.owner.handoff(summary)

    async def update_requirements(self) -> str:
        owner = self.owner
        if owner is None or not (owner.flags.started or owner.requirements_pending):
            return "Not updated. First explore and call start_compass_brief."
        if owner.batch_size != 1:
            return "Not updated. Call update_compass_requirements alone."
        if len(owner.user_messages) <= owner.requirements_through:
            return "No new user input to apply; existing requirements remain active."
        owner.check_budget()
        owner.flags.handoff_seen = False
        owner.flags.started = False
        owner.requirements_pending = True
        owner.arm_submission_check()
        owner.save()
        await owner.core.before_fork()
        issue = REQUIREMENTS_UPDATE + dump({"original_task": owner.task, "followups": owner.user_messages})
        await owner.core.start(issue, revise=True, repository_context=await owner.core.exploration_context())
        if any(c.tag == "extraction" for c in owner.core.goals.snapshot().conclusions):
            raise InvalidCompassRun("Compass requirement update failed; editing remains blocked")
        await owner.core.maintain(phase="requirements-update", drain_conversation=True)
        owner.requirements_through = len(owner.user_messages)
        owner.requirements_pending = False
        owner.flags.started = True
        owner.conflicts.clear()
        owner.audit("requirements_updated", through=owner.requirements_through)
        owner.save()
        return "Requirements updated from user inputs. Read the next Brief before editing."

    def add_tools(self, tools: list[FunctionTool], build: ToolBuild) -> None:
        bound = bind_extension("compass", make_extension(self, interactive=True), build)
        if {t.name for t in tools}.intersection(t.name for t in bound.tools):
            raise ValueError("Compass tool name collision")
        tools.extend(bound.tools)
        self.tools = tools

    def bind(self, agent: Agent, bindings: TurnBindings, injection: InjectionMiddleware) -> None:
        self.agent = agent
        self.bindings = bindings
        self.injection = injection

    def cancel_pending(self) -> None:
        if self.chat is not None and self.chat.pending:
            self.injection.cancel("compass:" + sha(self.chat.pending.text))
        if self.workflow_id is not None:
            self.injection.cancel(self.workflow_id)

    async def begin(self, request: RunRequest) -> None:
        identity = request.origin.invocation_id
        session_id = request.origin.session_id
        state = self.bindings.backend.history_state
        saved = state.get(STATE)
        if saved and saved.get("session_id", session_id) != session_id:
            saved = None
        active = saved and any(
            saved["flags"].get(key, False) for key in ("started", "handoff_requested", "write_requested")
        )
        resume = saved and (active or saved["invocation_id"] == identity)
        task_id = saved.get("task_id", saved["invocation_id"]) if resume else identity
        reuse = self.owner is not None and self.owner.task_id == task_id and self.owner.session_id == session_id
        if not reuse:
            if self.owner is not None:
                await self.owner.core.before_fork()
            self.cancel_pending()
            task = saved["task"] if resume else "\n".join(m.text for m in request.messages if user_input(m))
            directory = self.directory / sha(task_id)[:24]
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            owner = InteractiveRunState(
                task,
                self.cwd,
                directory,
                self.constants,
                self.model,
                artifacts=Artifacts(),
                state=lambda: self.bindings.backend.history_state,
                invocation_id=identity,
                task_id=task_id,
                session_id=session_id,
            )
            if resume:
                await owner.restore(saved)
            self.owner = owner
        owner = self.owner
        # Version 1 did not persist input identities; do not re-add its opener on replay.
        initial = not resume or (saved.get("version") == 1 and saved["invocation_id"] == identity)
        owner.invocation_id = identity
        for index, message in enumerate(request.messages):
            if not user_input(message):
                continue
            entry_id = (
                "injected:" + item_identity(message)
                if is_injected_message(message)
                else "request:" + identity + ":" + str(index) + ":" + sha(message.text)
            )
            owner.record_input(entry_id, message.text, initial=initial)
        owner.tool_kinds = {tool.name: get_tool_kind(tool) for tool in self.tools}
        workflow_id = "compass-workflow:" + sha(identity)
        if self.chat is None or self.chat.owner is not owner or self.workflow_id != workflow_id:
            self.cancel_pending()
            # Native LoadedAgent assembly supplies a sequence, preserving host order.
            middleware = cast(Sequence[ChatMiddleware | FunctionMiddleware], self.agent.middleware or [])
            chain = [item for item in middleware if not isinstance(item, (BriefChat, ExplorationGuard))]
            self.chat = InteractiveBriefChat(owner, self.injection, allow_stream=True)
            self.agent.middleware = [ExplorationGuard(owner), self.chat, *chain]
            self.workflow_id = workflow_id
            self.injection.queue(INTERACTIVE_WORKFLOW, injection_id=self.workflow_id)
        owner.save()

    async def validate_handoff(self, request: RunRequest, outcome: Ok) -> Ok | Failed:
        owner = self.owner
        if owner is None:
            raise RuntimeError("Compass turn not started")
        try:
            require_handoff(owner)
        except InvalidCompassRun as exc:
            # Finish through iCode's normal failure contract, so a later
            # user turn can resume this task without abandoning the lease.
            await self.bindings.report_policy_failure(request, exc)
            return Failed(
                handle=outcome.handle,
                usage=outcome.usage,
                effects=outcome.effects,
                stop=StopCause.FAILED,
                continuation=None,
                disposition=FailureDisposition.TERMINAL,
                category=FailureCategory.DEFINITIVE,
                error=str(exc),
                exception=exc,
            )
        return outcome

    async def run(self, request: RunRequest) -> InvocationOutcome:
        await self.begin(request)
        owner = self.owner
        chat = self.chat
        if owner is None or chat is None:
            raise RuntimeError("Compass turn not started")
        try:
            outcome = await self.bindings.backend.run(request)
            if not isinstance(outcome, Ok):
                return outcome
            if not owner.flags.started and not owner.flags.handoff_requested and not owner.flags.write_requested:
                owner.audit("compass_skipped", reason="not_started_no_edit_attempt")
                return outcome
            outcome = await self.validate_handoff(request, outcome)
            if not isinstance(outcome, Ok):
                return outcome
            check = prepare_submission_check(owner)
            if check is not None:
                owner.check_invocation_id = owner.invocation_id
                owner.save()
                held, prompt = check
                if held:
                    outcome = await self.bindings.backend.run(
                        replace(
                            request,
                            intent=RunIntent.CONTINUE,
                            continuation=None,
                            messages=[Message("user", [prompt])],
                        )
                    )
            if isinstance(outcome, Ok):
                outcome = await self.validate_handoff(request, outcome)
            if isinstance(outcome, Ok) and owner.calls < MAX_CALLS:
                await owner.core.maintain(phase="icode", stage_end=True)
            return outcome
        finally:
            # Native retry owns the immutable payload until backend.run exits.
            # Afterwards a manual retry may regenerate an unconfirmed Brief;
            # do not leave a consumed draft blocking its next injection.
            if chat.pending is not None:
                self.injection.cancel("compass:" + sha(chat.pending.text))
                chat.pending = None
                chat.observed = None
            await owner.core.before_fork()
            owner.save()

    async def aclose(self) -> None:
        try:
            if self.owner is not None:
                await self.owner.core.before_fork()
                self.owner.save()
        finally:
            self.cancel_pending()
            await self.model.aclose()
