"""Chrys Feature lifecycle, model context and execution observation wiring."""

from __future__ import annotations

import asyncio
import json
import logging
from hashlib import sha256

from chrys.foundation.events.types import MainTurnCompleted, SessionReady
from chrys.foundation.io.result_content import extract_result_text
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_kinds import KIND_SUB_AGENT, get_tool_kind
from chrys.foundation.util.sub_agent_context import sub_agent_parent_result_metadata
from chrys.kernel import Message
from chrys.kernel.middleware import FunctionMiddleware
from chrys.service.agent_middleware.before_model_request import BeforeModelRequestMiddleware
from chrys.service.agent_middleware.events.hook_dispatch import append_extra_context_to_result

from compass_brief.briefing.delivery import delivery_strategy
from compass_brief.briefing.render import requirement_conflicts
from compass_brief.chrys.artifacts import ChrysArtifacts
from compass_brief.chrys.delivery import brief_additions, conflict_notice, delegated_prompt
from compass_brief.chrys.event_bridge import HostEventSink
from compass_brief.chrys.events import ClaimMaintenanceRequested, ClaimsUpdated, EvidenceUpdated, GoalUpdated
from compass_brief.chrys.evidence import EvidenceMiddleware
from compass_brief.chrys.model import ChrysMaintenanceModel
from compass_brief.chrys.storage import deployment_binding, session_directory
from compass_brief.chrys.task_input import authoritative_task_text, contract_reference_context, task_input_contract
from compass_brief.evidence.actions import classify_shell
from compass_brief.model_calls.policy import CallSettings
from compass_brief.session import CompassSession
from compass_brief.state import STATE_KEY, decode_state

logger = logging.getLogger(__name__)


