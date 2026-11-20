# Copyright (c) 2026 Chrys. All rights reserved.

"""Evidence records shared by service, orchestration, and frontend layers.

Evidence is derived from executed tools.  It records observations and their
provenance; it does not encode model-authored hypotheses or truth judgments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Literal, get_args

from compass.core.claims.models import evidence_id

logger = logging.getLogger(__name__)

EvidenceKind = Literal["observation", "test", "change", "error"]
EvidenceOutcome = Literal["ok", "error"]
EvidenceActionClass = Literal[
    "read",
    "search",
    "test",
    "new_test",
    "self_script",
    "change",
    "environment",
    "reproduce",
    "safe",
    "other",
]

EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(get_args(EvidenceKind))
EVIDENCE_OUTCOMES: Final[frozenset[str]] = frozenset(get_args(EvidenceOutcome))
EVIDENCE_ACTION_CLASSES: Final[frozenset[str]] = frozenset(get_args(EvidenceActionClass))
MAX_EVIDENCE_DISPLAY_RECORDS: Final[int] = 256
"""Frontend-only tail cap; the persisted audit Ledger remains append-only."""
MAX_EVIDENCE_SUMMARY_LENGTH: Final[int] = 500
MAX_EVIDENCE_EXCERPT_LENGTH: Final[int] = 1000
MAX_EVIDENCE_TARGET_LENGTH: Final[int] = 500
MAX_EVIDENCE_FILES: Final[int] = 16
MAX_EVIDENCE_FILE_LENGTH: Final[int] = 500


def evidence_excerpt(text: str, limit: int = MAX_EVIDENCE_EXCERPT_LENGTH) -> str:
    """Keep the start and end of output, explicitly marking omitted content.

    Test summaries and process status commonly occur at the end. This is an
    excerpt, not a semantic summary; missing content cannot establish success.
    Numbered edit output drops unchanged context first and keeps both labels.
    """
    if len(text) <= limit:
        return text
    # edit_file output must not lose the label that distinguishes old from new.
    prefix, before_label, changes = text.partition("\nBefore:\n")
    before, after_label, after = changes.partition("\nAfter:\n")
    if text.startswith("Replaced ") and before_label and after_label and limit >= 120:
        # Discard repeated context first, not the changed lines in its middle.
        before_lines, after_lines = before.splitlines(), after.splitlines()
        matcher = SequenceMatcher(
            None,
            [line.partition("|")[2] for line in before_lines],
            [line.partition("|")[2] for line in after_lines],
        )
        if all(line.partition("|")[0].strip().isdigit() for line in before_lines + after_lines):
            old, new = [], []
            for tag, i, j, a, b in matcher.get_opcodes():
                if tag == "equal":
                    old.append("[... unchanged context omitted ...]")
                    new.append("[... unchanged context omitted ...]")
                else:
                    old.extend(before_lines[i:j])
                    new.extend(after_lines[a:b])
            before, after = "\n".join(old), "\n".join(new)
        prefix = evidence_excerpt(prefix, limit // 5)
        remaining = limit - len(prefix) - len(before_label) - len(after_label)
        before_budget = remaining // 2
        return (
            prefix
            + before_label
            + evidence_excerpt(before, before_budget)
            + after_label
            + evidence_excerpt(after, remaining - before_budget)
        )
    marker = "\n[... output omitted ...]\n"
    if limit <= len(marker):
        return marker.strip()[:limit]
    head = (limit - len(marker)) // 3
    tail = limit - len(marker) - head
    start, end = text[:head], text[-tail:]
    if "\n" in start:
        start = start.rsplit("\n", 1)[0]
    if text[-tail - 1] != "\n" and "\n" in end:
        end = end.split("\n", 1)[1]
    return start + marker + end


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One bounded, tool-derived observation with execution provenance."""

    sequence: int
    """Session-local monotonic record number."""

    kind: EvidenceKind
    """Coarse deterministic classification of the executed action."""

    outcome: EvidenceOutcome
    """Whether the tool completed normally or reported an error."""

    tool_name: str
    """Executed tool name."""

    tool_kind: str = ""
    """Canonical Chrys tool kind, when available."""

    phase: str = ""
    """Workflow phase that executed the tool."""

    invocation_id: str = ""
    """Sub-agent invocation that executed the tool."""

    raw_path: str = ""
    """Session-relative path to the bounded raw result, when the excerpt is incomplete."""

    step: int = 0
    """One-based tool-execution step within the owning sub-agent invocation."""

    target: str = ""
    """Bounded path, query, or command identifying what was inspected or changed."""

    summary: str = ""
    """Deterministic one-line description of the action."""

    excerpt: str = ""
    """Bounded excerpt of the observed tool result or raised error."""

    files: tuple[str, ...] = ()
    """Paths deterministically attributed to this executed action."""

    read_range: tuple[int, int] | None = None
    """Exact 1-indexed line range observed, or ``None`` for a non-range action."""

    read_full: bool = False
    """Whether one file was read fully without output truncation."""

    opaque_write: bool = False
    """Whether the action may have changed files without naming all targets."""

    creation_candidates: tuple[str, ...] = ()
    """Paths created when their first trajectory contact is this write."""

    deleted_files: tuple[str, ...] = ()
    """Paths deterministically targeted by a successful deletion."""

    action_class: str = ""
    """Persisted deterministic shell action class (for example ``test``)."""

    exit_code: int | None = None
    """Observed process status; absence is not evidence of exit zero."""

    working_dir: str = ""
    """Execution context for distinguishing otherwise identical commands."""

    tool_call_id: str = ""
    """Host tool-call identity; links to assistant intent, not behavioral proof."""

    action_input: str = ""
    """Bounded requested input for a completed action; not proof the edit succeeded."""


