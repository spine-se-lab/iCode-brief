"""Executed-evidence visibility, coverage and freshness; no text or host dependency."""

from dataclasses import dataclass

from compass_brief.evidence.actions import normalize_path, phase_name
from compass_brief.evidence.models import EvidenceRecord


@dataclass(frozen=True, slots=True)
class _ReadState:
    sequence: int
    read_range: tuple[int, int] | None
    read_full: bool
    mutation_stamp: int
    opaque_stamp: int


@dataclass(frozen=True, slots=True)
class ReadCoverage:
    path: str
    sequence: int
    full: bool
    ranges: tuple[tuple[int, int], ...]
    freshness: str


def visible_items(
    items: tuple[EvidenceRecord, ...],
    *,
    active_phase: str,
    active_invocation_id: str,
) -> tuple[EvidenceRecord, ...]:
    if not active_phase or not active_invocation_id:
        return items
    return tuple(
        item
        for item in items
        if not (
            phase_name(item.phase) == active_phase and item.invocation_id and item.invocation_id != active_invocation_id
        )
    )


def current_focus(items: tuple[EvidenceRecord, ...], *, limit: int = 3) -> tuple[str, ...]:
    mentioned: set[str] = set()
    created: set[str] = set()
    latest_edit: dict[str, int] = {}
    latest_delete: dict[str, int] = {}

    for item in items:
        files = _item_files(item)
        for path in item.creation_candidates:
            normalized = normalize_path(path)
            if normalized and normalized not in mentioned:
                created.add(normalized)
        if item.outcome == "ok" and item.kind == "change":
            deleted = {normalize_path(path) for path in item.deleted_files}
            for path in files:
                if path and path not in deleted:
                    latest_edit[path] = item.sequence
            for path in deleted:
                if path:
                    latest_delete[path] = item.sequence
        mentioned.update(files)

    candidates = [
        (sequence, path)
        for path, sequence in latest_edit.items()
        if path not in created and sequence > latest_delete.get(path, -1) and _is_focus_worthy_file(path)
    ]
    candidates.sort(reverse=True)
    return tuple(path for _, path in candidates[:limit])


def _merge_ranges(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(values):
        if merged:
            previous_start, previous_end = merged[-1]
            previous_normalized_end = (1 << 63) - 1 if previous_end == -1 else previous_end
            if start <= previous_normalized_end + 1:
                merged[-1] = (previous_start, -1 if end == -1 or previous_end == -1 else max(previous_end, end))
                continue
        merged.append((start, end))
    return merged


def stale_sequences(items: tuple[EvidenceRecord, ...]) -> set[int]:
    changed_at: dict[str, int] = {}
    for item in items:
        if item.kind != "change" or item.outcome != "ok":
            continue
        for path in _item_files(item):
            changed_at[path] = item.sequence
    stale: set[int] = set()
    for item in items:
        if item.kind not in {"observation", "test"}:
            continue
        if any(changed_at.get(path, -1) > item.sequence for path in _item_files(item)):
            stale.add(item.sequence)
    return stale


def _item_files(item: EvidenceRecord) -> tuple[str, ...]:
    if item.files:
        return tuple(normalize_path(path) for path in item.files if path)
    # Compatibility for the first Evidence schema, whose single path lived
    # only in ``target``. Commands and search expressions contain whitespace
    # and are deliberately not guessed to be files.
    if item.target and not any(char.isspace() for char in item.target) and "." in item.target:
        return (normalize_path(item.target),)
    return ()


def _is_focus_worthy_file(path: str) -> bool:
    if not path:
        return False
    if path.startswith(("/tmp/", "tmp/")):
        return False
    return not path.endswith((".bak", ".orig", ".swp"))


def read_coverage(items: tuple[EvidenceRecord, ...]) -> tuple[ReadCoverage, ...]:
    reads: dict[str, list[_ReadState]] = {}
    mutation_clock: dict[str, int] = {}
    opaque_stamp = 0

    for item in items:
        files = _item_files(item)
        if item.outcome == "ok" and item.kind == "change":
            deleted = {normalize_path(path) for path in item.deleted_files}
            for path in files:
                if path and path not in deleted:
                    mutation_clock[path] = mutation_clock.get(path, 0) + 1
        if item.outcome == "ok" and item.opaque_write:
            opaque_stamp += 1
        if item.outcome != "ok" or len(files) != 1 or not (item.read_full or item.read_range is not None):
            continue
        path = files[0]
        reads.setdefault(path, []).append(
            _ReadState(
                sequence=item.sequence,
                read_range=item.read_range,
                read_full=item.read_full,
                mutation_stamp=mutation_clock.get(path, 0),
                opaque_stamp=opaque_stamp,
            )
        )

    entries = []
    for path, records in reads.items():
        latest = max(records, key=lambda record: record.sequence)
        current_mutation = mutation_clock.get(path, 0)
        freshness = (
            "changed"
            if current_mutation > latest.mutation_stamp
            else "unchanged"
            if latest.opaque_stamp == opaque_stamp
            else "unknown"
        )
        entries.append(
            ReadCoverage(
                path,
                latest.sequence,
                any(r.read_full for r in records),
                tuple(_merge_ranges([r.read_range for r in records if r.read_range is not None])),
                freshness,
            )
        )
    return tuple(sorted(entries, key=lambda item: item.sequence, reverse=True))
