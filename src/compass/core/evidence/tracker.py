# Copyright (c) 2026 Chrys. All rights reserved.

"""Session-scoped append-only evidence tracker."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from compass.core.evidence.models import (
    MAX_EVIDENCE_EXCERPT_LENGTH,
    MAX_EVIDENCE_FILE_LENGTH,
    MAX_EVIDENCE_FILES,
    MAX_EVIDENCE_SUMMARY_LENGTH,
    MAX_EVIDENCE_TARGET_LENGTH,
    EvidenceKind,
    EvidenceOutcome,
    EvidenceRecord,
    evidence_excerpt,
    evidence_id,
    normalize_evidence_action_class,
    parse_evidence,
    serialize_evidence,
)
from compass.ports import MissingArtifactStore, SessionArtifactStore

logger = logging.getLogger(__name__)

DEFAULT_RAW_EVIDENCE_MAX_BYTES = 65536


class EvidenceTracker:
    """Stores an immutable, append-only sequence of executed-tool observations."""

    def __init__(
        self,
        session_dir: Path | None = None,
        *,
        raw_max_bytes: int = DEFAULT_RAW_EVIDENCE_MAX_BYTES,
        artifacts: SessionArtifactStore | None = None,
    ) -> None:
        self._artifacts = artifacts or MissingArtifactStore()
        self._items: tuple[EvidenceRecord, ...] = ()
        self._next_sequence = 1
        self._lock = asyncio.Lock()
        self._session_dir = session_dir
        self._raw_max_bytes = max(0, raw_max_bytes)

    def set_session_dir(self, session_dir: Path | None) -> None:
        """Update the audit directory after a session transition or lazy creation."""
        self._session_dir = session_dir

    async def record(
        self,
        *,
        kind: EvidenceKind,
        outcome: EvidenceOutcome,
        tool_name: str,
        tool_kind: str = "",
        action_class: str = "",
        exit_code: int | None = None,
        working_dir: str = "",
        phase: str = "",
        invocation_id: str = "",
        tool_call_id: str = "",
        action_input: str = "",
        step: int = 0,
        target: str = "",
        summary: str = "",
        excerpt: str = "",
        files: tuple[str, ...] = (),
        read_range: tuple[int, int] | None = None,
        read_full: bool = False,
        opaque_write: bool = False,
        creation_candidates: tuple[str, ...] = (),
        deleted_files: tuple[str, ...] = (),
        raw_result: str = "",
    ) -> tuple[EvidenceRecord, ...]:
        """Append one observation and assign its session-local sequence."""
        async with self._lock:
            sequence = self._next_sequence
            original = raw_result or excerpt
            excerpt = evidence_excerpt(excerpt or raw_result, MAX_EVIDENCE_EXCERPT_LENGTH)
            raw_path = await self._persist_raw(sequence, original, excerpt)
            item = EvidenceRecord(
                sequence=sequence,
                kind=kind,
                outcome=outcome,
                tool_name=tool_name,
                tool_kind=tool_kind,
                action_class=normalize_evidence_action_class(action_class),
                exit_code=exit_code,
                working_dir=working_dir,
                phase=phase,
                invocation_id=invocation_id,
                tool_call_id=tool_call_id,
                action_input=evidence_excerpt(action_input),
                raw_path=raw_path,
                step=step,
                target=target[:MAX_EVIDENCE_TARGET_LENGTH],
                summary=summary[:MAX_EVIDENCE_SUMMARY_LENGTH],
                excerpt=excerpt,
                files=_bounded_files(files),
                read_range=read_range,
                read_full=read_full,
                opaque_write=opaque_write,
                creation_candidates=_bounded_files(creation_candidates),
                deleted_files=_bounded_files(deleted_files),
            )
            self._next_sequence += 1
            self._items = (*self._items, item)
            return self._items

    async def _persist_raw(self, sequence: int, raw_result: str, excerpt: str) -> str:
        """Persist a bounded raw result when the display excerpt is incomplete."""
        if not raw_result or raw_result == excerpt or self._session_dir is None or self._raw_max_bytes <= 0:
            return ""
        encoded = raw_result.encode("utf-8", errors="backslashreplace")
        truncated = len(encoded) > self._raw_max_bytes
        payload = encoded[: self._raw_max_bytes].decode("utf-8", errors="replace")
        if truncated:
            payload += "\n\n[raw evidence truncated at configured byte limit]\n"
        relative = Path("evidence") / "raw" / f"{evidence_id(sequence)}.txt"
        try:
            await asyncio.to_thread(self._artifacts.write_text, self._session_dir / relative, payload)
        except OSError:
            logger.warning("Failed to persist raw evidence %s", evidence_id(sequence), exc_info=True)
            return ""
        return relative.as_posix()

    async def clear(self) -> None:
        """Reset the tracker to an empty session state."""
        async with self._lock:
            self._items = ()
            self._next_sequence = 1

    async def restore(self, raw: object) -> tuple[EvidenceRecord, ...]:
        """Hydrate from ``chrys_evidence``; malformed input is ignored."""
        async with self._lock:
            self._items = parse_evidence(raw)
            self._next_sequence = max((item.sequence for item in self._items), default=0) + 1
            return self._items

    def snapshot(self) -> tuple[EvidenceRecord, ...]:
        """Return the current immutable record tuple."""
        return self._items

    async def recorded_output(self, record: EvidenceRecord) -> str:
        """Read only this session's canonical, bounded saved output; never a workspace file."""
        relative = Path("evidence") / "raw" / f"{evidence_id(record.sequence)}.txt"
        if self._session_dir is None or record.raw_path != relative.as_posix() or self._raw_max_bytes <= 0:
            return record.excerpt
        path = self._session_dir / relative
        limit = min(self._raw_max_bytes, DEFAULT_RAW_EVIDENCE_MAX_BYTES)

        def read() -> str:
            try:
                with self._artifacts.open_verified_binary(path) as stream:
                    data = stream.read(limit + 1)
                text = data[:limit].decode("utf-8", errors="replace")
                return text + ("\n[recorded output truncated]" if len(data) > limit else "")
            except OSError:
                return record.excerpt

        return await asyncio.to_thread(read)

    def created_paths(self) -> frozenset[str]:
        """Return paths whose first observed session action created them."""
        return frozenset(path for item in self._items for path in item.creation_candidates)

    def changed_paths(self) -> frozenset[str]:
        """Return every path a session write touched, whether created or edited."""
        return frozenset(
            path for item in self._items if item.kind == "change" for path in (*item.files, *item.creation_candidates)
        )

    def serialize(self) -> list[dict[str, object]]:
        """Return the plain-dict ``chrys_evidence`` payload."""
        return serialize_evidence(self._items)

    def render_context(self, *, phase: str = "", invocation_id: str = "") -> str:
        """Render the current deterministic Ledger state for one model call."""
        from compass.core.briefing.evidence import format_evidence_context

        return format_evidence_context(
            self._items,
            active_phase=phase,
            active_invocation_id=invocation_id,
        )


def _bounded_files(files: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in files:
        clean = value.strip()[:MAX_EVIDENCE_FILE_LENGTH]
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= MAX_EVIDENCE_FILES:
            break
    return tuple(result)
