# Copyright (c) 2026 Chrys. All rights reserved.

"""Session-owned Claim Table with validated commits and audit persistence."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

from compass_brief.claims.coverage import CoverageRetraction, is_weak_coverage, retract_links
from compass_brief.claims.models import (
    CLAIM_KINDS,
    CONFLICT_NOTE_CHARS,
    CLAIM_VERDICTS,
    MAX_CLAIM_DIGEST_LENGTH,
    MAX_CLAIM_STATEMENT_LENGTH,
    ClaimGrounding,
    ClaimKind,
    ClaimRecord,
    ClaimSnapshot,
    ClaimState,
    ClaimVerdict,
    CoverageLevel,
    RequirementCoverage,
    claim_id,
    parse_claim_id,
    parse_claims,
    parse_evidence_id,
    serialize_claim,
    serialize_claims,
)
from compass_brief.evidence.actions import is_read_only_command, is_test_attempt, is_test_command
from compass_brief.evidence.models import EvidenceRecord
from compass_brief.ports import MissingArtifactStore, SessionArtifactStore

KIND_SHELL = "shell"

logger = logging.getLogger(__name__)

_GROUNDING_PRIORITY: Final[dict[ClaimGrounding, int]] = {
    "agent_only": 0,
    "issue": 1,
    "self_check": 2,
    "repo": 3,
    "new_test": 4,
    "repo_test": 5,
}


def brief_filename(revision: int) -> str:
    """Return the audit filename corresponding to one rendered Brief revision."""
    return f"brief-{max(0, revision):04d}.txt"


CorrectionKind = Literal["agent_corrected", "user_corrected", "state_changed"]
ContradictionVerdict = Literal["conflict", "fork", "boundary", "compatible", "unclear"]
_EVIDENCE_REF_RE: Final[re.Pattern[str]] = re.compile(r"\bE-\d{3,}\b")
_OPEN_FORKS_MAX: Final[int] = 3
_ACTIVITY_RE = re.compile(
    r"^(?:(?:Agent |The agent )?(?:wrote|created|edited|ran|deleted|modified)\b|(?:After cleanup, )?git (?:status|diff)\b)",
    re.IGNORECASE,
)
_HARNESS_ASSUMPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"\b(?:grader|harness|evaluation\s+harness|test\s+patch)\b[^.]{0,80}?"
    r"\b(?:will|would|should)\b[^.]{0,40}?\b(?:suppl|provid|appl|add|creat)"
    r"|"
    r"\b(?:will|would|should)\b[^.]{0,40}?\b(?:suppl|provid|appl|add|creat)"
    r"[^.]{0,80}?\b(?:grader|harness|evaluation\s+harness|test\s+patch)\b"
    r")",
    re.IGNORECASE,
)


class ClaimCommitError(ValueError):
    """Raised when a maintenance delta violates a mechanical host constraint."""


@dataclass(frozen=True, slots=True)
class NewClaim:
    statement: str
    kind: ClaimKind
    evidence_refs: tuple[str, ...]
    evidence_digest: str = ""
    grounding: ClaimGrounding | None = None
    assumes_external_provision: bool = False
    verdict: ClaimVerdict = "unverified"


@dataclass(frozen=True, slots=True)
class ClaimAugment:
    id: str
    evidence_refs: tuple[str, ...] = ()
    evidence_digest: str = ""
    verdict: ClaimVerdict | None = None


@dataclass(frozen=True, slots=True)
class ClaimMerge:
    statement: str
    supporting_claim_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimConflict:
    a: str
    b: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class RequirementContradiction:
    requirement_id: str
    claim_id: str
    verdict: ContradictionVerdict
    requirement_excerpt: str = ""
    claim_excerpt: str = ""
    resolution: str = ""
    distinguishing_input: str = ""


@dataclass(frozen=True, slots=True)
class ClaimDeactivate:
    id: str
    evidence_refs: tuple[str, ...]
    reason_kind: CorrectionKind
    superseded_by: str = ""


@dataclass(frozen=True, slots=True)
class ClaimDelta:
    new_claims: tuple[NewClaim, ...] = ()
    augment: tuple[ClaimAugment, ...] = ()
    merge: tuple[ClaimMerge, ...] = ()
    conflicts: tuple[ClaimConflict, ...] = ()
    contradiction_scan: tuple[RequirementContradiction, ...] = ()
    deactivate: tuple[ClaimDeactivate, ...] = ()
    requirement_coverage: tuple[tuple[str, tuple[str, ...]], ...] = ()
    coverage_retractions: tuple[CoverageRetraction, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimCommitResult:
    snapshot: ClaimSnapshot
    new_ids: tuple[str, ...]
    rejected_merges: int = 0


class ClaimStore:
    """In-memory authority for claims; ``session.json`` remains restore truth."""

    def __init__(
        self,
        session_dir: Path | None = None,
        *,
        artifacts: SessionArtifactStore | None = None,
        task_documents: frozenset[str] = frozenset(),
        evidence_lookup: Callable[[str], EvidenceRecord | None] | None = None,
        session_created_paths: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._artifacts = artifacts or MissingArtifactStore()
        self.task_documents = task_documents
        self._snapshot = ClaimSnapshot()
        self._claims_by_id: dict[str, ClaimRecord] = {}
        self._evidence_to_claims: dict[str, tuple[str, ...]] = {}
        self._next_sequence = 1
        self._session_dir = session_dir
        self._audit_lines: list[str] = []
        self._lock = asyncio.Lock()
        self._evidence_lookup = evidence_lookup or (lambda _evidence_id: None)
        self._session_created_paths = session_created_paths or frozenset

    def configure_grounding(
        self,
        *,
        evidence_lookup: Callable[[str], EvidenceRecord | None],
        session_created_paths: Callable[[], frozenset[str]],
    ) -> None:
        """Install the session evidence views used for mechanical grounding."""
        self._evidence_lookup = evidence_lookup
        self._session_created_paths = session_created_paths

    def set_session_dir(self, session_dir: Path | None) -> None:
        """Update the audit directory after a session transition."""
        self._session_dir = session_dir

    def snapshot(self) -> ClaimSnapshot:
        """Return the current immutable snapshot."""
        return self._snapshot

    def snapshot_for_review(self, pending_evidence_ids: frozenset[str], *, repo_path: str = "") -> ClaimSnapshot:
        """Apply recorded pending mutations read-only, without advancing maintenance.

        The submission turn records observations without maintenance. Reuse the
        commit's invalidation rules so its last edit cannot inherit an old pass.
        Pending test results still need maintenance to establish new support.
        """
        stale = _claims_staled_by_changes(
            self.active_claims(),
            records=self._claims_by_id,
            evidence_lookup=self._evidence_lookup,
            evidence_delta_ids=pending_evidence_ids,
            processed_evidence_seq=self._snapshot.state.processed_evidence_seq,
            repo_path=repo_path,
        )
        return replace(
            self._snapshot,
            claims=tuple(
                replace(item, verdict="stale") if item.id in stale else item for item in self._snapshot.claims
            ),
        )

    def active_claims(self) -> tuple[ClaimRecord, ...]:
        """Return active records in their stable projection order."""
        return tuple(self._claims_by_id[item] for item in self._snapshot.state.active_ids if item in self._claims_by_id)

    def get(self, record_id: str) -> ClaimRecord | None:
        """Look up any active or inactive claim by canonical ID."""
        return self._claims_by_id.get(record_id)

    def claims_for_evidence(self, evidence_ref: str) -> tuple[str, ...]:
        """Return the reverse mapping for an evidence ID."""
        return self._evidence_to_claims.get(evidence_ref, ())

    def serialize(self) -> dict[str, object]:
        """Return the complete JSON-compatible session payload."""
        return serialize_claims(self._snapshot)

    async def clear(self) -> None:
        """Reset all claim state for a fresh session."""
        async with self._lock:
            self._install(ClaimSnapshot())
            self._audit_lines = []

    async def restore(self, raw: object) -> ClaimSnapshot:
        """Hydrate from ``chrys_claims`` and rebuild reverse indexes."""
        async with self._lock:
            snapshot = parse_claims(raw)
            self._install(snapshot)
            restored_audit = await asyncio.to_thread(self._read_audit_lines)
            self._audit_lines = restored_audit or [
                json.dumps(serialize_claim(item), ensure_ascii=True) for item in snapshot.claims
            ]
            return self._snapshot

    async def append_brief_metrics(
        self,
        *,
        brief_file: str,
        step: int,
        phase: str,
        chars: int,
        active: int,
        shown: int,
        actions: tuple[str, ...],
        tensions_rendered: int,
        tensions_suppressed: int,
        weak_requirements: tuple[str, ...],
    ) -> None:
        """Append one final-render record to the session's Claim metrics log."""
        async with self._lock:
            if self._session_dir is None:
                return
            path = self._session_dir / "claims" / "brief_metrics.jsonl"
            brief_path = self._session_dir / "claims" / "briefs" / brief_file
            if not await asyncio.to_thread(brief_path.is_file):
                raise FileNotFoundError(f"Claim Brief metrics target does not exist: {brief_path}")
            lines = await asyncio.to_thread(_read_owner_only_lines, path, "Claim Brief metrics", self._artifacts)
            lines.append(
                json.dumps(
                    {
                        "brief_file": brief_file,
                        "step": max(0, step),
                        "phase": phase,
                        "chars": max(0, chars),
                        "active": max(0, active),
                        "shown": max(0, shown),
                        "actions": list(actions),
                        "tensions_rendered": max(0, tensions_rendered),
                        "tensions_suppressed": max(0, tensions_suppressed),
                        "weak_requirements": list(weak_requirements),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
            await asyncio.to_thread(self._artifacts.write_text, path, "\n".join(lines) + "\n")

    async def commit(
        self,
        delta: ClaimDelta,
        *,
        phase: str,
        evidence_delta_ids: frozenset[str],
        processed_evidence_seq: int,
        processed_step: int,
        retrieved_evidence_ids: frozenset[str] = frozenset(),
        brief_renderer: Callable[[ClaimSnapshot], str] | None = None,
        append_brief: bool = True,
        requirement_ids: frozenset[str] = frozenset(),
        replace_requirement_coverage: bool = False,
        maintenance_complete: bool = True,
        repo_path: str = "",
        has_agent_statements: bool = False,
    ) -> ClaimCommitResult:
        """Validate and atomically install one maintenance delta and its Brief.

        Retrieved IDs permit citations, not new processing: staleness, cursor advancement
        and coverage retractions still use only the fresh evidence window.
        """
        allowed_evidence_ids = evidence_delta_ids | retrieved_evidence_ids
        async with self._lock:
            previous_snapshot = self._snapshot
            previous_next_sequence = self._next_sequence
            previous_audit_length = len(self._audit_lines)
            records = dict(self._claims_by_id)
            active = list(self._snapshot.state.active_ids)
            active_set = set(active)
            changed: dict[str, ClaimRecord] = {}
            new_ids: list[str] = []
            new_claim_ids: list[str] = []
            rejected_merges = 0
            next_sequence = self._next_sequence
            created_paths = self._session_created_paths()

            # Verdicts describe a relationship to concrete evidence, so a
            # later candidate mutation can make that relationship obsolete.
            # Invalidate only claims that predate this transaction; new claims
            # and explicit augments below can establish a fresh verdict from
            # evidence in the same maintenance window.
            stale_reasons = _claims_staled_by_changes(
                tuple(records[record_id] for record_id in active if record_id in records),
                records=records,
                evidence_lookup=self._evidence_lookup,
                evidence_delta_ids=evidence_delta_ids,
                processed_evidence_seq=self._snapshot.state.processed_evidence_seq,
                repo_path=repo_path,
            )
            for record_id, change_refs in stale_reasons.items():
                updated = replace(
                    records[record_id],
                    verdict="stale",
                    evidence_refs=_unique((*records[record_id].evidence_refs, *change_refs)),
                )
                records[record_id] = updated
                changed[record_id] = updated

            for item in delta.new_claims:
                _validate_statement(item.statement)
                if item.kind not in CLAIM_KINDS:
                    raise ClaimCommitError(f"unknown claim kind: {item.kind}")
                _validate_evidence_refs(item.evidence_refs, allowed_evidence_ids, required=item.kind != "stated")
                if not item.evidence_refs and not has_agent_statements:
                    raise ClaimCommitError("statement without recorded agent text or evidence")
                if not item.assumes_external_provision and _HARNESS_ASSUMPTION_RE.search(item.statement):
                    logger.warning(
                        "claims: external-provision sentinel matched an unflagged claim: %s",
                        item.statement.strip(),
                    )
                record_kind: ClaimKind = (
                    "stated"
                    if item.assumes_external_provision
                    else "activity"
                    if item.kind == "observation" and _ACTIVITY_RE.match(item.statement.strip())
                    else item.kind
                )
                # A model's activity label must not hide a recorded failure or test result.
                if record_kind == "activity" and any(
                    (evidence := self._evidence_lookup(ref)) is not None
                    and (evidence.outcome == "error" or evidence.kind == "test")
                    for ref in item.evidence_refs
                ):
                    record_kind = "observation"
                record_id = claim_id(next_sequence)
                next_sequence += 1
                grounding = item.grounding or self._derive_grounding(
                    kind=record_kind,
                    evidence_refs=_unique(item.evidence_refs),
                    supporting_claim_ids=(),
                    records=records,
                    created_paths=created_paths,
                    repo_path=repo_path,
                    statement=item.statement.strip(),
                )
                if not item.evidence_refs:
                    grounding = "agent_only"
                if item.assumes_external_provision:
                    grounding = _weaker_grounding(grounding, "agent_only")
                record = ClaimRecord(
                    id=record_id,
                    statement=item.statement.strip(),
                    kind=record_kind,
                    phase=phase,
                    created_step=processed_step,
                    grounding=grounding,
                    evidence_refs=_unique(item.evidence_refs),
                    evidence_digest=item.evidence_digest.strip()[:MAX_CLAIM_DIGEST_LENGTH],
                    assumes_external_provision=item.assumes_external_provision,
                    verdict=_normalize_claim_verdict(item.verdict) if item.evidence_refs else "unverified",
                )
                records[record_id] = record
                changed[record_id] = record
                active.append(record_id)
                active_set.add(record_id)
                new_ids.append(record_id)
                new_claim_ids.append(record_id)

            for item in delta.augment:
                if item.id not in active_set:
                    raise ClaimCommitError(f"augment references inactive claim: {item.id}")
                _validate_evidence_refs(item.evidence_refs, allowed_evidence_ids, required=False)
                current = records[item.id]
                digest = item.evidence_digest.strip()[:MAX_CLAIM_DIGEST_LENGTH] or current.evidence_digest
                updated = replace(
                    current,
                    evidence_refs=_unique((*current.evidence_refs, *item.evidence_refs)),
                    evidence_digest=digest,
                    verdict=(_normalize_claim_verdict(item.verdict) if item.verdict is not None else current.verdict),
                )
                updated = replace(
                    updated,
                    grounding=_stronger_grounding(
                        current.grounding,
                        self._derive_grounding(
                            kind=updated.kind,
                            evidence_refs=updated.evidence_refs,
                            supporting_claim_ids=updated.supporting_claim_ids,
                            records=records,
                            created_paths=created_paths,
                            repo_path=repo_path,
                            statement=updated.statement,
                        ),
                    ),
                )
                records[item.id] = updated
                changed[item.id] = updated

            child_to_parent: dict[str, str] = {}
            for item in delta.merge:
                try:
                    _validate_merge(item, records=records, active_set=active_set)
                except ClaimCommitError as exc:
                    rejected_merges += 1
                    logger.info("claims: rejected merge: %s", exc)
                    continue
                supporting = _unique(item.supporting_claim_ids)
                merged_grounding = self._derive_grounding(
                    kind="observation",
                    evidence_refs=(),
                    supporting_claim_ids=supporting,
                    records=records,
                    created_paths=created_paths,
                    repo_path=repo_path,
                    statement=item.statement.strip(),
                )
                if any(records[child].grounding == "self_check" for child in supporting if child in records):
                    merged_grounding = _weaker_grounding(merged_grounding, "self_check")
                elif any(records[child].grounding == "new_test" for child in supporting if child in records):
                    merged_grounding = _weaker_grounding(merged_grounding, "new_test")
                record_id = claim_id(next_sequence)
                next_sequence += 1
                record = ClaimRecord(
                    id=record_id,
                    statement=item.statement.strip(),
                    kind="observation",
                    phase=phase,
                    created_step=processed_step,
                    grounding=merged_grounding,
                    verdict=_merged_claim_verdict(supporting, records),
                    supporting_claim_ids=supporting,
                    conflicts_with=_unique(
                        tuple(
                            conflict
                            for child_id in supporting
                            for conflict in records[child_id].conflicts_with
                            if conflict not in supporting
                        )
                    ),
                )
                records[record_id] = record
                changed[record_id] = record
                active = [value for value in active if value not in supporting]
                active_set.difference_update(supporting)
                active.append(record_id)
                active_set.add(record_id)
                new_ids.append(record_id)
                child_to_parent.update(dict.fromkeys(supporting, record_id))

            notes = list(self._snapshot.conflict_notes)
            if child_to_parent:
                for record_id in tuple(active):
                    current = records[record_id]
                    transferred = _unique(tuple(child_to_parent.get(value, value) for value in current.conflicts_with))
                    transferred = tuple(value for value in transferred if value != record_id and value in active_set)
                    if transferred != current.conflicts_with:
                        updated = replace(current, conflicts_with=transferred)
                        records[record_id] = updated
                        changed[record_id] = updated
                transferred_notes: list[tuple[str, str, str]] = []
                for a, b, note in notes:
                    resolved_a, resolved_b = child_to_parent.get(a, a), child_to_parent.get(b, b)
                    if resolved_a != resolved_b and _conflict_note_is_live(
                        resolved_a,
                        resolved_b,
                        note,
                        active_set=active_set,
                        known_claim_ids=set(records),
                    ):
                        folded_markers = tuple(
                            f"(counterpart folded into {resolved})"
                            for original, resolved in ((a, resolved_a), (b, resolved_b))
                            if original != resolved
                        )
                        transferred_note = " ".join((note, *folded_markers)).strip()[:200]
                        entry = (resolved_a, resolved_b, transferred_note)
                        if entry not in transferred_notes:
                            transferred_notes.append(entry)
                notes = transferred_notes

            resolved_conflicts: list[ClaimConflict] = []
            for item in delta.conflicts:
                a = _resolve_new_ref(item.a, new_claim_ids)
                b = _resolve_new_ref(item.b, new_claim_ids)
                if a == b or a not in active_set or b not in active_set:
                    raise ClaimCommitError(f"conflict must reference two active claims: {a}, {b}")
                resolved_conflicts.append(ClaimConflict(a=a, b=b, note=item.note.strip()[:200]))

            deactivated: set[str] = set()
            for item in delta.deactivate:
                if item.id not in records:
                    raise ClaimCommitError(f"deactivate references unknown claim: {item.id}")
                parent_ids = {
                    record_id for record_id in active_set if item.id in records[record_id].supporting_claim_ids
                }
                if item.id not in active_set and not parent_ids:
                    raise ClaimCommitError(f"deactivate references inactive claim: {item.id}")
                if item.reason_kind not in {"agent_corrected", "user_corrected", "state_changed"}:
                    raise ClaimCommitError(f"invalid deactivation reason: {item.reason_kind}")
                _validate_evidence_refs(item.evidence_refs, allowed_evidence_ids, required=True)
                superseded_by = _resolve_new_ref(item.superseded_by, new_claim_ids) if item.superseded_by else ""
                if superseded_by and superseded_by not in active_set:
                    raise ClaimCommitError(f"superseding claim is inactive: {superseded_by}")
                if superseded_by == item.id:
                    raise ClaimCommitError("a claim cannot supersede itself")
                current = records[item.id]
                if item.reason_kind == "state_changed":
                    successor = records.get(superseded_by)
                    previous = self._claims_by_id.get(item.id, current)
                    previous_seq = max((parse_evidence_id(ref) or 0 for ref in previous.evidence_refs), default=0)
                    if (
                        successor is None
                        or successor.kind != "observation"
                        or successor.verdict != "supported"
                        or not any(
                            ref in successor.evidence_refs
                            and (parse_evidence_id(ref) or 0) > previous_seq
                            for ref in item.evidence_refs
                        )
                    ):
                        raise ClaimCommitError("state change requires a supported replacement with newer supplied evidence")
                updated = replace(
                    current,
                    evidence_refs=_unique((*current.evidence_refs, *item.evidence_refs)),
                    superseded_by=superseded_by,
                )
                records[item.id] = updated
                changed[item.id] = updated
                deactivated.add(item.id)
                deactivated.update(parent_ids)

            if deactivated:
                for record_id in tuple(active):
                    record = records[record_id]
                    if any(child in deactivated for child in record.supporting_claim_ids):
                        deactivated.add(record_id)
                if any(
                    item.reason_kind == "state_changed" and records[item.id].superseded_by in deactivated
                    for item in delta.deactivate
                ):
                    raise ClaimCommitError("state change replacement must remain active")
                active = [value for value in active if value not in deactivated]
                active_set.difference_update(deactivated)

            for item in resolved_conflicts:
                for left, right in ((item.a, item.b), (item.b, item.a)):
                    current = records[left]
                    updated = replace(current, conflicts_with=_unique((*current.conflicts_with, right)))
                    records[left] = updated
                    changed[left] = updated
                if item.note:
                    notes = [entry for entry in notes if frozenset(entry[:2]) != frozenset((item.a, item.b))]
                    notes.append((item.a, item.b, item.note))

            # The scan is a secondary diagnostic: an unusable row is dropped so
            # that one bad reference cannot discard the claims, merges and
            # requirement coverage carried by the same maintenance window.  A
            # row that would silently clear a recorded tension is dropped too,
            # which leaves the tension standing.
            for item in delta.contradiction_scan:
                requirement_id = item.requirement_id
                if requirement_id not in requirement_ids:
                    logger.warning("claims: dropped contradiction row for unknown requirement %s", requirement_id)
                    continue
                if not item.claim_id:
                    continue
                record_id = _resolve_new_ref(item.claim_id, new_claim_ids)
                if record_id not in active_set:
                    logger.warning(
                        "claims: dropped contradiction row for %s referencing inactive claim %s",
                        requirement_id,
                        record_id,
                    )
                    continue
                record = records[record_id]
                grounded = record.grounding in {"repo", "new_test", "repo_test"}
                # A decision or the agent's own observation can contradict the
                # contract without repository support. Record the tension, never
                # promote it to a fact or let an unsupported claim resolve one.
                ungrounded_conflict = record.kind != "activity" and item.verdict in {"conflict", "fork", "boundary"}
                # A resolution that cites a check from this window is grounded by that check, even
                # when the claim it closes was the agent's own unverified reading (every V1 fork is).
                cited_check = item.verdict == "compatible" and bool(
                    set(_EVIDENCE_REF_RE.findall(item.resolution)) & evidence_delta_ids
                )
                if record.verdict == "stale" or record.superseded_by or not (
                    grounded or ungrounded_conflict or cited_check
                ):
                    logger.warning(
                        "claims: dropped contradiction row for %s referencing ineligible claim %s",
                        requirement_id,
                        record_id,
                    )
                    continue
                pair = frozenset((requirement_id, record_id))
                previous_note = next((note for a, b, note in notes if frozenset((a, b)) == pair), "")
                open_note = bool(previous_note) and not previous_note.startswith("resolved: ")
                # V1: only a check that ran in this window can close a tension; a stated choice cannot.
                unproven_resolution = (
                    item.verdict == "compatible"
                    and open_note
                    and not set(_EVIDENCE_REF_RE.findall(item.resolution)) & evidence_delta_ids
                )
                if unproven_resolution:
                    logger.warning(
                        "claims: kept unresolved tension %s ↔ %s; the resolution cites no check from this window",
                        requirement_id,
                        record_id,
                    )
                    continue
                open_forks = [a for a, _b, note in notes if note.startswith("fork ")]
                if item.verdict in {"fork", "boundary"} and (
                    open_note or requirement_id in open_forks or len(open_forks) >= _OPEN_FORKS_MAX
                ):
                    # One open fork per requirement and a few in total, so each one stays readable and gets run.
                    continue
                notes = [entry for entry in notes if frozenset(entry[:2]) != pair]
                if item.verdict in {"conflict", "fork", "boundary"}:
                    notes.append(
                        (
                            requirement_id,
                            record_id,
                            _requirement_conflict_note(
                                item.requirement_excerpt,
                                item.claim_excerpt,
                                item.distinguishing_input,
                                fork=item.verdict == "fork",
                                boundary=item.verdict == "boundary",
                            ),
                        )
                    )
                elif item.verdict == "compatible" and previous_note and not previous_note.startswith("resolved: "):
                    checked = (
                        "boundary run, result matches task text — "
                        if previous_note.startswith("fork (boundary")
                        else "fork checked, reading not certified — " if previous_note.startswith("fork ") else ""
                    )
                    notes.append((requirement_id, record_id, f"resolved: {checked}{item.resolution.strip()[:190]}"))
                elif previous_note:
                    notes.append((requirement_id, record_id, previous_note))

            # Merges and corrections both remove records from the active
            # projection.  Conflict edges may only connect records that
            # remain active, regardless of which operation removed one.
            for record_id in records:
                current = records[record_id]
                kept = (
                    tuple(value for value in current.conflicts_with if value in active_set)
                    if record_id in active_set
                    else ()
                )
                if kept != current.conflicts_with:
                    updated = replace(current, conflicts_with=kept)
                    records[record_id] = updated
                    changed[record_id] = updated
            notes = [
                entry
                for entry in notes
                if _conflict_note_is_live(
                    *entry,
                    active_set=active_set,
                    known_claim_ids=set(records),
                )
            ]

            # Carry relevance to an explicitly supported successor without carrying old tensions.
            coverage_successors = {
                **child_to_parent,
                **{
                    item.id: records[item.id].superseded_by
                    for item in delta.deactivate
                    if item.reason_kind == "state_changed"
                },
            }
            coverage_ids = {
                requirement_id: tuple(
                    value
                    for value in _unique(
                        tuple(coverage_successors.get(item, item) for item in requirement_coverage.claim_ids)
                    )
                    if value in active_set
                )
                for requirement_id, requirement_coverage in self._snapshot.state.requirement_coverage.items()
            }
            if replace_requirement_coverage:
                coverage_ids = dict.fromkeys(sorted(requirement_ids, key=_requirement_sort_key), ())
            # Model proposals are additions, never implicit retractions. An empty
            # row records assessment only for a previously unassessed requirement.
            # Full replacement is reserved for explicit trusted Store callers;
            # model maintenance (including phase-end) never requests it.
            added_pairs = set()
            for requirement_id, covered_by in delta.requirement_coverage:
                if requirement_id not in requirement_ids:
                    logger.info("claims: ignored unknown requirement coverage id %s", requirement_id)
                    continue
                try:
                    resolved = _unique(tuple(_resolve_new_ref(value, new_claim_ids) for value in covered_by))
                except ClaimCommitError:
                    logger.info("claims: ignored invalid coverage for %s", requirement_id)
                    continue
                if any(record_id not in active_set for record_id in resolved):
                    logger.info("claims: ignored inactive coverage for %s", requirement_id)
                    continue
                coverage_ids[requirement_id] = _unique((*coverage_ids.get(requirement_id, ()), *resolved))
                added_pairs.update((requirement_id, value) for value in resolved)
            coverage_ids, retracted = retract_links(
                coverage_ids,
                delta.coverage_retractions,
                requirement_ids=requirement_ids,
                evidence_ids=evidence_delta_ids,
                added_pairs=added_pairs,
            )
            coverage = _project_coverage(coverage_ids, records, active_set, tuple(notes))
            weak_requirement_since: dict[str, int] = {}
            # Remember even requirements the model omitted, without fabricating
            # an assessed coverage row. Full replacement may remove old task IDs.
            previous_since = {} if replace_requirement_coverage else self._snapshot.state.weak_requirement_since
            tracked_requirements = coverage.keys() | requirement_ids | previous_since.keys()
            for requirement_id in sorted(tracked_requirements, key=_requirement_sort_key):
                if is_weak_coverage(coverage.get(requirement_id), records):
                    weak_requirement_since[requirement_id] = self._snapshot.state.weak_requirement_since.get(
                        requirement_id, processed_step
                    )

            state = ClaimState(
                revision=self._snapshot.state.revision + 1,
                active_ids=tuple(active),
                processed_evidence_seq=processed_evidence_seq,
                processed_step=processed_step,
                # External result commits also use this transaction,
                # so only the explicit scheduling path may advance the model-
                # call cooldown.  Preserve its independently persisted cursor.
                last_maintenance_attempt_step=self._snapshot.state.last_maintenance_attempt_step,
                last_brief_step=self._snapshot.state.last_brief_step,
                last_maintenance_ok=True,
                maintenance_degraded_after_step=(
                    None
                    if brief_renderer is not None and append_brief and maintenance_complete
                    else self._snapshot.state.maintenance_degraded_after_step
                ),
                requirement_coverage=coverage,
                weak_requirement_since=weak_requirement_since,
            )
            ordered = tuple(sorted(records.values(), key=lambda record: parse_claim_id(record.id) or 0))
            snapshot = ClaimSnapshot(
                state=state,
                claims=ordered,
                conflict_notes=tuple(notes),
                latest_brief=self._snapshot.latest_brief,
            )
            persist_brief = brief_renderer is not None and append_brief
            if brief_renderer is not None:
                rendered_brief = brief_renderer(snapshot)
                snapshot = replace(
                    snapshot,
                    state=(
                        replace(
                            snapshot.state,
                            last_brief_step=processed_step,
                        )
                        if append_brief
                        else snapshot.state
                    ),
                    latest_brief=rendered_brief,
                )
            self._next_sequence = next_sequence
            self._snapshot = snapshot
            self._claims_by_id = records
            self._rebuild_reverse_index()
            self._audit_lines.extend(json.dumps(serialize_claim(item), ensure_ascii=True) for item in changed.values())
            try:
                await self._persist_audit(brief=persist_brief)
            except OSError:
                self._install(previous_snapshot)
                self._next_sequence = previous_next_sequence
                del self._audit_lines[previous_audit_length:]
                raise
            for item in retracted:
                logger.info(
                    "claims: coverage retracted rev=%s %s/%s evidence=%s reason=%s",
                    state.revision,
                    item.requirement_id,
                    item.claim_id,
                    ",".join(item.evidence_refs),
                    item.reason,
                )
            return ClaimCommitResult(
                snapshot=self._snapshot,
                new_ids=tuple(new_ids),
                rejected_merges=rejected_merges,
            )

    async def mark_maintenance_attempt(self, step: int) -> ClaimSnapshot:
        """Durably advance the model-call cooldown without consuming evidence."""
        step = max(0, step)
        async with self._lock:
            previous = self._snapshot
            if step <= previous.state.last_maintenance_attempt_step:
                return previous
            self._snapshot = replace(
                previous,
                state=replace(previous.state, last_maintenance_attempt_step=step),
            )
            try:
                await self._persist_audit()
            except OSError:
                self._snapshot = previous
                raise
            return self._snapshot

    def _derive_grounding(
        self,
        *,
        kind: ClaimKind,
        evidence_refs: tuple[str, ...],
        supporting_claim_ids: tuple[str, ...],
        records: dict[str, ClaimRecord],
        created_paths: frozenset[str],
        repo_path: str,
        statement: str,
    ) -> ClaimGrounding:
        candidates: list[ClaimGrounding] = [records[item].grounding for item in supporting_claim_ids if item in records]
        statement_tokens = _statement_evidence_tokens(statement)
        for evidence_ref in evidence_refs:
            evidence = self._evidence_lookup(evidence_ref)
            if evidence is None:
                continue
            if evidence.kind == "change":
                continue
            evidence_paths = (*evidence.files, evidence.target)
            has_shell_action = evidence.tool_kind == KIND_SHELL
            if (
                has_shell_action
                and evidence.kind == "test"
                and is_test_attempt(evidence.target)
                and not is_test_command(evidence.target)
            ):
                # Attempt-only pipelines cannot become test support, including
                # when their named paths happen to be candidate-authored tests.
                continue
            is_legacy = not evidence.tool_kind and not evidence.action_class
            is_self_script = (has_shell_action and evidence.action_class == "self_script") or (
                is_legacy and evidence.summary.startswith("self-script:")
            )
            is_new_test = (
                (has_shell_action and evidence.action_class == "new_test")
                or (is_legacy and evidence.summary.startswith("new-test:"))
                or any(_path_matches_created(path, created_paths, repo_path) for path in evidence_paths if path)
            )
            if is_self_script and evidence.kind == "test":
                candidates.append("self_check")
                continue
            if is_new_test and evidence.kind == "test":
                candidates.append("new_test")
                continue
            if any(
                _normalized_path(path, repo_path) == _normalized_path(document, repo_path)
                for path in evidence_paths
                if path
                for document in self.task_documents
            ):
                candidates.append("issue")
                continue
            if evidence.kind == "test" and _is_repository_test(evidence):
                candidates.append("repo_test")
                continue
            if (
                evidence.kind == "observation"
                and evidence.files
                and all(
                    _is_repo_path(path, repo_path) and not _path_matches_created(path, created_paths, repo_path)
                    for path in evidence.files
                )
                and (kind != "stated" or _evidence_matches_statement(evidence, statement_tokens))
            ):
                candidates.append("repo")
                continue
            if (
                evidence.kind == "observation"
                and not evidence.files
                and is_read_only_command(evidence.target)
                and _command_targets_repo(evidence.target, repo_path)
            ):
                candidates.append("repo")
        return max(candidates, key=_GROUNDING_PRIORITY.__getitem__, default="agent_only")

    async def mark_maintenance_failed(
        self,
        *,
        skip_window: bool,
        processed_evidence_seq: int,
        processed_step: int,
        error: str = "maintenance_failed",
    ) -> ClaimSnapshot:
        """Record failure without acknowledging evidence. Legacy skip requests are ignored."""
        async with self._lock:
            previous = self._snapshot
            degraded_after_step = self._snapshot.state.maintenance_degraded_after_step
            if degraded_after_step is None:
                degraded_after_step = self._snapshot.state.processed_step
            state = replace(
                self._snapshot.state,
                last_maintenance_ok=False,
                maintenance_degraded_after_step=degraded_after_step,
                last_maintenance_error=error[:200],
            )
            self._snapshot = replace(self._snapshot, state=state)
            try:
                await self._persist_audit()
            except OSError:
                self._snapshot = previous
                raise
            return self._snapshot

    def _install(self, snapshot: ClaimSnapshot) -> None:
        self._claims_by_id = {item.id: item for item in snapshot.claims}
        active = set(snapshot.state.active_ids)
        coverage = _project_coverage(
            {key: value.claim_ids for key, value in snapshot.state.requirement_coverage.items()},
            self._claims_by_id,
            active,
            snapshot.conflict_notes,
        )
        weak_requirement_since = {
            requirement_id: snapshot.state.weak_requirement_since.get(requirement_id, snapshot.state.processed_step)
            for requirement_id in coverage.keys() | snapshot.state.weak_requirement_since.keys()
            if is_weak_coverage(coverage.get(requirement_id), self._claims_by_id)
        }
        self._snapshot = replace(
            snapshot,
            state=replace(
                snapshot.state,
                requirement_coverage=coverage,
                weak_requirement_since=weak_requirement_since,
            ),
        )
        self._next_sequence = max((parse_claim_id(item.id) or 0 for item in snapshot.claims), default=0) + 1
        self._rebuild_reverse_index()

    def _rebuild_reverse_index(self) -> None:
        mapping: dict[str, list[str]] = {}
        for item in self._claims_by_id.values():
            for ref in item.evidence_refs:
                mapping.setdefault(ref, []).append(item.id)
        self._evidence_to_claims = {key: tuple(value) for key, value in mapping.items()}

    def _read_audit_lines(self) -> list[str]:
        if self._session_dir is None:
            return []
        path = self._session_dir / "claims" / "claims.jsonl"
        return _read_owner_only_lines(path, "Claim audit log", self._artifacts)

    async def _persist_audit(self, *, brief: bool = False) -> None:
        # Once a transaction starts writing, cancellation waits for its commit
        # marker; neither live nor saved cursors may get ahead of persistence.
        task = asyncio.create_task(self._write_audit(brief=brief))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _write_audit(self, *, brief: bool = False) -> None:
        if self._session_dir is None:
            return
        claims_dir = self._session_dir / "claims"
        state_payload = json.dumps(serialize_claims(self._snapshot), indent=2, ensure_ascii=True)
        log_payload = "\n".join(self._audit_lines)
        if log_payload:
            log_payload += "\n"
        await asyncio.to_thread(self._artifacts.write_text, claims_dir / "claims.jsonl", log_payload)
        if brief and self._snapshot.state.revision > 0:
            path = claims_dir / "briefs" / brief_filename(self._snapshot.state.revision)
            await asyncio.to_thread(self._artifacts.write_text, path, self._snapshot.latest_brief)
        # ``state.json`` is the audit directory's commit marker.  It is
        # deliberately replaced after the append-log and optional Brief.
        await asyncio.to_thread(self._artifacts.write_text, claims_dir / "state.json", state_payload)


def _read_owner_only_lines(path: Path, label: str, artifacts: SessionArtifactStore) -> list[str]:
    try:
        with artifacts.open_verified_binary(path) as file:
            payload = file.read().decode("utf-8")
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return []
        logger.warning("Ignoring unreadable %s at %s", label, path, exc_info=True)
        return []
    except UnicodeError:
        logger.warning("Ignoring undecodable %s at %s", label, path, exc_info=True)
        return []
    return [line for line in payload.splitlines() if line.strip()]


def _validate_statement(statement: str) -> None:
    if not statement.strip():
        raise ClaimCommitError("claim statement is empty")
    if len(statement.strip()) > MAX_CLAIM_STATEMENT_LENGTH:
        raise ClaimCommitError("claim statement exceeds the configured limit")


def _validate_merge(
    item: ClaimMerge,
    *,
    records: dict[str, ClaimRecord],
    active_set: set[str],
) -> None:
    _validate_statement(item.statement)
    supporting = _unique(item.supporting_claim_ids)
    if len(supporting) < 2:
        raise ClaimCommitError("merge requires at least two distinct supporting claims")
    if any(record_id not in active_set for record_id in supporting):
        raise ClaimCommitError("merge references an inactive claim")
    if any(records[record_id].supporting_claim_ids for record_id in supporting):
        raise ClaimCommitError("merge nesting is limited to one level")
    if any(other in supporting for record_id in supporting for other in records[record_id].conflicts_with):
        raise ClaimCommitError("merge would fold a recorded conflict into one claim")
    old_length = sum(len(records[record_id].statement) for record_id in supporting)
    if len(item.statement.strip()) >= old_length:
        raise ClaimCommitError("merge does not reduce the rendered claim length")


def _validate_evidence_refs(refs: tuple[str, ...], allowed: frozenset[str], *, required: bool) -> None:
    if required and not refs:
        raise ClaimCommitError("claim requires evidence references")
    unknown = set(refs) - allowed
    if unknown:
        raise ClaimCommitError(f"evidence references are outside this maintenance window: {sorted(unknown)}")


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _resolve_new_ref(value: str, new_ids: list[str]) -> str:
    if not value.startswith("<new:") or not value.endswith(">"):
        return value
    try:
        index = int(value[5:-1])
    except ValueError as exc:
        raise ClaimCommitError(f"invalid new-claim reference: {value}") from exc
    if index < 0 or index >= len(new_ids):
        raise ClaimCommitError(f"new-claim reference is out of range: {value}")
    return new_ids[index]


def _statement_evidence_tokens(statement: str) -> tuple[str, ...]:
    values = re.findall(r"`([^`]+)`|\b([\w./-]+\.py)\b", statement)
    return _unique(tuple((quoted or path).lower() for quoted, path in values if quoted or path))


def _evidence_matches_statement(evidence: EvidenceRecord, tokens: tuple[str, ...]) -> bool:
    if not tokens:
        return False
    haystack = "\n".join((evidence.target, evidence.excerpt, *evidence.files)).lower()
    return any(token in haystack for token in tokens)


def _path_matches_created(path: str, created_paths: frozenset[str], repo_path: str) -> bool:
    candidate = _normalized_path(path, repo_path)
    return any(candidate == _normalized_path(created, repo_path) for created in created_paths)


def _normalized_path(path: str, repo_path: str) -> Path:
    candidate = Path(path)
    if not repo_path:
        return candidate
    if not candidate.is_absolute() and repo_path:
        candidate = Path(repo_path) / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return candidate


def _is_repo_path(path: str, repo_path: str) -> bool:
    if not path:
        return False
    candidate = _normalized_path(path, repo_path)
    if not repo_path:
        return not candidate.is_absolute()
    try:
        root = Path(repo_path).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _is_repository_test(evidence: EvidenceRecord) -> bool:
    # New records persist the action classifier's exact decision.  Reclassify
    # the target only for old session records that predate ``action_class``.
    # Do not infer this from the display summary: a third-party tool whose name
    # merely contains "test" is rendered as a test observation but is not
    # necessarily a repository test runner.
    if evidence.tool_kind == KIND_SHELL:
        if evidence.action_class == "test":
            return not evidence.target or is_test_command(evidence.target)
        return not evidence.action_class and is_test_command(evidence.target)
    # Sessions written before tool-kind and action-class provenance existed
    # can retain repo-test grounding, but only when their target parses as an
    # actual test command.  One missing field is not legacy: fail closed when
    # a partially populated modern record carries inconsistent provenance.
    return not evidence.tool_kind and not evidence.action_class and is_test_command(evidence.target)


def _project_coverage(
    coverage_ids: dict[str, tuple[str, ...]],
    records: dict[str, ClaimRecord],
    active: set[str],
    notes: tuple[tuple[str, str, str], ...],
) -> dict[str, RequirementCoverage]:
    conflicts = {
        (a, b) if _is_requirement_id(a) else (b, a)
        for a, b, note in notes
        if not note.startswith("resolved: ")
        and ((_is_requirement_id(a) and b in active) or (_is_requirement_id(b) and a in active))
    }
    coverage = {}
    for requirement_id, claim_ids in coverage_ids.items():
        eligible = tuple(
            record_id
            for record_id in claim_ids
            if record_id in active
            and not records[record_id].assumes_external_provision
            and (requirement_id, record_id) not in conflicts
        )
        coverage[requirement_id] = RequirementCoverage(eligible, _coverage_level(eligible, records))
    return coverage


def _coverage_level(claim_ids: tuple[str, ...], records: dict[str, ClaimRecord]) -> CoverageLevel:
    priorities = {"none": 0, "self_check": 1, "stated": 2, "repo": 3, "new_test": 4, "repo_test": 5}
    levels: list[CoverageLevel] = ["none"]
    for record_id in claim_ids:
        record = records.get(record_id)
        if (
            record is None
            or record.kind == "activity"
            or record.grounding == "issue"
            or record.assumes_external_provision
        ):
            continue
        if record.grounding in {"repo", "new_test", "repo_test", "self_check"}:
            levels.append(record.grounding)
        elif record.kind == "stated" and record.grounding == "agent_only":
            levels.append("stated")
    return max(levels, key=priorities.__getitem__)


def _normalize_claim_verdict(value: object) -> ClaimVerdict:
    """Fail closed when an untyped caller supplies an unknown verdict."""
    return value if isinstance(value, str) and value in CLAIM_VERDICTS else "unverified"  # type: ignore[return-value]


def _merged_claim_verdict(claim_ids: tuple[str, ...], records: dict[str, ClaimRecord]) -> ClaimVerdict:
    """Conservatively propagate child verdicts into a merged claim."""
    verdicts = tuple(records[record_id].verdict for record_id in claim_ids if record_id in records)
    if "stale" in verdicts:
        return "stale"
    if verdicts and all(verdict == verdicts[0] for verdict in verdicts):
        return verdicts[0]
    return "unverified"


def _claims_staled_by_changes(
    claims: tuple[ClaimRecord, ...],
    *,
    records: dict[str, ClaimRecord],
    evidence_lookup: Callable[[str], EvidenceRecord | None],
    evidence_delta_ids: frozenset[str],
    processed_evidence_seq: int,
    repo_path: str,
) -> dict[str, tuple[str, ...]]:
    """Map active decided claims to later mutations that invalidate their verdict."""
    changes = tuple(
        sorted(
            (
                (evidence_ref, evidence)
                for evidence_ref in evidence_delta_ids
                if parse_evidence_id(evidence_ref) is not None
                and (evidence := evidence_lookup(evidence_ref)) is not None
                and evidence.sequence > processed_evidence_seq
                and evidence.kind == "change"
                and evidence.outcome == "ok"
            ),
            key=lambda item: (item[1].sequence, item[0]),
        )
    )
    if not changes:
        return {}
    change_paths = {evidence_ref: _evidence_paths(evidence, repo_path) for evidence_ref, evidence in changes}
    opaque_change_refs = tuple(evidence_ref for evidence_ref, evidence in changes if evidence.opaque_write)
    stale: dict[str, tuple[str, ...]] = {}
    for claim in claims:
        if claim.verdict not in {"supported", "contradicted"}:
            continue
        sources = (claim, *(records[item] for item in claim.supporting_claim_ids if item in records))
        evidence = tuple(
            item
            for source in sources
            for evidence_ref in source.evidence_refs
            if (item := evidence_lookup(evidence_ref)) is not None
        )
        evidence_paths = frozenset(path for item in evidence for path in _evidence_paths(item, repo_path))
        related_changes = tuple(
            evidence_ref for evidence_ref, paths in change_paths.items() if paths.intersection(evidence_paths)
        )
        test_conclusion = any(item.kind == "test" for item in evidence) or any(
            source.grounding in {"self_check", "new_test", "repo_test"} for source in sources
        )
        if test_conclusion:
            related_changes = tuple(evidence_ref for evidence_ref, _evidence in changes)
        elif opaque_change_refs:
            related_changes = _unique((*related_changes, *opaque_change_refs))
        if related_changes:
            stale[claim.id] = related_changes
    return stale


def _evidence_paths(evidence: EvidenceRecord, repo_path: str) -> frozenset[Path]:
    paths = _unique((*evidence.files, *evidence.creation_candidates, *evidence.deleted_files))
    if (
        not paths
        and evidence.target
        and not any(character.isspace() for character in evidence.target)
        and "." in Path(evidence.target).name
    ):
        paths = (evidence.target,)
    return frozenset(_normalized_path(path, repo_path) for path in paths)


def _is_requirement_id(value: str) -> bool:
    return re.fullmatch(r"[RK]\d+", value) is not None


def _conflict_note_is_live(
    a: str,
    b: str,
    note: str,
    *,
    active_set: set[str],
    known_claim_ids: set[str],
) -> bool:
    if _is_requirement_id(a) and b in known_claim_ids:
        return note.startswith("resolved: ") or b in active_set
    if _is_requirement_id(b) and a in known_claim_ids:
        return note.startswith("resolved: ") or a in active_set
    return a in active_set and b in active_set


def _requirement_conflict_note(
    requirement_excerpt: str,
    claim_excerpt: str,
    distinguishing_input: str = "",
    *,
    fork: bool = False,
    boundary: bool = False,
) -> str:
    requirement = " ".join(requirement_excerpt.split())[:80]
    claim = " ".join(claim_excerpt.split())[:80]
    if boundary:  # Starts with "fork " so the fork cap, display and closure rule apply unchanged.
        probe = " ".join(distinguishing_input.split())[:160]
        return f'fork (boundary not yet run): requirement: "{requirement}"; run {probe}'[:CONFLICT_NOTE_CHARS]
    note = (
        f'fork (your reading is unverified): requirement: "{requirement}" ↔ your reading: "{claim}"'
        if fork
        else f'requirement: "{requirement}" ↔ claim: "{claim}"'
    )
    probe = " ".join(distinguishing_input.split())[:160]
    if probe:
        note += f"; distinguishing input: {probe}"
    return note[:CONFLICT_NOTE_CHARS]


def _stronger_grounding(left: ClaimGrounding, right: ClaimGrounding) -> ClaimGrounding:
    return max((left, right), key=_GROUNDING_PRIORITY.__getitem__)


def _weaker_grounding(left: ClaimGrounding, right: ClaimGrounding) -> ClaimGrounding:
    return min((left, right), key=_GROUNDING_PRIORITY.__getitem__)


def _command_targets_repo(command: str, repo_path: str) -> bool:
    if not repo_path:
        return False
    normalized = repo_path.rstrip("/")
    return normalized in command or bool(re.search(rf"\bcd\s+['\"]?{re.escape(normalized)}(?:/|['\"\s;&]|$)", command))


def _normalize_statement(statement: str) -> str:
    return "".join(character for character in statement.lower() if character.isalnum())


def _requirement_sort_key(value: str) -> tuple[int, int]:
    try:
        return ({"R": 0, "K": 1}.get(value[:1], 2), int(value[1:]))
    except ValueError:
        return 2, 0
