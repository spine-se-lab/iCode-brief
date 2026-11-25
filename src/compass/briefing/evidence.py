# Copyright (c) 2026 Chrys. All rights reserved.

"""Deterministic, bounded Ledger state rendering for model context."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from compass.evidence.actions import phase_name
from compass.evidence.models import EvidenceRecord
from compass.evidence.semantics import current_focus, read_coverage, stale_sequences, visible_items

MAX_INJECTED_EVIDENCE: Final[int] = 10
MAX_EVIDENCE_CONTEXT_CHARS: Final[int] = 7000
_PREVIOUSLY_READ_FILES_CAP: Final[int] = 8
_PREVIOUSLY_READ_RANGES_CAP: Final[int] = 3
_RENDERED_EXCERPT_CHARS: Final[int] = 420


def format_evidence_context(
    items: tuple[EvidenceRecord, ...],
    *,
    outstanding: tuple[str, ...] = (),
    active_phase: str = "",
    active_invocation_id: str = "",
) -> str:
    """Render Ledger's inform path without making correctness decisions.

    The shape mirrors Ledger's Worker-facing ``<manager_memory>`` tail:
    current focus is derived from successful writes, previously-read coverage
    is shown only when the executed read proves it, and freshness wording is
    withheld after an opaque write. Chrys additionally retains a bounded
    cross-stage observation section because each delegated task is a separate
    agent and does not inherit the previous phase's raw message history.

    Concurrent ensemble invocations in the same phase are isolated: an invocation
    sees its own records but not a sibling sample's work. Later phases see the
    earlier phase's records.
    """
    visible = visible_items(
        items,
        active_phase=phase_name(active_phase),
        active_invocation_id=active_invocation_id,
    )
    lines = [
        "<manager_memory>",
        "Ledger state derived only from executed tools; these are historical observations, not truth judgments.",
    ]

    focus = current_focus(visible)
    if focus:
        lines.extend(("", "## Current focus", *(f"- {path}" for path in focus)))

    previously_read = _render_previously_read(visible, exclude_files=set(focus))
    if previously_read:
        lines.extend(("", *previously_read))

    selected = visible[-MAX_INJECTED_EVIDENCE:]
    if selected:
        stale = stale_sequences(visible)
        lines.extend(("", "## Recent tool observations"))
        for item in selected:
            phase = item.phase or "workflow"
            marker = " stale-after-change" if item.sequence in stale else ""
            target = f" target={item.target}" if item.target else ""
            lines.append(
                f"- [E{item.sequence} {phase} {item.kind}/{item.outcome}{marker}] "
                f"{item.summary or item.tool_name}{target}"
            )
            if item.excerpt:
                excerpt = item.excerpt[:_RENDERED_EXCERPT_CHARS]
                if len(item.excerpt) > _RENDERED_EXCERPT_CHARS:
                    excerpt = f"{excerpt.rstrip()}…"
                lines.append(f"  observed: {excerpt}")
    else:
        lines.extend(("", "- No tool-derived evidence has been recorded yet."))

    if outstanding:
        lines.extend(("", "## Open questions", *(f"- {item}" for item in outstanding)))
    lines.append("</manager_memory>")
    return _bounded(lines)


def _render_previously_read(
    items: tuple[EvidenceRecord, ...],
    *,
    exclude_files: set[str],
    root: str = "",
    max_files: int | None = _PREVIOUSLY_READ_FILES_CAP,
) -> list[str]:
    entries = []
    for read in read_coverage(items):
        if read.path in exclude_files:
            continue
        marker = {
            "changed": "edited afterwards — the version on disk is newer",
            "unchanged": "not modified since",
            "unknown": "",
        }[read.freshness]
        if read.full:
            coverage = "read fully"
        else:
            shown = ", ".join(
                _format_range(value)
                for value in (read.ranges if max_files is None else read.ranges[:_PREVIOUSLY_READ_RANGES_CAP])
            )
            remaining = len(read.ranges) - _PREVIOUSLY_READ_RANGES_CAP
            if max_files is not None and remaining > 0:
                shown = f"{shown}, +{remaining} more ranges"
            coverage = f"lines {shown} read"
        detail = f"{coverage}; {marker}" if marker else coverage
        entries.append((read.sequence, f"- {_display_path(read.path, root)} [{detail}]"))

    if not entries:
        return []
    entries.sort(reverse=True)
    return ["## Previously read", *(line for _, line in entries[:max_files])]


def render_previously_read(
    items: tuple[EvidenceRecord, ...],
    *,
    root: str = "",
    max_files: int | None = _PREVIOUSLY_READ_FILES_CAP,
) -> list[str]:
    """Return Brief-ready read coverage without the legacy section heading."""
    rendered = _render_previously_read(items, exclude_files=set(), root=root, max_files=max_files)
    return rendered[1:] if rendered else []


def _display_path(path: str, root: str) -> str:
    if not root:
        return path
    try:
        return Path(path).relative_to(Path(root)).as_posix()
    except ValueError:
        return path


def _format_range(value: tuple[int, int]) -> str:
    start, end = value
    return f"{start}-EOF" if end == -1 else f"{start}-{end}"


def _bounded(lines: list[str]) -> str:
    rendered = "\n".join(lines)
    if len(rendered) <= MAX_EVIDENCE_CONTEXT_CHARS:
        return rendered
    suffix = "\n- Older Ledger observations omitted to stay within the context budget.\n</manager_memory>"
    return f"{rendered[: MAX_EVIDENCE_CONTEXT_CHARS - len(suffix)].rstrip()}{suffix}"
