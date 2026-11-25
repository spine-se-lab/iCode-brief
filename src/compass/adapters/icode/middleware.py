"""Native iCode request middleware: evidence, Brief delivery and bounded calls."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel

from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_RESULT_METADATA_KEY,
    tool_result_metadata_failure_state,
    tool_result_metadata_is_rejected,
)
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import ChatResponse, Content, Message
from chrys.kernel.exchanges import (
    EmptyIdPolicy,
    LiveAccessor,
    NoneIdPolicy,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.kernel.middleware import ChatContext, ChatMiddleware, FunctionInvocationContext, FunctionMiddleware
from chrys.service.agent_middleware.injection import InjectionMiddleware
from compass.briefing.policy import DeliveryFacts, delivery_reason
from compass.briefing.render import requirement_conflicts
from compass.evaluation.budget import MAX_CALLS
from compass.evaluation.prompts import CLOSING, CONFLICT_REVIEW
from compass.evaluation.run_state import RunState
from compass.evidence.actions import classify_shell
from compass.evidence.observation import ToolObservation
from compass.provenance import dump, sha

# Pair only within a native exchange: provider call IDs can repeat in later turns.
EVIDENCE_PAIRING = PairingPolicy(
    call_types=frozenset({"function_call"}),
    result_types=frozenset({"function_result"}),
    include_informational_calls=False,
    none_id=NoneIdPolicy.IGNORE,
    empty_id=EmptyIdPolicy.IGNORE,
    malformed_id="treat_as_none",
)


def item_identity(item: Message | Content) -> str:
    return read_analytics_item_id(item.additional_properties) or sha(dump(item.to_dict()))


async def capture(owner: RunState, messages: Sequence[Message], *, maintain: bool) -> None:
    for message in messages:
        if message.role == "assistant":
            for index, item in enumerate(message.contents):
                if item.type in {"text", "text_reasoning"} and item.text:
                    await owner.core.record_agent_statement(
                        f"{item_identity(message)}:{index}", item.text, phase="icode", step=owner.calls
                    )
    accessor = LiveAccessor()
    for exchange in iter_exchanges(messages, accessor):
        paired = pair_results(messages, exchange, accessor, EVIDENCE_PAIRING)
        for assignments in paired.truthy_assignments.values():
            for call_at, result_at in assignments:
                if result_at is None:
                    continue
                call = messages[call_at.message_index].contents[call_at.content_index]
                item = messages[result_at.message_index].contents[result_at.content_index]
                identity = item_identity(item)
                if identity in owner.seen:
                    continue
                owner.seen.add(identity)
                name, args = call.name, call.arguments
                kind = owner.tool_kinds.get(name)
                meta = (item.additional_properties or {}).get(TOOL_RESULT_METADATA_KEY, {})
                if kind not in {"filesystem.read", "filesystem.write", "search", "shell"}:
                    continue
                if meta.get("compass_control") or tool_result_metadata_is_rejected(meta):
                    continue
                if isinstance(args, str):
                    args = json.loads(args)
                code = meta.get("shell_exit_code", meta.get("process_exit_code"))
                code = code if type(code) is int else None
                await owner.core.observe(
                    ToolObservation(
                        tool_name=cast(str, name),
                        tool_kind=kind,
                        arguments=cast(dict, args or {}),
                        result=item.result if isinstance(item.result, str) else dump(item.result),
                        failed=tool_result_metadata_failure_state(meta) is True or bool(item.exception),
                        exit_code=code,
                        working_dir=str(owner.cwd),
                        phase="icode",
                        tool_call_id=identity,
                        step=owner.calls,
                    ),
                    schedule_maintenance=maintain and owner.flags.started and owner.calls < MAX_CALLS,
                )


def check_edit(owner: RunState, *, name: str, kind: str | None, arguments: dict[str, Any]) -> str | None:
    """Shared policy; native hooks may recheck approval-edited arguments.

    Recording a write attempt is monotonic and safe to repeat. It must not
    spend model budget or acknowledge a Brief, even on approval rechecks.
    """
    changes = kind == "filesystem.write" or (
        kind == "shell" and classify_shell(arguments.get("command", "")) == "change"
    )
    if changes:
        owner.record_write_attempt()
    if changes and not owner.flags.handoff_seen:
        owner.audit("write_held", tool=name)
        return "Not executed. Explore, call start_compass_brief alone, and read the Brief before editing."
    return None


class ExplorationGuard(FunctionMiddleware):
    """Dedicated evaluation adapter; normal sessions use Extension.before_tool."""

    def __init__(self, owner: RunState) -> None:
        self.owner = owner

    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        args = context.arguments
        args = cast(BaseModel, args).model_dump() if hasattr(args, "model_dump") else dict(args)
        denial = check_edit(
            self.owner, name=context.function.name, kind=get_tool_kind(context.function), arguments=args
        )
        if denial is not None:
            context.metadata[TOOL_RESULT_METADATA_KEY] = {"compass_control": True, "failed": True}
            context.result = denial
            return
        await call_next()


@dataclass(frozen=True)
class PendingBrief:
    """One immutable delivery envelope, retained by identity during native retries."""

    text: str
    facts: DeliveryFacts
    conflicts: set[str]
    reason: str
    purpose: Literal["brief", "closing"] = "brief"


class BriefChat(ChatMiddleware):
    def __init__(self, owner: RunState, injection: InjectionMiddleware, *, allow_stream: bool = False) -> None:
        self.owner = owner
        self.injection = injection
        self.allow_stream = allow_stream
        self.pending: PendingBrief | None = None

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        run_state = self.owner
        run_state.check_budget()
        if context.stream and not self.allow_stream:
            raise RuntimeError("Compass experiment profile must use stream=false")
        await capture(run_state, context.messages, maintain=True)
        facts = run_state.core.delivery_facts()
        snapshot = run_state.core.claims.snapshot()
        active = {x.id: x for x in snapshot.claims if x.id in snapshot.state.active_ids}
        conflicts = set(requirement_conflicts(active, snapshot.conflict_notes)) - run_state.conflicts
        reason = delivery_reason(facts, run_state.mark)
        text = ""
        purpose: Literal["brief", "closing"] = "brief"
        if run_state.flags.started and (not run_state.flags.handoff_seen or reason or conflicts):
            text = run_state.core.brief_for_request(recipient="model", phase="icode")
            reason = reason or "exploration-or-conflict"
            if conflicts:
                text += "\n\n[Compass — new requirement conflict]\n" + "\n".join(sorted(conflicts)) + CONFLICT_REVIEW
        elif run_state.calls > 200 and not run_state.flags.closing_seen:
            text, reason, purpose = CLOSING, "closing-budget", "closing"
        if text and self.pending is None:
            if not run_state.flags.rules_seen:
                text += run_state.constants["SUBMISSION_RULES"]
            self.pending = PendingBrief(text=text, facts=facts, conflicts=conflicts, reason=reason, purpose=purpose)
            self.injection.queue(text, injection_id="compass:" + sha(text))

        context.request_message_observers.append(self.observe_request)
        self.start_request()
        if context.stream:
            context.stream_result_hooks.append(self.record_response)
        await call_next()
        if not context.stream:
            await self.record_response(cast(ChatResponse, context.result))

    def observe_request(self, messages: Sequence[Message]) -> None:
        if self.pending and any(self.pending.text in m.text for m in messages):
            self.confirm_delivery()

    def confirm_delivery(self) -> None:
        run_state = self.owner
        brief = self.pending
        if brief is None:
            raise RuntimeError("No pending Compass Brief to confirm")
        if brief.purpose == "closing":
            run_state.flags.closing_seen = True
            run_state.flags.rules_seen = True
            run_state.audit("closing_delivered", text=brief.text, sha256=sha(brief.text))
            self.pending = None
            return
        run_state.mark = run_state.mark.after(brief.facts)
        run_state.conflicts.update(brief.conflicts)
        run_state.flags.rules_seen = True
        run_state.flags.handoff_seen = run_state.flags.started
        run_state.audit("brief_delivered", reason=brief.reason, text=brief.text, sha256=sha(brief.text))
        self.pending = None

    def start_request(self) -> None:
        # Count one middleware invocation, not the two request observations.
        self.owner.check_budget()
        self.owner.calls += 1
        self.owner.audit("main_request")

    async def record_response(self, response: ChatResponse) -> ChatResponse:
        run_state = self.owner
        run_state.batch_size = sum(item.type == "function_call" for m in response.messages for item in m.contents)
        await capture(run_state, response.messages, maintain=False)
        run_state.audit(
            "main_response", model=response.model, response_id=response.response_id, usage=response.usage_details
        )
        run_state.save()
        return response
