"""Incremental Requirement-Claim links and evidence-backed retractions."""

from collections.abc import Mapping
from dataclasses import dataclass

from compass_brief.claims.models import WEAK_COVERAGE_LEVELS, ClaimRecord, RequirementCoverage


def is_weak_coverage(coverage: RequirementCoverage | None, claims: Mapping[str, ClaimRecord]) -> bool:
    """Reminder eligibility, separate from the historical provenance label.

    Callers supply active claims. A supported test claim is evidence only for its
    stated scope, not proof that the requirement is satisfied; explicit conflicts
    remain a separate review signal. Unknown, refuted and stale claims cannot
    suppress a reminder merely because they once cited a test.
    """
    if coverage is None or coverage.level in WEAK_COVERAGE_LEVELS:
        return True
    return not any(
        (claim := claims.get(claim_id)) is not None
        and claim.kind != "activity"
        and not claim.assumes_external_provision
        and claim.grounding in {"new_test", "repo_test"}
        and claim.verdict == "supported"
        for claim_id in coverage.claim_ids
    )


@dataclass(frozen=True, slots=True)
class CoverageRetraction:
    requirement_id: str
    claim_id: str
    reason: str
    evidence_refs: tuple[str, ...]


def retract_links(
    links: Mapping[str, tuple[str, ...]],
    retractions: tuple[CoverageRetraction, ...],
    *,
    requirement_ids: frozenset[str],
    evidence_ids: frozenset[str],
    added_pairs: set[tuple[str, str]],
) -> tuple[dict[str, tuple[str, ...]], tuple[CoverageRetraction, ...]]:
    """Only explicit, justified removals may subtract an existing link.

    Evidence IDs must belong to this maintenance window. This validates the
    provenance, not the semantic truth of the model's explanation. Conflicting
    add/remove instructions fail closed by retaining the link.
    """
    result = dict(links)
    accepted = []
    for item in retractions:
        existing = result.get(item.requirement_id, ())
        if (
            item.requirement_id not in requirement_ids
            or item.claim_id not in existing
            or (item.requirement_id, item.claim_id) in added_pairs
            or not item.reason.strip()
            or not item.evidence_refs
            or not set(item.evidence_refs).issubset(evidence_ids)
        ):
            continue
        result[item.requirement_id] = tuple(value for value in existing if value != item.claim_id)
        accepted.append(item)
    return result, tuple(accepted)
