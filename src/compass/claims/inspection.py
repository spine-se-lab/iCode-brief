# Copyright (c) 2026 Chrys. All rights reserved.

"""Read-only progressive inspection of Claims, Evidence, and Goal state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from compass.briefing.render import CANONICAL_DETAIL_ENTRY, render_active_brief
from compass.claims.models import RequirementCoverage, grounding_counts, parse_claim_id, parse_evidence_id
from compass.evidence.models import EvidenceRecord, evidence_id
from compass.task_contract.extraction import requirement_entries


class ClaimInspector:
    """Session-bound implementation of ``inspect_claims``."""

    def __init__(
        self,
        claim_store,
        evidence_tracker,
        goal_tracker,
        session_dir: Path | None,
        detail_entry: str = CANONICAL_DETAIL_ENTRY,
        detail_renderer=None,
    ) -> None:
        self._claims = claim_store
        self._evidence = evidence_tracker
        self._goal = goal_tracker
        self._session_dir = session_dir
        # An inspection reply embeds a Brief, which names this entry too; a reply
        # must not point at a tool the asking host cannot execute.
        self._detail_entry = detail_entry
        self._detail_renderer = detail_renderer

    async def inspect_claims(
        self,
        refs: Annotated[
            list[str],
            "Claim IDs (C-012), requirement/constraint IDs (R3/K1), evidence IDs (E-034), or 'goal'. Empty returns the Brief.",
        ] = [],  # noqa: B006
    ) -> str:
        """Inspect claim scope or provenance when it matters to a decision.

        Use this when claims conflict, when a decision depends on a claim's
        exact scope, or before correcting a claim. Do not call it every step.
        The operation is read-only and never triggers maintenance.
        """
        if not refs:
            snapshot = self._claims.snapshot()
            evidence = self._evidence.snapshot()
            brief = render_active_brief(
                snapshot,
                self._goal.snapshot(),
                evidence,
                detail_entry=self._detail_entry,
                detail_renderer=self._detail_renderer,
            )
            latest_step = evidence[-1].sequence if evidence else 0
            pending = max(0, latest_step - snapshot.state.processed_step)
            counts = grounding_counts(snapshot)
            rendered_counts = ", ".join(f"{key}: {value}" for key, value in counts.items())
            return f"{brief}\n\nGrounding: {rendered_counts}.\nFreshness: {pending} steps not yet maintained."

        sections: list[str] = []
        for ref in refs:
            if ref == "goal":
                sections.append(self._goal_text())
            elif parse_claim_id(ref) is not None:
                sections.append(self._claim_text(ref))
            elif parse_evidence_id(ref) is not None:
                sections.append(self._evidence_text(ref))
            elif re.fullmatch(r"[RK]-?\d+", ref, re.IGNORECASE) is not None:
                sections.append(self._requirement_text(ref))
            else:
                sections.append(f"not found: {ref}")
        return "\n\n".join(sections)

    def _goal_text(self) -> str:
        goal = self._goal.snapshot()
        lines = ["goal", f"Description: {goal.description or '(not set)'}"]
        lines.extend(f"- [{item.tag or 'attribute'}] {item.content}" for item in goal.conclusions)
        return "\n".join(lines)

    def _claim_text(self, ref: str) -> str:
        item = self._claims.get(ref)
        if item is None:
            return f"not found: {ref}"
        evidence_by_id = {evidence_id(record.sequence): record for record in self._evidence.snapshot()}
        lines = [
            item.id,
            f"Statement: {item.statement}",
            f"Kind: {item.kind}",
            f"Grounding: {item.grounding}",
            f"Verdict: {item.verdict}",
            f"Phase: {item.phase or '(unknown)'}",
            f"Created step: {item.created_step}",
        ]
        if item.evidence_digest:
            lines.append(f"Evidence digest: {item.evidence_digest}")
        if item.evidence_refs:
            lines.append("Evidence:")
            for evidence_ref in item.evidence_refs:
                evidence = evidence_by_id.get(evidence_ref)
                lines.append(
                    f"- {evidence_ref}: {_evidence_summary(evidence)}"
                    if evidence is not None
                    else f"- {evidence_ref}: not found"
                )
        if item.supporting_claim_ids:
            lines.append("Supporting claims:")
            for child_id in item.supporting_claim_ids:
                child = self._claims.get(child_id)
                lines.append(f"- {child_id}: {child.statement}" if child is not None else f"- {child_id}: not found")
        if item.conflicts_with:
            lines.append("Conflicts with:")
            for other_id in item.conflicts_with:
                other = self._claims.get(other_id)
                lines.append(f"- {other_id}: {other.statement}" if other is not None else f"- {other_id}: not found")
        if item.superseded_by:
            lines.append(f"Superseded by: {item.superseded_by}")
        lines.append("If this is not enough, inspect the E-IDs.")
        return "\n".join(lines)

    def _requirement_text(self, ref: str) -> str:
        upper = ref.upper()
        prefix = upper[0]
        normalized = f"{prefix}{upper[1:].removeprefix('-')}"
        requirements = dict(requirement_entries(self._goal.snapshot()))
        content = requirements.get(normalized)
        if content is None:
            return f"not found: {ref}"
        coverage = self._claims.snapshot().state.requirement_coverage.get(normalized, RequirementCoverage())
        label = "Constraint" if prefix == "K" else "Requirement"
        lines = [normalized, f"{label}: {content}", f"Level: {coverage.level}", "Covered by:"]
        if not coverage.claim_ids:
            lines.append("- (none)")
        else:
            for claim_id in coverage.claim_ids:
                claim = self._claims.get(claim_id)
                lines.append(
                    f"- {claim_id}: {claim.statement} [grounding={claim.grounding}; verdict={claim.verdict}]"
                    if claim is not None
                    else f"- {claim_id}: not found"
                )
        return "\n".join(lines)

    def _evidence_text(self, ref: str) -> str:
        sequence = parse_evidence_id(ref)
        record = next((item for item in self._evidence.snapshot() if item.sequence == sequence), None)
        if record is None:
            return f"not found: {ref}"
        lines = [
            ref,
            f"Tool: {record.tool_name} ({record.tool_kind or 'unclassified'})",
            f"Phase: {record.phase or '(unknown)'}; invocation: {record.invocation_id or '(none)'}; step: {record.step}",
            f"Outcome: {record.outcome}; kind: {record.kind}",
            f"Target: {record.target or '(none)'}",
            f"Summary: {record.summary or '(none)'}",
            f"Files: {', '.join(record.files) if record.files else '(none)'}",
            f"Read range: {_read_range(record)}",
            f"Excerpt: {record.excerpt or '(empty)'}",
        ]
        raw = self._raw_path(record.raw_path)
        if raw is not None:
            lines.append(f"Raw result: {raw} (use read_file for the bounded full output)")
        refs = self._claims.claims_for_evidence(ref)
        lines.append(f"Referenced by claims: {', '.join(refs) if refs else '(none)'}")
        return "\n".join(lines)

    def _raw_path(self, relative: str) -> Path | None:
        if not relative or self._session_dir is None:
            return None
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            return None
        return self._session_dir / path


def _evidence_summary(record: EvidenceRecord) -> str:
    target = f" {record.target}" if record.target else ""
    return f"{record.tool_name}{target} — {record.summary or record.outcome}"


def _read_range(record: EvidenceRecord) -> str:
    if record.read_full:
        return "full file"
    if record.read_range is None:
        return "(none)"
    start, end = record.read_range
    return f"{start}-EOF" if end == -1 else f"{start}-{end}"