class CompassBriefFeature(CompassSession):
    def __init__(self, engine):
        self._engine = engine
        self._reset()

    def _reset(self):
        self._completion_lock = asyncio.Lock()
        self._exploration_lock = asyncio.Lock()
        self._last_completed_turn = None
        self._bound_session_id = self._engine.session_id
        directory = session_directory(self._engine.session_dir, self._engine.session_id)
        binding = deployment_binding()
        self._delivery_strategy = delivery_strategy(binding[1] if binding else {})
        configuration = binding[1] if binding else {}
        self._task_input_contract = task_input_contract(configuration)
        # Explicit exploration handoff creates the contract before implementation,
        # grounded in the recorded tool observations.
        self._contract_after_exploration = bool(configuration.get("contract_after_exploration"))
        self._notified_conflicts: set[str] = set()  # ponytail: in memory; a restored session may repeat one notice
        self._task_text = ""
        self._exploration_review_pending = False
        CompassSession.__init__(
            self,
            task_reference_context=lambda task: contract_reference_context(deployment_binding(), task),
            artifacts=ChrysArtifacts(),
            events=HostEventSink(self._engine.event_bus),
            model=self._host_model,
            session_id=self._engine.session_id,
            directory=directory,
            repo_path=self._workspace_cwd,
            call_settings=CallSettings.from_configuration(binding[1] if binding else {}),
        )

    def _workspace_cwd(self):
        return self._engine.workspace_primary_cwd

    def _bind(self):
        if self._bound_session_id != self._engine.session_id:
            self._reset()
        directory = session_directory(self._engine.session_dir, self._engine.session_id)
        binding = deployment_binding()
        self._delivery_strategy = delivery_strategy(binding[1] if binding else {})
        self.bind_directory(directory)

    async def restore_session_state(self, state):
        decode_state(state.get(STATE_KEY))
        await self.before_fork()
        self._bind()
        await super().restore_session_state(state)

    def contribute_session_state(self):
        self._bind()
        return super().contribute_session_state()

    async def subscribe(self) -> None:
        await self._engine.event_bus.subscribe(ClaimMaintenanceRequested, self._request_maintenance)
        await self._engine.event_bus.subscribe(SessionReady, self._session_ready)
        await self._engine.event_bus.subscribe(MainTurnCompleted, self._turn_completed)

    async def _turn_completed(self, event: MainTurnCompleted) -> None:
        """One bounded final flush, before the host's final session save.

        The host emits this only for a successful main pass, never a tool step,
        sub-agent completion, cancellation or failure. Failed maintenance keeps
        pending Evidence; retry remains an explicit user/normal policy action.
        """
        async with self._completion_lock:
            if event.session_id != self._engine.session_id or event.session_id != self._bound_session_id:
                return
            identity = (event.session_id, event.turn_number)
            if identity == self._last_completed_turn:
                return
            self._last_completed_turn = identity
            self._capture_conversation(self._engine.history_messages)
            if not (self.maintenance_status().pending_evidence or self.statements.pending or self.user_inputs.pending):
                return
            async with self.activity():
                await self.request_maintenance()

    async def _session_ready(self, event: SessionReady) -> None:
        """Bind and project a committed host session even before its first prompt.

        /new preserves the mounted sidebar and the Feature adapter instances.
        Waiting for a tool/model request to bind would leave the old Brief there.
        A model/profile reload of the same session must not discard its state.
        """
        if event.session_id != self._engine.session_id:
            return
        self._bind()
        claims = self.claims.snapshot()
        for projection in (
            GoalUpdated(state=self.goals.snapshot(), session_id=event.session_id),
            EvidenceUpdated(items=self.evidence.snapshot(), session_id=event.session_id),
            ClaimsUpdated(
                state=claims.state, items=claims.claims, latest_brief=claims.latest_brief, session_id=event.session_id
            ),
        ):
            await self._engine.event_bus.publish(projection)
        await self.publish_maintenance_status()

    async def _request_maintenance(self, event) -> None:
        if event.session_id == self._engine.session_id:
            async with self.activity():
                await self.request_maintenance(defer=self._engine.is_running)
                if not self._engine.is_running:
                    await self._engine.writer.save_current_session(raise_on_error=True)

    async def on_user_prompt(self) -> None:
        self._bind()

    def _host_model(self):
        profile = self._engine.current.manifest.active_profile
        if profile is None:
            return None
        binding = deployment_binding()
        return ChrysMaintenanceModel(
            profile,
            parent_session_id=self._engine.session_id or "",
            session_dir=self._engine.session_dir,
            report_usage=self._engine.usage_publisher.accumulate_side_call_usage,
            settings=self._call_settings,
            response_format=(binding[1] if binding else {}).get("maintenance_response_format", "json_schema"),
        )

    async def _maintain(self, **kwargs) -> None:
        # Exploration Evidence stays pending until the task contract exists, so
        # the first maintenance after it sees the whole exploration window.
        if self.goals.snapshot().description or not self._contract_after_exploration:
            await super()._maintain(**kwargs)

    async def _start_after_exploration(self, summary: str) -> str:
        """Commit the explicit exploration handoff once, before implementation."""
        async with self._exploration_lock:
            if self.goals.snapshot().description:
                return self.brief_for_request()
            if not isinstance(summary, str) or not summary.strip():
                return (
                    "Exploration is not complete. Inspect the relevant entry points, implementation and tests, "
                    "then call start_compass_brief(exploration_summary=...) with findings, source references "
                    "and unresolved assumptions. Wait for the returned G/R/K and Brief before editing."
                )
            evidence = self.evidence.snapshot()
            if not any(e.kind == "observation" and e.outcome == "ok" for e in evidence):
                return "No successful exploration observations recorded. Read the relevant repository code first."
            if not self._task_text:
                return "No original user task is bound. Start from the user's task before generating the contract."
            # The summary is an agent statement, never synthetic repository evidence.
            self.record_assistant_reply(
                message_id="compass-exploration:" + sha256((self._task_text + summary).encode()).hexdigest(),
                text="Exploration handoff (agent interpretation):\n" + summary.strip(),
                order=len(self._engine.history_messages),
            )
            self._exploration_review_pending = True
            await self.start(self._task_text, repository_context=await self.exploration_context())
            self._task_text = ""
            await self.maintain(phase="exploration-complete", drain_conversation=True)
            return "Exploration handoff complete. Review this contract before editing.\n\n" + self.brief_for_request()

    async def before_model_request(self, messages: list[Message]) -> list[Message]:
        self._bind()
        self._capture_conversation(messages)
        user = next(
            (
                m.text
                for m in reversed(messages)
                if m.role == "user"
                and m.text
                and m.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.FEATURE_CONTEXT
            ),
            "",
        )
        user = authoritative_task_text(user, self._task_input_contract)
        if self._contract_after_exploration and not self.goals.snapshot().description:
            self._task_text = self._task_text or user  # extracted later; see _start_after_exploration
            return []
        text = await self.prepare_request(user)  # also extracts the task contract on first use
        self._exploration_review_pending = False  # next model step can review the returned contract
        return brief_additions(
            text,
            messages,
            self._delivery_strategy,
            facts=self.delivery_facts()
            if getattr(BeforeModelRequestMiddleware, "supports_feature_metadata", False)
            else None,
            task_key=sha256(repr(self.goals.snapshot()).encode()).hexdigest(),
        )

    def _new_conflict_notice(self) -> str:
        """Put each new requirement conflict to the agent once, in the tool result it is reading."""
        snapshot = self.claims.snapshot()
        active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
        lines = [
            line
            for line in requirement_conflicts(active, snapshot.conflict_notes)
            if line not in self._notified_conflicts
        ]
        self._notified_conflicts.update(lines)
        return conflict_notice(lines) if lines else ""

    def _capture_conversation(self, messages: list[Message]) -> None:
        for order, message in enumerate(messages):
            # Host-generated reminders, replay boundaries and our own Brief
            # are not new user instructions or model statements.
            if message.role not in {"user", "assistant"} or message.additional_properties.get(HistoryMarkerKind.KEY):
                continue
            text = (_statement(message) if message.role == "assistant"
                    else authoritative_task_text(message.text, self._task_input_contract))
            if not text.strip():
                continue
            identity = message.message_id or sha256(f"{message.role}:{order}:{text}".encode()).hexdigest()
            if message.role == "assistant":
                self.record_assistant_reply(
                    message_id=identity, text=text, order=order,
                    tool_call_ids=tuple(c.call_id for c in message.contents if c.type == "function_call" and c.call_id),
                )
            else:
                self.record_user_input(message_id=identity, text=text, order=order)

    def function_middlewares(self, context) -> tuple:
        self._bind()
        observer = EvidenceMiddleware(self, phase=context.profile_name, workspace_cwd=context.workspace_cwd)
        return (_Control(self, context, observer), observer)

    async def on_pre_compact(self, info) -> None:
        await self.before_fork()

    async def on_compaction(self, info) -> None:
        pass

    async def on_compress(self, info) -> None:
        pass


