"""Map existing core projections to complete entries and full-width detail text."""

import re

from rich.text import Text

from compass.briefing.evidence import render_previously_read
from compass.briefing.freshness import evidence_status
from compass.briefing.policy import weak_requirements
from compass.briefing.render import attention_lines, tension_lines
from compass.claims.models import ClaimSnapshot
from compass.task_contract.extraction import requirement_entries

from .details import DetailEntry

_GROUNDING = {
    "repo": "code-read",
    "repo_test": "repo-test",
    "new_test": "session-test",
    "self_check": "self-check",
    "agent_only": "agent-only",
    "issue": "issue",
}


def _entry(identity, title, summary, body, meta="", links=()):
    return DetailEntry(
        identity,
        title,
        summary,
        body.plain,
        meta,
        links,
        emphasis=tuple((span.start, span.end) for span in body.spans if span.style == "bold"),
    )


def _section_body(key, line):
    text = Text(line)
    if key == "already-read":
        text.append(
            "\n\nCoverage counts distinct files with confirmed read ranges, not read calls or all file access. "
            "Search hits, test execution and unrecognized shell reads do not prove full-file coverage. "
            "This list and the model Brief include all tracked files."
        )
    if key == "freshness":
        # Only the known core status labels, not arbitrary Evidence/user text.
        text.highlight_regex(
            r"(?m)^(?:Recorded through|Pending window:|Claims marked stale:|"
            r"File-read freshness \(recorded tools only\)$)|(?<=; )(?:maintained through|pending)(?= )",
            style="bold",
        )
    return text


def coverage_summary(coverage, *, compact=False):
    if coverage is None or not coverage.claim_ids:
        return "No related claims recorded"
    refs = ", ".join(coverage.claim_ids) or "no associated Claims"
    return f"Related claims: {refs}"


def brief_entries(goal, claims, evidence):
    """Read only. No retrieval, model calls, or reconstructed domain decisions."""
    groups = {}
    lookup = {}
    state = claims.state
    current_step = max((item.sequence for item in evidence), default=0)
    active = {item.id: item for item in claims.claims if item.id in state.active_ids}
    tensions = tension_lines(active, claims.conflict_notes)
    coverage = state.requirement_coverage if state else {}
    contracts = requirement_entries(goal)
    contract_details = dict(
        zip(
            (identity for identity, _ in contracts),
            (item for kind in ("criteria", "constraint") for item in goal.conclusions if item.tag == kind),
            strict=True,
        )
    )
    reminders = (
        {
            identity: f"{severity} · weak for {age} evidence steps"
            for age, identity, severity, _ in weak_requirements(
                ClaimSnapshot(state=state, claims=claims.claims), contracts, current_step=current_step
            )
        }
        if state
        else {}
    )
    goal_body = Text(goal.description)
    groups["goal"] = (_entry("goal", "Goal", goal.description, goal_body),) if goal_body else ()
    for key, prefix in (("requirements", "R"), ("constraints", "K")):
        entries = []
        for identity, content in contracts:
            if not identity.startswith(prefix):
                continue
            item = coverage.get(identity)
            refs = item.claim_ids if item else ()
            body = Text().append("Requirement" if prefix == "R" else "Constraint", style="bold")
            body.append("\n  " + content + "\n\n", style="none").append("Associated Claims", style="bold")
            body.append("\n  " + (", ".join(refs) or "No related claims recorded"))
            original = contract_details[identity]
            if original.evidence_refs:
                body.append("\n").append("Evidence:", style="bold")
                body.append(" " + ", ".join(f"E-{ref:03}" for ref in original.evidence_refs))
            if identity in reminders:
                body.append("\n\n").append("Weak coverage", style="bold").append(f"\n{reminders[identity]}")
            if state:
                related_tensions = [line for line in tensions if identity in re.findall(r"\b[RK]\d+\b", line)]
                if related_tensions:
                    body.append("\n\n").append("Tensions", style="bold").append("\n" + "\n".join(related_tensions))
            entries.append(_entry(identity, identity, content, body, coverage_summary(item, compact=True), refs))
        groups[key] = tuple(entries)
    for item in claims.claims:
        linked = tuple(identity for identity, row in coverage.items() if item.id in row.claim_ids)
        body = Text().append("Claim", style="bold").append("\n  " + item.statement + "\n\n", style="none")
        for separator, label, value in (
            ("", "Source", _GROUNDING[item.grounding]),
            ("    ", "Verdict", item.verdict),
            ("\n", "Kind", item.kind),
            ("    ", "Phase", item.phase or "(unknown)"),
            ("    ", "Step", str(item.created_step)),
            ("\n", "Assumes external provision", str(item.assumes_external_provision)),
        ):
            body.append(separator).append(label + ":", style="bold").append(" " + value)
        body.append("\n\n").append("Evidence", style="bold")
        body.append(f"\n{', '.join(item.evidence_refs) or 'No references'}")
        body.append(f"\n{item.evidence_digest or 'No evidence digest recorded.'}")
        body.append("\n\n").append("Associated Requirements / Constraints", style="bold")
        body.append(f"\n{', '.join(linked) or 'No recorded associations'}")
        for label, values in (
            ("Merged from", item.supporting_claim_ids),
            ("Conflicts", item.conflicts_with),
            ("Superseded by", (item.superseded_by,) if item.superseded_by else ()),
        ):
            if values:
                body.append("\n\n").append(label, style="bold").append(f"\n{', '.join(values)}")
        links = tuple(
            dict.fromkeys(
                (
                    *linked,
                    *item.evidence_refs,
                    *item.supporting_claim_ids,
                    *item.conflicts_with,
                    *((item.superseded_by,) if item.superseded_by else ()),
                )
            )
        )
        title = item.id + (" · tension" if item.conflicts_with else "")
        if item.verdict in {"contradicted", "stale"}:
            title += f" · {item.verdict}"
        lookup[item.id] = _entry(
            item.id, title, item.statement, body, f"{_GROUNDING[item.grounding]} · {item.verdict}", links
        )
    groups["claims"] = tuple(lookup[item.id] for item in active.values())
    sections = {
        "tensions": (tensions, "Tension"),
        "already-read": (render_previously_read(evidence, max_files=None), "Read coverage"),
        "attention": (attention_lines(claims, contracts, current_step=current_step), "Weak coverage"),
        "freshness": ((evidence_status(claims, evidence),), "Evidence status"),
    }
    for key, (lines, title) in sections.items():
        groups[key] = tuple(
            _entry(
                f"{key}:{index}",
                "Processing progress" if key == "freshness" else f"{title} {index + 1}",
                line.splitlines()[0] if key == "freshness" else line,
                _section_body(key, line),
                links=tuple(dict.fromkeys(re.findall(r"\b(?:C-\d{3,}|[RK]\d+)\b", line))),
            )
            for index, line in enumerate(lines)
        )
    # Evidence stays out of the module navigation; Claim details can navigate to it.
    for record in evidence:
        identity = f"E-{record.sequence:03d}"
        body = Text().append("Evidence", style="bold").append("\n  " + record.summary, style="none")
        for label, value in (
            ("Tool", record.tool_name),
            ("Outcome", record.outcome),
            ("Target", record.target),
            ("Recorded output", record.excerpt),
        ):
            body.append("\n\n").append(label, style="bold").append("\n  " + value, style="none")
        lookup[identity] = _entry(identity, identity, record.summary, body)
    for entries in groups.values():
        lookup.update((entry.id, entry) for entry in entries)
    return groups, lookup
