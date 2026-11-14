# Copyright (c) 2026 Chrys. All rights reserved.

"""Claim-table records shared by service, orchestration, and frontend layers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Final, Literal, get_args

logger = logging.getLogger(__name__)

ClaimKind = Literal["observation", "stated", "activity"]
ClaimGrounding = Literal["repo", "issue", "agent_only", "self_check", "new_test", "repo_test"]
ClaimVerdict = Literal["unverified", "supported", "contradicted", "stale"]
CoverageLevel = Literal["none", "self_check", "stated", "repo", "new_test", "repo_test"]

CLAIM_KINDS: Final[frozenset[str]] = frozenset(get_args(ClaimKind))
CLAIM_GROUNDINGS: Final[frozenset[str]] = frozenset(get_args(ClaimGrounding))
CLAIM_VERDICTS: Final[frozenset[str]] = frozenset(get_args(ClaimVerdict))
COVERAGE_LEVELS: Final[frozenset[str]] = frozenset(get_args(CoverageLevel))
#: Coverage that does not show a requirement holds: nothing, the agent's word, a
#: self-check, or code that was only read. Reading code shows what exists, not how
#: it behaves: on live trials an agent reading back its own implementation lifted
#: every requirement to "repo" and the pre-submit review had nothing left to list.
#: Test provenance alone is insufficient: is_weak_coverage also requires a
#: currently supported test claim. Neither predicate establishes satisfaction.
WEAK_COVERAGE_LEVELS: Final[frozenset[str]] = frozenset({"none", "self_check", "stated", "repo"})
# Storage safety bound, not the stylistic target for a concise claim.
MAX_CLAIM_STATEMENT_LENGTH: Final[int] = 2048
MAX_CLAIM_DIGEST_LENGTH: Final[int] = 300
MAX_CLAIMS: Final[int] = 4096
_CLAIM_ID_RE = re.compile(r"^C-(\d{3,})$")
_EVIDENCE_ID_RE = re.compile(r"^E-(\d{3,})$")


def statement_excerpt(value: str, limit: int) -> str:
    """Keep scope and trailing result, with an explicit gap, without rewriting facts."""
    text = value.strip()
    if len(text) <= limit:
        return text
    marker = " … "
    if limit <= len(marker):
        return "…"[:limit]
    head = (limit - len(marker)) // 3
    tail = limit - len(marker) - head
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def claim_id(sequence: int) -> str:
    """Return the canonical display identifier for a claim sequence."""
    return f"C-{sequence:03d}"


#: Requirement notes quote both sides plus, for V1 forks/conflicts, a distinguishing input.
CONFLICT_NOTE_CHARS = 360


def evidence_id(sequence: int) -> str:
    """Return the canonical display identifier for an evidence sequence."""
    return f"E-{sequence:03d}"


def parse_claim_id(value: str) -> int | None:
    """Return the numeric sequence in a canonical claim ID."""
    match = _CLAIM_ID_RE.fullmatch(value)
    return int(match.group(1)) if match is not None else None


def parse_evidence_id(value: str) -> int | None:
    """Return the numeric sequence in a canonical evidence ID."""
    match = _EVIDENCE_ID_RE.fullmatch(value)
    return int(match.group(1)) if match is not None else None


def claim_payload_has_state(raw: object) -> bool:
    """Return whether a serialized payload carries non-default Claim state."""
    if not isinstance(raw, dict):
        return False
    claims = raw.get("claims")
    if isinstance(claims, list) and claims:
        return True
    latest_brief = raw.get("latest_brief")
    if isinstance(latest_brief, str) and latest_brief:
        return True
    state = raw.get("state")
    if not isinstance(state, dict):
        return False
    return any(
        (
            _nonnegative_int(state.get("revision")),
            _nonnegative_int(state.get("processed_evidence_seq")),
            _nonnegative_int(state.get("processed_step")),
            _nonnegative_int(state.get("last_maintenance_attempt_step")),
            _nonnegative_int(state.get("last_brief_step")),
            state.get("last_maintenance_ok", True) is False,
            _optional_nonnegative_int(state.get("maintenance_degraded_after_step")) is not None,
            bool(state.get("requirement_coverage")),
            bool(state.get("weak_requirement_since")),
        )
    )


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """One immutable-statement claim backed by evidence or child claims."""

    id: str
    statement: str
    kind: ClaimKind
    phase: str
    created_step: int
    grounding: ClaimGrounding = "agent_only"
    evidence_refs: tuple[str, ...] = ()
    supporting_claim_ids: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    evidence_digest: str = ""
    superseded_by: str = ""
    assumes_external_provision: bool = False
    verdict: ClaimVerdict = "unverified"


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    """Host-computed support level and active Claim IDs for one requirement."""

    claim_ids: tuple[str, ...] = ()
    level: CoverageLevel = "none"


@dataclass(frozen=True, slots=True)
class ClaimState:
    """Authoritative active-claim projection and maintenance cursors."""

    revision: int = 0
    active_ids: tuple[str, ...] = ()
    processed_evidence_seq: int = 0
    processed_step: int = 0
    last_brief_step: int = 0
    last_maintenance_ok: bool = True
    maintenance_degraded_after_step: int | None = None
    requirement_coverage: dict[str, RequirementCoverage] = field(default_factory=dict)
    weak_requirement_since: dict[str, int] = field(default_factory=dict)
    """First evidence step at which each still-weak R/K item became weak."""

    last_maintenance_attempt_step: int = 0
    """Session-global evidence step of the latest scheduled maintenance attempt."""

    maintenance_cursor_version: int = 1
    last_maintenance_error: str = ""


@dataclass(frozen=True, slots=True)
class ClaimSnapshot:
    """Complete session claim payload used by persistence and UI events."""

    state: ClaimState = ClaimState()
    claims: tuple[ClaimRecord, ...] = ()
    conflict_notes: tuple[tuple[str, str, str], ...] = ()
    latest_brief: str = ""


def parse_claims(raw: object) -> ClaimSnapshot:
    """Tolerantly decode a persisted ``chrys_claims`` payload."""
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("Ignoring non-dict claims payload of type %s", type(raw).__name__)
        return ClaimSnapshot()

    records: list[ClaimRecord] = []
    claim_projection_cleaned = False
    raw_claims = raw.get("claims")
    if isinstance(raw_claims, list):
        for entry in raw_claims[:MAX_CLAIMS]:
            record = _parse_record(entry)
            if record is not None and all(item.id != record.id for item in records):
                records.append(record)
                persisted_verdict = entry.get("verdict") if isinstance(entry, dict) else None
                if persisted_verdict != record.verdict or entry.get("grounding") != record.grounding:
                    claim_projection_cleaned = True

    known = {item.id for item in records}
    raw_state = raw.get("state")
    state_map = raw_state if isinstance(raw_state, dict) else {}
    active_ids = tuple(value for value in _strings(state_map.get("active_ids")) if value in known)
    raw_coverage = state_map.get("requirement_coverage")
    raw_weak_since = state_map.get("weak_requirement_since")
    contract_state_cleaned = _requirement_mapping_was_cleaned(raw_coverage) or _requirement_mapping_was_cleaned(
        raw_weak_since
    )
    state = ClaimState(
        revision=_nonnegative_int(state_map.get("revision")),
        active_ids=active_ids,
        processed_evidence_seq=_nonnegative_int(state_map.get("processed_evidence_seq")),
        processed_step=_nonnegative_int(state_map.get("processed_step")),
        last_maintenance_attempt_step=_nonnegative_int(state_map.get("last_maintenance_attempt_step")),
        last_brief_step=_nonnegative_int(state_map.get("last_brief_step")),
        last_maintenance_ok=state_map.get("last_maintenance_ok", True) is not False,
        maintenance_degraded_after_step=_optional_nonnegative_int(state_map.get("maintenance_degraded_after_step")),
        requirement_coverage=_parse_requirement_coverage(raw_coverage, set(active_ids)),
        weak_requirement_since=_parse_weak_requirement_since(raw_weak_since),
        last_maintenance_error=_text(state_map.get("last_maintenance_error"))[:200],
    )
    # Older versions advanced the cursor on failure. Reopen that missing range
    # once; modern checkpoints persist version 1 even during partial recovery.
    rewound = state_map.get("maintenance_cursor_version") != 1 and state.maintenance_degraded_after_step is not None
    if rewound:
        cursor = min(state.processed_evidence_seq, state.maintenance_degraded_after_step)
        state = replace(
            state,
            processed_evidence_seq=cursor,
            processed_step=cursor,
            last_maintenance_ok=False,
            last_maintenance_error="legacy_window_pending",
        )

    notes: list[tuple[str, str, str]] = []
    raw_notes = raw.get("conflict_notes")
    if isinstance(raw_notes, list):
        for entry in raw_notes:
            if not isinstance(entry, dict):
                continue
            a, b, note = _text(entry.get("a")), _text(entry.get("b")), _text(entry.get("note"))
            endpoints_are_claims = a in known and b in known
            endpoints_are_requirement_claim = (_is_requirement_id(a) and b in known) or (
                _is_requirement_id(b) and a in known
            )
            if (endpoints_are_claims or endpoints_are_requirement_claim) and note:
                notes.append((a, b, note[:CONFLICT_NOTE_CHARS]))
            elif note and (_looks_like_requirement_id(a) or _looks_like_requirement_id(b)):
                contract_state_cleaned = True

    latest_brief = (
        "" if contract_state_cleaned or claim_projection_cleaned or rewound else _text(raw.get("latest_brief"))
    )
    return ClaimSnapshot(
        state=state,
        claims=tuple(records),
        conflict_notes=tuple(notes),
        latest_brief=latest_brief,
    )


def serialize_claims(snapshot: ClaimSnapshot) -> dict[str, object]:
    """Encode a complete claim snapshot as JSON-compatible values."""
    return {
        "state": {
            "revision": snapshot.state.revision,
            "active_ids": list(snapshot.state.active_ids),
            "processed_evidence_seq": snapshot.state.processed_evidence_seq,
            "processed_step": snapshot.state.processed_step,
            "last_maintenance_attempt_step": snapshot.state.last_maintenance_attempt_step,
            "last_brief_step": snapshot.state.last_brief_step,
            "last_maintenance_ok": snapshot.state.last_maintenance_ok,
            "maintenance_cursor_version": snapshot.state.maintenance_cursor_version,
            "last_maintenance_error": snapshot.state.last_maintenance_error,
            "maintenance_degraded_after_step": snapshot.state.maintenance_degraded_after_step,
            "requirement_coverage": {
                requirement_id: {
                    "claim_ids": list(coverage.claim_ids),
                    "level": coverage.level,
                }
                for requirement_id, coverage in snapshot.state.requirement_coverage.items()
            },
            "weak_requirement_since": dict(snapshot.state.weak_requirement_since),
        },
        "claims": [serialize_claim(record) for record in snapshot.claims],
        "conflict_notes": [{"a": a, "b": b, "note": note} for a, b, note in snapshot.conflict_notes],
        "latest_brief": snapshot.latest_brief,
    }


def serialize_claim(record: ClaimRecord) -> dict[str, object]:
    """Encode one claim record."""
    return {
        "id": record.id,
        "statement": record.statement,
        "kind": record.kind,
        "phase": record.phase,
        "created_step": record.created_step,
        "grounding": record.grounding,
        "evidence_refs": list(record.evidence_refs),
        "supporting_claim_ids": list(record.supporting_claim_ids),
        "conflicts_with": list(record.conflicts_with),
        "evidence_digest": record.evidence_digest,
        "superseded_by": record.superseded_by,
        "assumes_external_provision": record.assumes_external_provision,
        "verdict": record.verdict,
    }


def _parse_record(raw: object) -> ClaimRecord | None:
    if not isinstance(raw, dict):
        return None
    record_id = _text(raw.get("id"))
    statement = statement_excerpt(_text(raw.get("statement")), MAX_CLAIM_STATEMENT_LENGTH)
    kind = _text(raw.get("kind"))
    if parse_claim_id(record_id) is None or not statement or kind not in CLAIM_KINDS:
        return None
    evidence_refs = tuple(value for value in _strings(raw.get("evidence_refs")) if parse_evidence_id(value) is not None)
    supporting = tuple(
        value for value in _strings(raw.get("supporting_claim_ids")) if parse_claim_id(value) is not None
    )
    if not evidence_refs and not supporting and kind != "stated":
        return None
    conflicts = tuple(value for value in _strings(raw.get("conflicts_with")) if parse_claim_id(value) is not None)
    grounding = _text(raw.get("grounding"))
    if grounding not in CLAIM_GROUNDINGS:
        grounding = "agent_only"
    verdict = _text(raw.get("verdict"))
    if verdict not in CLAIM_VERDICTS:
        verdict = "unverified"
    if not evidence_refs and not supporting:
        # Conversation-only decisions are valid persisted claims, not tool
        # facts. Restore them with the same provenance limits as commit().
        grounding = "agent_only"
        if verdict != "stale":
            verdict = "unverified"
    return ClaimRecord(
        id=record_id,
        statement=statement,
        kind=kind,  # type: ignore[arg-type]
        phase=_text(raw.get("phase")),
        created_step=_nonnegative_int(raw.get("created_step")),
        grounding=grounding,  # type: ignore[arg-type]
        evidence_refs=evidence_refs,
        supporting_claim_ids=supporting,
        conflicts_with=conflicts,
        evidence_digest=_text(raw.get("evidence_digest"))[:MAX_CLAIM_DIGEST_LENGTH],
        superseded_by=_text(raw.get("superseded_by")),
        assumes_external_provision=raw.get("assumes_external_provision") is True,
        verdict=verdict,  # type: ignore[arg-type]
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return tuple(result)


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _is_requirement_id(value: str) -> bool:
    return re.fullmatch(r"[RK]\d+", value) is not None


def _looks_like_requirement_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z]\d+", value) is not None


def _requirement_mapping_was_cleaned(raw: object) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, dict):
        return True
    return any(not isinstance(requirement_id, str) or not _is_requirement_id(requirement_id) for requirement_id in raw)


def _parse_requirement_coverage(raw: object, known_claim_ids: set[str]) -> dict[str, RequirementCoverage]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, RequirementCoverage] = {}
    for requirement_id, raw_coverage in raw.items():
        if not isinstance(requirement_id, str) or not _is_requirement_id(requirement_id):
            continue
        claim_ids = raw_coverage
        level: CoverageLevel = "none"
        if isinstance(raw_coverage, dict):
            claim_ids = raw_coverage.get("claim_ids")
            raw_level = _text(raw_coverage.get("level"))
            if raw_level in COVERAGE_LEVELS:
                level = raw_level  # type: ignore[assignment]
        result[requirement_id] = RequirementCoverage(
            claim_ids=tuple(value for value in _strings(claim_ids) if value in known_claim_ids),
            level=level,
        )
    return result


def _parse_weak_requirement_since(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        requirement_id: step
        for requirement_id, step in raw.items()
        if isinstance(requirement_id, str)
        and _is_requirement_id(requirement_id)
        and isinstance(step, int)
        and not isinstance(step, bool)
        and step >= 0
    }


def grounding_counts(snapshot: ClaimSnapshot) -> dict[str, int]:
    """Count active claims by mechanically derived grounding class."""
    active = set(snapshot.state.active_ids)
    result = dict.fromkeys(("repo_test", "new_test", "repo", "agent_only", "self_check", "issue"), 0)
    for item in snapshot.claims:
        if item.id in active:
            result[item.grounding] += 1
    return result