class _Control(FunctionMiddleware):
    def __init__(self, feature: CompassBriefFeature, context, observer: EvidenceMiddleware) -> None:
        self.feature, self.context = feature, context
        self.observer = observer

    async def process(self, context, call_next) -> None:
        owner = self.feature
        async with owner.activity():
            name = context.function.name
            args = context.arguments if isinstance(context.arguments, dict) else {}
            if name == "inspect_compass_brief":
                context.result = await owner.inspect(args.get("refs", []))
                return
            if name == "maintain_compass_brief":
                context.result = await owner.maintain()
                return
            if name == "start_compass_brief" and owner._contract_after_exploration:
                context.result = await owner._start_after_exploration(args.get("exploration_summary", ""))
                return
            if name in {"start_compass_brief", "revise_compass_brief"}:
                try:
                    await owner.start(args.get("issue", ""), revise=name == "revise_compass_brief")
                    context.result = owner.brief_for_request()
                except ValueError as exc:
                    context.result = f"Error: {exc}"
                return
            tool_kind = get_tool_kind(context.function)
            command = next((args[key] for key in ("cmd", "command", "script")
                            if isinstance(args.get(key), str)), "")
            if (owner._contract_after_exploration
                    and (not owner.goals.snapshot().description or owner._exploration_review_pending)
                    and (tool_kind == "filesystem.write"
                         or (tool_kind == "shell" and classify_shell(command) == "change"))):
                context.result = (
                    "Not executed: complete exploration first. Call start_compass_brief(exploration_summary=...) "
                    "by itself, review the returned G/R/K and Brief, then retry this change."
                )
                return
            child = tool_kind == KIND_SUB_AGENT
            phase = name
            if child and owner.goals.snapshot().description and isinstance(args.get("prompt"), str):
                context.arguments = {
                    **args,
                    "prompt": delegated_prompt(
                        owner.brief_for_request(recipient="delegate", phase=phase),
                        args["prompt"],
                        owner._engine.history_messages,
                    ),
                }
            await call_next()
            # Maintenance runs inside this call, so a conflict it found is new here.
            notice = owner._new_conflict_notice() if sub_agent_parent_result_metadata.get() is None else ""
            if notice:
                context.result, _ = append_extra_context_to_result(
                    context.result, extract_result_text(context.result), [notice]
                )
            if child:
                output = extract_result_text(context.result)
                try:
                    await owner.delegation_completed(output, phase=phase)
                except Exception:
                    logger.warning("Delegated task maintenance failed", exc_info=True)


def _statement(message: Message) -> str:
    """Visible reply plus its whole reasoning, the agent's own working statements."""
    thought = ""
    for content in message.contents:
        if content.type != "text_reasoning":
            continue
        if content.text:
            thought = content.text
        else:
            try:
                details = json.loads(content.protected_data or "null")
            except ValueError:
                continue
            if isinstance(details, list):  # OpenRouter reasoning_details
                details = "".join(
                    item["text"] for item in details if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            thought = details if isinstance(details, str) else ""
        if thought.strip():
            break  # one field per message; a gateway may mirror the same text in several
    thought = thought.strip()
    if not thought:
        return message.text
    return f"{message.text}\n[reasoning]\n{thought}".strip()
