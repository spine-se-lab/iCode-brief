"""Explain successful processing and observed file changes separately.

This projection uses only the recorded ledger: it never reads current files or
claims that elapsed time, a maintenance commit, or a reminder validates evidence.
"""

from compass.claims.models import ClaimSnapshot, evidence_id
from compass.evidence.models import EvidenceRecord
from compass.evidence.semantics import read_coverage


def evidence_status(snapshot: ClaimSnapshot, evidence: tuple[EvidenceRecord, ...]) -> str:
    state = snapshot.state
    latest = max((item.sequence for item in evidence), default=0)
    pending = tuple(item.sequence for item in evidence if item.sequence > state.processed_evidence_seq)
    recorded = evidence_id(latest) if latest else "none"
    maintained = evidence_id(state.processed_evidence_seq) if state.processed_evidence_seq else "none"
    lines = [f"Recorded through {recorded}; maintained through {maintained}; pending {len(pending)}."]
    if pending:
        lines.append(f"Pending window: {evidence_id(min(pending))}..{evidence_id(max(pending))}.")
    if not state.last_maintenance_ok:
        lines.append(
            "Last maintenance failed; pending evidence has not been marked as processed. Use Maintain to retry."
        )
    lines.append("Maintenance processes observations into Claims; it does not re-run tests or re-read files.")
    reads = read_coverage(evidence)
    labels = {
        "changed": "changed after read; re-read before relying on it",
        "unchanged": "no recorded changes since read",
        "unknown": "unknown after a possible untracked write; re-read before relying on it",
    }
    lines.append("\nFile-read freshness (recorded tools only)")
    if not reads:
        lines.append("No file-read freshness available: no confirmed file reads recorded.")
    lines.extend(f"{read.path}: {labels[read.freshness]} (read at {evidence_id(read.sequence)})" for read in reads[:8])
    if len(reads) > 8:
        lines.append(f"{len(reads) - 8} older file entries omitted.")
    stale = tuple(item.id for item in snapshot.claims if item.verdict == "stale")
    if stale:
        shown = ", ".join(stale[:8]) + (f", … (+{len(stale) - 8})" if len(stale) > 8 else "")
        lines.append(f"\nClaims marked stale: {shown}. Inspect their details before relying on them.")
    lines.append("\nNo recorded change is not proof of current validity; external changes are not monitored.")
    return "\n".join(lines)
