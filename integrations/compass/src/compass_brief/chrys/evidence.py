"""Translate Chrys completed-tool facts into the public Compass observation API."""

from __future__ import annotations

import json
import logging
from difflib import unified_diff

from chrys.foundation.io.result_content import extract_result_text
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.tool_result_metadata import (
    legacy_error_text_applies,
    result_text_exit_code,
    shell_exit_code_from_metadata,
    tool_result_metadata_failure_state,
)
from chrys.foundation.util.sub_agent_context import sub_agent_parent_result_metadata
from chrys.kernel.middleware import FunctionMiddleware

from compass_brief.evidence.models import evidence_excerpt
from compass_brief.evidence.observation import ToolObservation

logger = logging.getLogger(__name__)
_SKIPPED_KINDS = frozenset({"ask_user", "sleep", "sub_agent", "todo", "goal", "compass_brief"})
_ERROR_PREFIXES = ("error:", "tool_error", "traceback (most recent call last):")


class EvidenceMiddleware(FunctionMiddleware):
    def __init__(self, session, *, phase="", workspace_cwd=None):
        self._session = session
        self._phase = phase.strip()
        self._workspace_cwd = workspace_cwd
        self._invocation_step = 0

    async def process(self, context, call_next):
        tool_kind = get_tool_kind(context.function) or ""
        if tool_kind in _SKIPPED_KINDS:
            await call_next()
            return
        try:
            await call_next()
        except Exception as exc:
            await self._record_fail_open(context, tool_kind, raised=exc)
            raise
        await self._record_fail_open(context, tool_kind)

    async def _record_fail_open(self, context, tool_kind, *, raised=None):
        try:
            await self._record(context, tool_kind, raised=raised)
        except Exception:
            logger.warning("Failed to record tool-derived evidence", exc_info=True)

    async def _record(self, context, tool_kind, *, raised=None):
        args = context.arguments if isinstance(context.arguments, dict) else {}
        tool_name = context.function.name
        result = str(raised) if raised is not None else extract_result_text(context.result)
        metadata_failed = tool_result_metadata_failure_state(context.metadata) is True
        exit_code = shell_exit_code_from_metadata(context.metadata) if tool_kind == "shell" else None
        if exit_code is None and tool_kind == "shell":
            exit_code = result_text_exit_code(result)
        legacy_error = legacy_error_text_applies(tool_kind, tool_name) and result.lstrip().lower().startswith(
            _ERROR_PREFIXES
        )
        errored = raised is not None or metadata_failed or exit_code not in (None, 0) or legacy_error
        requested_cwd = args.get("working_dir")
        execution_cwd = self._workspace_cwd or ""
        if isinstance(requested_cwd, str) and self._workspace_cwd:
            execution_cwd = resolve_workspace_path(requested_cwd, base_cwd=self._workspace_cwd)
        parent = sub_agent_parent_result_metadata.get()
        self._invocation_step += 1
        await self._session.observe(
            ToolObservation(
                tool_name=tool_name,
                tool_kind=tool_kind,
                arguments=args,
                result=result,
                failed=errored,
                exit_code=exit_code,
                working_dir=execution_cwd,
                phase=self._phase,
                step=self._invocation_step,
                invocation_id=parent.sub_agent_invocation_id if parent else "",
                tool_call_id=context.metadata.get("call_id", ""),
                action_input=_requested_action(args)
                if tool_kind in {"filesystem.write", "shell"} else "",
            )
        )


def _requested_action(args: dict) -> str:
    """Keep changed assertions/inputs instead of spending the budget on shared context.

    This describes a request only. The tool result still decides whether it ran.
    """
    old, new = args.get("old_string"), args.get("new_string")
    if isinstance(old, str) and isinstance(new, str):
        before, after = old.splitlines(), new.splitlines()
        start, tail = 0, 0
        while start < min(len(before), len(after)) and before[start] == after[start]:
            start += 1
        while tail < min(len(before), len(after)) - start and before[-tail - 1] == after[-tail - 1]:
            tail += 1
        # Trim shared context first: difflib's autojunk otherwise drops a small
        # changed assertion surrounded by hundreds of repeated lines.
        diff = "\n".join(unified_diff(
            before[max(0, start - 3):len(before) - tail + 3],
            after[max(0, start - 3):len(after) - tail + 3],
            fromfile="requested-before", tofile="requested-after", lineterm="",
        ))
        return evidence_excerpt(json.dumps({
            "path": args.get("path", args.get("file_path", "")),
            "requested_diff": evidence_excerpt(diff, 800),
        }, ensure_ascii=False))
    return evidence_excerpt(json.dumps(args, ensure_ascii=False))