def parse_evidence(raw: object) -> tuple[EvidenceRecord, ...]:
    """Tolerantly decode a persisted ``chrys_evidence`` payload."""
    if not isinstance(raw, list):
        if raw is not None:
            logger.warning("Ignoring non-list evidence payload of type %s", type(raw).__name__)
        return ()

    items: list[EvidenceRecord] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict evidence entry of type %s", type(entry).__name__)
            continue

        sequence = entry.get("sequence", 0)
        if not isinstance(sequence, int) or sequence < 1:
            sequence = len(items) + 1

        kind = entry.get("kind", "observation")
        if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
            kind = "observation"

        outcome = entry.get("outcome", "ok")
        if not isinstance(outcome, str) or outcome not in EVIDENCE_OUTCOMES:
            outcome = "ok"

        tool_name = _text(entry.get("tool_name"))
        if not tool_name:
            logger.warning("Skipping evidence entry without a tool name: %r", entry)
            continue

        items.append(
            EvidenceRecord(
                sequence=sequence,
                kind=kind,
                outcome=outcome,
                tool_name=tool_name,
                tool_kind=_text(entry.get("tool_kind")),
                action_class=normalize_evidence_action_class(entry.get("action_class")),
                exit_code=_exit_code(entry.get("exit_code")),
                working_dir=_text(entry.get("working_dir")),
                tool_call_id=_text(entry.get("tool_call_id")),
                action_input=evidence_excerpt(_text(entry.get("action_input"))),
                phase=_text(entry.get("phase")),
                invocation_id=_text(entry.get("invocation_id")),
                raw_path=_text(entry.get("raw_path")),
                step=_nonnegative_int(entry.get("step")),
                target=_text(entry.get("target"))[:MAX_EVIDENCE_TARGET_LENGTH],
                summary=_text(entry.get("summary"))[:MAX_EVIDENCE_SUMMARY_LENGTH],
                excerpt=evidence_excerpt(_text(entry.get("excerpt"))),
                files=_texts(entry.get("files")),
                read_range=_range(entry.get("read_range")),
                read_full=entry.get("read_full") is True,
                opaque_write=entry.get("opaque_write") is True,
                creation_candidates=_texts(entry.get("creation_candidates")),
                deleted_files=_texts(entry.get("deleted_files")),
            )
        )
    return tuple(items)


def serialize_evidence(items: tuple[EvidenceRecord, ...]) -> list[dict[str, object]]:
    """Encode records as the plain-dict ``chrys_evidence`` payload."""
    return [
        {
            "sequence": item.sequence,
            "kind": item.kind,
            "outcome": item.outcome,
            "tool_name": item.tool_name,
            "tool_kind": item.tool_kind,
            "action_class": normalize_evidence_action_class(item.action_class),
            "exit_code": item.exit_code,
            "working_dir": item.working_dir,
            "tool_call_id": item.tool_call_id,
            "action_input": item.action_input,
            "phase": item.phase,
            "invocation_id": item.invocation_id,
            "raw_path": item.raw_path,
            "step": item.step,
            "target": item.target,
            "summary": item.summary,
            "excerpt": item.excerpt,
            "files": list(item.files),
            "read_range": list(item.read_range) if item.read_range is not None else None,
            "read_full": item.read_full,
            "opaque_write": item.opaque_write,
            "creation_candidates": list(item.creation_candidates),
            "deleted_files": list(item.deleted_files),
        }
        for item in items
    ]


def _exit_code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def normalize_evidence_action_class(value: object) -> str:
    """Return only action-class values produced by Chrys' classifier."""
    if not isinstance(value, str) or not value:
        return ""
    return value if value in EVIDENCE_ACTION_CLASSES else "other"


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        clean = item.strip()[:MAX_EVIDENCE_FILE_LENGTH]
        if clean not in items:
            items.append(clean)
        if len(items) >= MAX_EVIDENCE_FILES:
            break
    return tuple(items)


def _range(value: object) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    start, end = value
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        return None
    if not isinstance(end, int) or isinstance(end, bool) or (end != -1 and end < start):
        return None
    return start, end


def format_evidence_id(sequence: int) -> str:
    """Backward-compatible evidence display helper."""
    return evidence_id(sequence)


def _nonnegative_int(value: object, *, fallback: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback
