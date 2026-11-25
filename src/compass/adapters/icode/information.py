"""One input boundary for native context and tool-hook information.

Keep the host facts for audit; forward only understood, executed actions to
Compass evidence. A tool result is not automatically proof of a repository fact.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from chrys.extensions import ToolCall, ToolContext, ToolError, ToolResult, TurnContext
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.tool_result_metadata import result_text_exit_code
from chrys.kernel import ChatResponse, ContextProvider, Message
from chrys.kernel.middleware import ChatContext, ChatMiddleware
from compass.evidence.observation import ToolObservation
from compass.gate.controller import CompassGate
from compass.provenance import dump, sha

from .storage import Artifacts

if TYPE_CHECKING:
    from chrys.service.profiles.models.schema import ModelProfile

logger = logging.getLogger(__name__)
EVIDENCE_KINDS = frozenset({"filesystem.read", "filesystem.write", "search", "shell"})


class InformationInput:
    """Collect facts without modifying host execution, model options or gate policy."""

    def __init__(self, gate: CompassGate, artifacts: Artifacts, profile: ModelProfile) -> None:
        self.gate = gate
        self.artifacts = artifacts
        self.path = gate.directory / "adapter-input.jsonl"
        # Deliberately select identity fields; never serialize credentials, headers or options.
        self.model_identity = {"profile_id": profile.id, "provider": profile.provider, "model_id": profile.model_id}

    def _check_origin(self, origin: InvocationOrigin | None, context: ToolContext | None = None) -> InvocationOrigin:
        if (
            self.gate.closed
            or origin is None
            or origin.kind != "turn"
            or origin.session_id != self.gate.session_id
            or (context is not None and context.session_id != origin.session_id)
            or (context is not None and context.session_data_dir != self.gate.directory)
        ):
            raise ToolError("Compass information must come from the current main session.")
        return origin

    @staticmethod
    def _context(context: ToolContext) -> dict[str, Any]:
        return {
            "cwd": str(context.cwd),
            "workspace_dirs": [str(path) for path in context.workspace_dirs],
            "agent_name": context.agent_name,
            "session_id": context.session_id,
            # Relative to this audit file; stays valid when the session is copied.
            "session_data_dir": ".",
        }

    def _record(self, source: str, origin: InvocationOrigin, **facts: Any) -> None:
        record = {
            "version": 1,
            "source": source,
            "observed_at": datetime.now(UTC).isoformat(),
            "origin": asdict(origin),
            **facts,
        }
        try:
            self.artifacts.append_text(self.path, json.dumps(record, ensure_ascii=True) + "\n")
        except Exception as exc:
            # Diagnostic capture must not replace a tool's result or block the task.
            # The checkpoint/evidence store still reports its own failures independently.
            logger.warning("Compass input audit unavailable (%s)", type(exc).__name__)

    def user_input(self, text: str, origin: InvocationOrigin | None, context: TurnContext | None = None) -> None:
        origin = self._check_origin(origin, context)
        self.gate.capture_user_input(origin.invocation_id, text)
        self._record(
            "turn_context" if context is not None else "bound_user_input",
            origin,
            context=self._context(context) if context is not None else None,
            prompt=text,
            model=self.model_identity,
        )

    def tool_result(self, call: ToolCall, result: ToolResult, origin: InvocationOrigin | None) -> None:
        origin = self._check_origin(origin, call.context)
        eligible = not result.rejected and call.kind in EVIDENCE_KINDS
        if result.rejected:
            disposition = "rejected_not_executed"
        elif call.kind == "extension.compass":
            disposition = "compass_feedback_not_repository_evidence"
        elif not eligible:
            disposition = "unmapped_tool_kind"
        else:
            disposition = "repository_observation"
        requested = call.arguments.get("workdir") if call.kind == "shell" else None
        cwd = (
            resolve_workspace_path(requested, base_cwd=call.context.cwd)
            if isinstance(requested, str) and requested
            else str(call.context.cwd)
        )
        exit_code = result_text_exit_code(result.text) if call.kind == "shell" else None
        self._record(
            "after_tool",
            origin,
            context=self._context(call.context),
            tool={"name": call.name, "kind": call.kind, "call_id": call.call_id, "arguments": call.arguments},
            result=asdict(result),
            working_dir=cwd,
            working_dir_source="shell.workdir_resolved" if isinstance(requested, str) and requested else "context.cwd",
            exit_code=exit_code,
            exit_code_source="result_text_suffix" if exit_code is not None else "not_provided",
            evidence_eligible=eligible,
            disposition=disposition,
        )
        if eligible:
            self.gate.observe(
                ToolObservation(
                    tool_name=call.name,
                    tool_kind=call.kind,
                    arguments=call.arguments,
                    result=result.text,
                    failed=result.failed,
                    exit_code=exit_code,
                    working_dir=cwd,
                    phase="icode",
                    invocation_id=origin.invocation_id,
                    tool_call_id=call.call_id,
                )
            )

    async def assistant_messages(self, messages: Sequence[Message], origin: InvocationOrigin | None) -> None:
        """Capture completed public replies before their tools run, never reasoning or injected history."""
        origin = self._check_origin(origin)
        async with self.gate.lock:
            session = await self.gate.ready()
            for message in messages:
                if message.role != "assistant":
                    continue
                text = "\n".join(item.text for item in message.contents if item.type == "text" and item.text)
                if not text.strip():
                    continue
                # Provider IDs are not the host's tool-event IDs. Retain them in
                # the identity, but do not manufacture a false Evidence linkage.
                calls = [item.call_id for item in message.contents if item.type == "function_call"]
                identity = message.message_id or sha(dump([text, calls]))
                if session.record_assistant_reply(
                    message_id=f"icode:{origin.invocation_id}:{identity}",
                    text=text,
                    order=len(session.statements.seen),
                ):
                    self._record("assistant_reply", origin, message_id=identity, text=text, provider_call_ids=calls)
            self.gate.save()


class PublicStatements(ContextProvider, ChatMiddleware):
    """Observe finalized main-model replies without changing responses or adding model calls."""

    def __init__(self, information: InformationInput, origin: Callable[[], InvocationOrigin]) -> None:
        ContextProvider.__init__(self, "compass-public-statements")
        self.information = information
        self.origin = origin

    async def before_run(self, *, agent, session, context, state) -> None:
        context.extend_middleware(self.source_id, self)

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        origin = self.origin()

        async def capture(response: ChatResponse) -> ChatResponse:
            try:
                await self.information.assistant_messages(response.messages, origin)
            except Exception as exc:
                logger.warning("Compass public statement capture failed (%s)", type(exc).__name__)
            return response

        if context.stream:
            context.stream_result_hooks.append(capture)
        await call_next()
        if not context.stream and isinstance(context.result, ChatResponse):
            await capture(context.result)
