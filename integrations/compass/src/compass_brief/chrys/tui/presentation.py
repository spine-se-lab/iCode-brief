"""Map existing core projections to compact entries and full-width detail text."""

import re

from rich.text import Text

from compass_brief.briefing.policy import weak_requirements
from compass_brief.chrys.tui.details import DetailEntry
from compass_brief.claims.models import ClaimSnapshot
from compass_brief.task_contract.extraction import requirement_entries

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
        emphasis=tuple((span.start, span.end) for span in body.spans),
    )


def _section_body(key, line):
    text = Text(line)
    if key == "already-read":
        text.append(
            "\n\nCoverage counts distinct files with confirmed read ranges, not read calls or all file access. "
            "Search hits, test execution and unrecognized shell reads do not prove full-file coverage. "
            "This list includes all tracked files; the model Brief may show a smaller budgeted subset."
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
    if coverage is None:
        return "not assessed"
    ids = coverage.claim_ids
    shown = ids[:3] if compact else ids
    refs = ", ".join(shown) or "no associated Claims"
    if len(shown) < len(ids):
        refs += f", … (+{len(ids) - len(shown)})"
    return f"{_GROUNDING.get(coverage.level, coverage.level)} · {refs}"


def brief_entries(goal, claims, brief):
    """Read only. No retrieval, model calls, or reconstructed domain decisions."""
    groups = {}
    lookup = {}
    state = claims.state
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
                ClaimSnapshot(state=state, claims=claims.items), contracts, current_step=claims.current_step
            )
        }
        if state
        else {}
    )
    goal_body = Text(goal.description)
    for item in goal.conclusions:
        goal_body.append("\n\n").append(item.tag or "Note", style="bold")
        goal_body.append(f" · {item.status}\n{item.content}")
        if item.evidence_refs:
            goal_body.append("\n").append("Evidence:", style="bold")
            goal_body.append(" " + ", ".join(f"E-{ref:03}" for ref in item.evidence_refs))
    groups["goal"] = (_entry("goal", "Goal", goal.description, goal_body),) if goal_body else ()
    for key, prefix in (("requirements", "R"), ("constraints", "K")):
        entries = []
        for identity, content in contracts:
            if not identity.startswith(prefix):
                continue
            item = coverage.get(identity)
            refs = item.claim_ids if item else ()
            body = Text(content + "\n\n").append("Associated Claims", style="bold")
            body.append(f"\n{coverage_summary(item)}\n\nAssociation means relevance, not satisfaction.")
            original = contract_details[identity]
            body.append("\n\n").append("Task contract status:", style="bold").append(f" {original.status}")
            if original.evidence_refs:
                body.append("\n").append("Evidence:", style="bold")
                body.append(" " + ", ".join(f"E-{ref:03}" for ref in original.evidence_refs))
            if identity in reminders:
                body.append("\n\n").append("Weak coverage", style="bold").append(f"\n{reminders[identity]}")
            if state:
                tensions = [line for line in brief.tensions if identity in re.findall(r"\b[RK]\d+\b", line)]
                if tensions:
                    body.append("\n\n").append("Tensions", style="bold").append("\n" + "\n".join(tensions))
            entries.append(_entry(identity, identity, content, body, coverage_summary(item, compact=True), refs))
        groups[key] = tuple(entries)
    for item in claims.items:
        linked = tuple(identity for identity, row in coverage.items() if item.id in row.claim_ids)
        body = Text(item.statement + "\n\n")
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
    groups["claims"] = tuple(lookup[item.id] for item in claims.active_items)
    sections = {
        "tensions": (brief.tensions, "Tension"),
        "already-read": (brief.already_read, "Read coverage"),
        "attention": (brief.attention, "Weak coverage"),
        "freshness": ((brief.freshness,) if brief.freshness else (), "Evidence status"),
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
    for entries in groups.values():
        lookup.update((entry.id, entry) for entry in entries)
    return groups, lookup
