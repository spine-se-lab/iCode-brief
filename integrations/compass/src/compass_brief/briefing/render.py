# Copyright (c) 2026 Chrys. All rights reserved.

"""Mechanical rendering of the active Goal & Claims Brief."""

from __future__ import annotations

import re
from itertools import zip_longest
from typing import Final

from compass_brief.briefing.evidence import render_previously_read
from compass_brief.briefing.policy import weak_requirements
from compass_brief.claims.coverage import is_weak_coverage
from compass_brief.claims.models import (
    ClaimGrounding,
    ClaimRecord,
    ClaimSnapshot,
    RequirementCoverage,
    parse_claim_id,
    parse_evidence_id,
    statement_excerpt,
)
from compass_brief.evidence.actions import is_test_attempt
from compass_brief.evidence.models import EvidenceRecord, evidence_id
from compass_brief.task_contract.extraction import requirement_entries
from compass_brief.task_contract.models import GoalState

_GROUNDING_ORDER: Final[dict[ClaimGrounding, int]] = {
    "repo_test": 0,
    "new_test": 1,
    "repo": 2,
    "agent_only": 3,
    "self_check": 4,
    "issue": 5,
}
_COVERAGE_LABEL: Final[dict[str, str]] = {
    "none": "no-evidence",
    "stated": "claimed",
    "self_check": "self-check",
    "repo": "code-read",
    "new_test": "session-test",
    "repo_test": "repo-tested",
}
_RUNTIME_GOAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bCurrently at\b[^.;\n]*[.;]?|\bStep\s+\d+\b[^.;\n]*[.;]?)",
    re.IGNORECASE,
)
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"`([^`\n]{1,60})`")
_MAX_KEPT_IDENTIFIER_CHARS: Final[int] = 160
_REQUIREMENT_LABEL_CHARS: Final[int] = 80
_CLAIM_TEXT_CHARS: Final[int] = 160
_FULL_CHARS: Final[int] = 1_000_000  # V1.9: full text until the budget forces the old step-downs
_REQUIREMENT_CONFLICTS_SHOWN: Final[int] = 3


#: The detail entry the Brief names by default. A host whose tools are reached
#: differently passes its own; the Brief must never tell a model to call
#: something that host cannot execute.
CANONICAL_DETAIL_ENTRY: Final = "inspect_compass_brief"



def render_active_brief(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    current_step: int | None = None,
    phase: str = "",
    repo_path: str = "",
    max_already_read: int = 4,
    brief_budget_chars: int = 2500,
    detail_entry: str = CANONICAL_DETAIL_ENTRY,
    detail_renderer=None,
) -> str:
    """Render a deterministic active projection within the configured hard budget."""
    brief, _actions = render_active_brief_with_actions(
        snapshot,
        goal,
        evidence,
        current_step=current_step,
        phase=phase,
        repo_path=repo_path,
        max_already_read=max_already_read,
        brief_budget_chars=brief_budget_chars,
        detail_entry=detail_entry,
        detail_renderer=detail_renderer,
    )
    return brief


def _render_v19_with_actions(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    current_step: int | None = None,
    phase: str = "",
    repo_path: str = "",
    max_already_read: int = 4,
    brief_budget_chars: int = 2500,
    detail_entry: str = CANONICAL_DETAIL_ENTRY,
    detail_renderer=None,
) -> tuple[str, tuple[str, ...]]:
    """Render the Brief and report every deterministic budget action applied."""
    budget = max(1, brief_budget_chars)
    canonical_budget = budget
    while True:
        reference = _reference_contract(snapshot, goal, budget=canonical_budget * 2 // 5) if canonical_budget >= 1400 else ""
        focus = (
            _verification_focus(snapshot, goal, evidence, budget=canonical_budget // 3)
            if canonical_budget >= 1400 else ""
        )
        brief, actions = _render_canonical_brief(
            snapshot,
            goal,
            evidence,
            current_step=current_step,
            phase=phase,
            repo_path=repo_path,
            max_already_read=max_already_read,
            brief_budget_chars=canonical_budget - len(focus) - (2 if focus else 0) - len(reference) - (2 if reference else 0),
        )
        if reference:
            brief = reference + "\n\n" + brief
        if focus:
            brief += "\n\n" + focus
        rendered = detail_renderer(brief) if detail_renderer else brief.replace(CANONICAL_DETAIL_ENTRY, detail_entry)
        if len(rendered) <= budget:
            return rendered, actions
        # Re-render whole entries instead of cutting a Claim or host command.
        # The one-character omission marker contains no entry and always fits.
        canonical_budget = max(1, canonical_budget - (len(rendered) - budget))



def _reference_contract(snapshot: ClaimSnapshot, goal: GoalState, *, budget: int) -> str:
    """Prioritize derived clarifications, without promoting them to verified facts."""
    conclusions = [c for tag in ("criteria", "constraint") for c in goal.conclusions if c.tag == tag]
    entries = [entry for entry, conclusion in zip(requirement_entries(goal), conclusions, strict=True)
               if conclusion.reference_derived]
    if not entries:
        return ""
    offset = snapshot.state.revision % len(entries)
    entries = entries[offset:] + entries[:offset]
    lines = ["Priority contract clarifications — check against issue/source; not verification:"]
    for identity, content in entries:
        line = f"- {identity}: {content}"
        if len("\n".join([*lines, line])) > budget:
            continue  # Keep full clauses; the canonical index still exposes every ID.
        lines.append(line)
        if len(lines) == 4:
            break
    return "\n".join(lines) if len(lines) > 1 else ""


def _verification_focus(
    snapshot: ClaimSnapshot, goal: GoalState, evidence: tuple[EvidenceRecord, ...], *, budget: int
) -> str:
    """Questions derived from live references, never new findings or coverage credit."""
    active = {
        item.id: item
        for item in snapshot.claims
        if item.id in snapshot.state.active_ids and item.verdict != "stale" and not item.superseded_by
    }
    requirements = requirement_entries(goal)
    behavior_claims = {
        ref
        for identity, _ in requirements if identity.startswith("R")
        for ref in snapshot.state.requirement_coverage.get(identity, RequirementCoverage()).claim_ids
    }
    failed = {evidence_id(item.sequence): item for item in evidence if item.outcome == "error"}
    # Rank existing facts, not inferred defects. A nonzero grep exit is not a
    # failed behavior check; require a test attempt, or a reproduction/self-check
    # already associated with a behavior requirement. Later references alone do
    # not establish that the original error was resolved for the same input.
    candidates: list[tuple[int, str, str, str]] = []
    for item in sorted(active.values(), key=lambda item: item.created_step):
        refs = [ref for ref in item.evidence_refs if ref in failed and (
            failed[ref].kind == "test" or is_test_attempt(failed[ref].target)
            or (item.id in behavior_claims and failed[ref].action_class in {"reproduce", "self_script"})
        )]
        if item.kind == "observation" and refs:
            candidates.append((0, ", ".join((item.id, *refs[:2])), item.statement,
                               "Which same-scope check resolves the referenced error?"))
            break  # Leave room for conflicts and separate contract checks.
    for tension in tension_lines(active, snapshot.conflict_notes, unresolved_only=True):
        refs, _, note = tension.removeprefix("- ").partition(": ")
        candidates.append((4 if refs.startswith("K") else 1, refs, note,
                           "Which check resolves these conflicting scopes?"))
    # Coverage/age warnings already live in the canonical Brief. Reusing the
    # committed revision here lets the bounded review window visit other clauses
    # instead of repeatedly asking about the same recently touched requirements.
    # K entries can contain independently binding public shapes/invariants.
    # Rotate the entire contract; giving every R a higher rank starves all K.
    contract = [
        entry
        for pair in zip_longest(
            [entry for entry in requirements if entry[0].startswith("R")],
            [entry for entry in requirements if entry[0].startswith("K")],
        )
        for entry in pair if entry is not None
    ]
    if contract:
        offset = snapshot.state.revision % len(contract)
        contract = contract[offset:] + contract[:offset]
    for identity, content in contract:
        coverage = snapshot.state.requirement_coverage.get(identity, RequirementCoverage())
        links = [ref for ref in coverage.claim_ids if ref in active][-2:]
        refs = ", ".join((identity, *links))
        if identity.startswith("K"):
            question = "Which action checks this constraint?"
        elif coverage.level in {"new_test", "repo_test", "self_check"}:
            question = "Which caller/input exposes an assumption shared by implementation and tests?"
        else:
            question = "Which public entry points or clauses remain unchecked?"
        candidates.append((3, refs, content, question))
    if not candidates:
        return ""
    lines = ["Verification focus — questions, not findings; excerpts: inspect IDs for full context:"]
    row_budget = (budget - len(lines[0]) - 3) // 3
    ordered = sorted(candidates, key=lambda item: item[0])
    contract_shown = False
    for priority, refs, content, question in ordered:
        # Multiple unresolved conflicts must not consume every slot forever.
        # Keep one slot for the rotating contract without clearing any conflict.
        if len(lines) == 3 and contract and not contract_shown and priority != 3:
            continue
        prefix, suffix = f"- {refs}: ", f" — {question}"
        text_budget = row_budget - len(prefix) - len(suffix)
        if text_budget < 20:
            continue
        lines.append(prefix + statement_excerpt(" ".join(content.split()), text_budget) + suffix)
        contract_shown |= priority == 3
        if len(lines) == 4:
            break
    return "\n".join(lines) if len(lines) > 1 else ""


def _render_canonical_brief(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    current_step: int | None,
    phase: str,
    repo_path: str,
    max_already_read: int,
    brief_budget_chars: int,
) -> tuple[str, tuple[str, ...]]:
    budget = max(1, brief_budget_chars)
    latest_evidence_step = evidence[-1].sequence if evidence else 0
    effective_current_step = max(
        snapshot.state.processed_step,
        current_step if current_step is not None else latest_evidence_step,
    )
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    shown = [
        active[item] for item in snapshot.state.active_ids
        if item in active and active[item].kind != "activity"
        and active[item].verdict != "stale" and not active[item].superseded_by
    ]
    # A grep miss or shell/poll timeout is not by itself an unresolved behavior failure.
    # Preserve old failed checks ahead of routine newer observations; age alone cannot resolve them.
    failed_refs = {
        evidence_id(item.sequence)
        for item in evidence
        if item.outcome == "error" and (item.kind == "test" or is_test_attempt(item.target))
    }
    shown.sort(key=lambda item: _claim_sort_key(item, failed_refs))
    actions: list[str] = []
    all_tensions = tension_lines(active, snapshot.conflict_notes)
    tension_count = len(all_tensions)
    already_read_limit = min(4, max(0, max_already_read))
    if max_already_read > already_read_limit:
        actions.append("already_read_4")

    # Keep a small window of concrete observations/risks while shortening prose.
    # Otherwise a long contract can consume the entire Brief and hide every fact.
    claim_limit = len(shown)
    priority_claims = [item for item in shown if _priority_claim(item)]
    claim_floor = min(3, len(priority_claims))
    requirement_chars = _FULL_CHARS
    tension_chars = _FULL_CHARS

    def _render(claims: int) -> str:
        return _render_brief(
            snapshot,
            goal,
            evidence,
            active=active,
            shown=shown,
            claim_limit=claims,
            current_step=effective_current_step,
            phase=phase,
            repo_path=repo_path,
            max_already_read=already_read_limit,
            requirement_chars=requirement_chars,
            tension_chars=tension_chars,
        )

    brief = _render(claim_limit)
    while len(brief) > budget and already_read_limit > 0:
        already_read_limit -= 1
        actions.append(f"already_read_{already_read_limit}")
        brief = _render(claim_limit)
    while len(brief) > budget and claim_limit > claim_floor:
        claim_limit -= 1
        brief = _render(claim_limit)
    while len(brief) > budget and requirement_chars > 64:
        requirement_chars = max(64, min(requirement_chars, _REQUIREMENT_LABEL_CHARS) - 16)
        actions.append(f"requirement_chars_{requirement_chars}")
        brief = _render(claim_limit)
    while len(brief) > budget and tension_chars > 48:
        tension_chars = max(48, min(tension_chars, 180) - 16)
        actions.append(f"tension_chars_{tension_chars}")
        brief = _render(claim_limit)
    if len(brief) > budget:
        actions.append("brief_compacted")
        priority_lines: list[str] = []
        priority_budget = budget // 2 if budget >= 700 else 0
        for item in priority_claims:
            candidate = [*priority_lines, _claim_line(item, statement_chars=_CLAIM_TEXT_CHARS)]
            if len("\n".join(candidate)) + 100 > priority_budget:
                continue  # Omit this whole row, not every later row that would fit.
            priority_lines = candidate
        claim_limit = len(priority_lines)
        priority_block = (
            f"\nClaims ({len(active)} active, {claim_limit} shown; remaining details: inspect_compass_brief):\n"
            + "\n".join(priority_lines)
            if priority_lines
            else ""
        )
        if not priority_block and active and budget >= 300:
            priority_block = f"\nClaims ({len(active)} active, 0 shown; details omitted: inspect_compass_brief)."
        brief = _minimal_brief(
            snapshot,
            goal,
            active=active,
            current_step=effective_current_step,
            budget=budget - len(priority_block),
        ) + priority_block
    # Report the final trimmed count, including zero, rather than intermediate limits.
    if claim_limit < len(shown):
        actions.append(f"claims_{claim_limit}")
    delivered_tensions = _delivered_tension_count(brief, all_tensions)
    actions.extend(
        (
            f"tensions_rendered_{delivered_tensions}",
            f"tensions_suppressed_{max(0, tension_count - delivered_tensions)}",
        )
    )
    return brief, tuple(dict.fromkeys(actions))


def _render_brief(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    active: dict[str, ClaimRecord],
    shown: list[ClaimRecord],
    claim_limit: int,
    current_step: int | None,
    phase: str,
    repo_path: str,
    max_already_read: int,
    requirement_chars: int,
    tension_chars: int,
) -> str:
    visible_claims = shown[:claim_limit]
    requirements = requirement_entries(goal)
    description = _RUNTIME_GOAL_RE.sub("", goal.description).strip(" ;-:") or "(not set)"
    lines: list[str] = []
    if snapshot.state.maintenance_degraded_after_step is not None:
        lines.append(
            f"[maintenance degraded: last maintained through step {snapshot.state.processed_step}; "
            "later evidence is pending. Earlier claims are not current guarantees; unverified decisions remain assumptions.]"
        )
    lines.extend(
        [
            "[Goal & Claims — Reminder]",
            "Compiled only from evidence already recorded in this session. It adds no new facts.",
            (
                "Use the latest reminder. Call inspect_compass_brief with a C-ID, R-ID, or K-ID before relying on weak or "
                "conflicting claims."
            ),
            "",
            f"Goal: {description}",
        ]
    )
    lines.extend(_contract_warning(goal))
    top_conflicts = requirement_conflicts(active, snapshot.conflict_notes)
    if top_conflicts:
        lines.extend(("", *_requirement_conflict_block(top_conflicts)))
    _append_weak_escalation(
        lines,
        snapshot,
        requirements,
        current_step=current_step,
    )
    behavior_requirements = tuple(item for item in requirements if item[0].startswith("R"))
    constraints = tuple(item for item in requirements if item[0].startswith("K"))
    _append_requirement_section(
        lines,
        snapshot,
        title='Requirements (issue text is in your prompt; full text: inspect_compass_brief(["R2"])):',
        items=behavior_requirements,
        content_chars=requirement_chars,
    )
    _append_requirement_section(
        lines,
        snapshot,
        title='Constraints (independently binding; full text: inspect_compass_brief(["K1"])):',
        items=constraints,
        content_chars=requirement_chars,
    )
    if requirements:
        lines.extend(
            (
                "",
                (
                    'Requirements and constraints marked "!" have no strong evidence yet; "?" means code-read, '
                    "a newly-added test, or test claims without current support. Reading code does not show the behavior holds: until a test "
                    "exercises it, a requirement stays open. Re-check each "
                    'before finishing, covering every clause (e.g. both "provided" and "changed").'
                ),
            )
        )

    if any(item.kind == "stated" and item.grounding == "agent_only" for item in shown):
        lines.extend(
            (
                "",
                (
                    "Unverified assumptions carried from earlier phases are marked grounding=agent-only below. "
                    "Check the stated condition; a changed test input or expectation does not resolve the original gap. "
                    "Use inspect_compass_brief by C-ID for details."
                ),
            )
        )

    lines.extend(("", f"Claims ({len(active)} active, {len(visible_claims)} shown):"))
    if visible_claims:
        lines.extend(_claim_line(item, statement_chars=_FULL_CHARS) for item in visible_claims)
        if len(shown) > len(visible_claims):
            lines.append(f"- +{len(shown) - len(visible_claims)} more (inspect_compass_brief([]))")
    elif active:
        lines.append("- Active claims omitted from this view; inspect_compass_brief([]) returns their details.")
    else:
        lines.append("- No maintained claims yet.")

    conflict_lines = [item for item in tension_lines(active, snapshot.conflict_notes) if item not in top_conflicts]
    if conflict_lines:
        lines.extend(
            (
                "",
                "Tensions (judge before acting):",
                *(_truncate_tension_line(item, tension_chars) for item in conflict_lines),
            )
        )

    already_read = render_previously_read(evidence, root=repo_path, max_files=max_already_read)
    if already_read:
        lines.extend(("", "Already read:", *already_read))

    lines.append(evidence_freshness(snapshot, evidence, current_step=current_step))
    return "\n".join(lines).strip()


def evidence_freshness(snapshot: ClaimSnapshot, evidence: tuple[EvidenceRecord, ...], *, current_step=None) -> str:
    """Compact model reminder position, not file validity or sidebar status."""
    latest_sequence = evidence[-1].sequence if evidence else 0
    start_sequence = snapshot.state.processed_evidence_seq + 1
    recorded = "none"
    if latest_sequence:
        recorded = (
            evidence_id(latest_sequence)
            if start_sequence > latest_sequence
            else f"{evidence_id(start_sequence)}..{evidence_id(latest_sequence)}"
        )
    step = current_step if current_step is not None else latest_sequence
    since = max(0, step - snapshot.state.last_brief_step)
    return f"Evidence recorded: {recorded}; Steps since last reminder: {since}."


def _append_requirement_section(
    lines: list[str],
    snapshot: ClaimSnapshot,
    *,
    title: str,
    items: tuple[tuple[str, str], ...],
    content_chars: int,
) -> None:
    if not items:
        return
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    lines.extend(("", title))
    if title.startswith("Requirements"):
        lines.append(
            "  Label = strongest evidence found so far, not proof of satisfaction. [names] are literal "
            "contract tokens an acceptance check compares verbatim."
        )
    for requirement_id, content in items:
        coverage = snapshot.state.requirement_coverage.get(requirement_id, RequirementCoverage())
        marker = (
            "! "
            if coverage.level in {"none", "self_check", "stated"}
            else "? "
            if coverage.level in {"repo", "new_test"} or is_weak_coverage(coverage, active)
            else "  "
        )
        level = _COVERAGE_LABEL.get(coverage.level, coverage.level)
        identifiers = f" {', '.join(coverage.claim_ids)}" if coverage.claim_ids else ""
        lines.append(
            f"  {marker}{requirement_id} {level:<12}{identifiers} — {_requirement_digest(content, content_chars)}"
        )


def _append_weak_escalation(
    lines: list[str],
    snapshot: ClaimSnapshot,
    requirements: tuple[tuple[str, str], ...],
    *,
    current_step: int | None,
) -> None:
    reminders = attention_lines(snapshot, requirements, current_step=current_step)
    if not reminders:
        return
    lines.extend(("", "OPEN LOOPS — weak evidence has remained unresolved:"))
    lines.extend(reminders)


def attention_lines(snapshot, requirements, *, current_step=None, minimum_age=24) -> list[str]:
    """The same weak-coverage reminders for model context and sidebar groups."""
    overdue = weak_requirements(snapshot, requirements, current_step=current_step, minimum_age=minimum_age)
    lines = []
    for age, requirement_id, severity, content in overdue:
        lines.append(
            f"- [{severity}] {requirement_id} has stayed weak for {age} steps — {_requirement_digest(content, 96)}"
        )
    return lines


def _priority_claim(item: ClaimRecord) -> bool:
    return item.verdict != "stale" and (
        item.kind in {"observation", "stated"} or item.verdict == "contradicted" or bool(item.conflicts_with)
    )


def _claim_sort_key(item: ClaimRecord, failed_refs: set[str]) -> tuple[int, ...]:
    # Only failed checks raise visibility, not every failed shell operation.
    latest_ref = max(item.evidence_refs, key=lambda ref: parse_evidence_id(ref) or 0, default="")
    risk = item.verdict == "contradicted" or bool(item.conflicts_with) or latest_ref in failed_refs
    return (
        item.verdict == "stale",
        not _priority_claim(item),
        not (item.kind == "stated" and item.verdict == "unverified"),
        not risk,
        -(parse_evidence_id(latest_ref) or 0),
        -item.created_step,
        _GROUNDING_ORDER[item.grounding],
        -(parse_claim_id(item.id) or 0),
    )


def _claim_line(item: ClaimRecord, *, statement_chars: int) -> str:
    grounding = {
        "agent_only": "agent-only",
        "self_check": "self-check",
        "new_test": "session-test",
        "repo_test": "repo-test",
        "issue": "issue",
        "repo": "code-read",
    }[item.grounding]
    status = f" [grounding={grounding} · verdict={item.verdict}]"
    kind = " [stated]" if item.kind == "stated" else ""
    if item.evidence_refs:
        provenance = ", ".join(item.evidence_refs[-3:])
        if len(item.evidence_refs) > 3:
            provenance += f"; +{len(item.evidence_refs) - 3} refs"
    elif item.supporting_claim_ids:
        provenance = f"merged from {', '.join(item.supporting_claim_ids[:3])}"
        if len(item.supporting_claim_ids) > 3:
            provenance += f"; +{len(item.supporting_claim_ids) - 3} claims"
    else:
        provenance = "no tool evidence"
    # A linked ID is not delivery of the decision. Keep the stored decision whole;
    # the outer budget loop drops whole rows and reports omitted details.
    text = " ".join(item.statement.split())
    if item.kind != "stated":
        text = statement_excerpt(text, statement_chars)
    return f"- {item.id}{status}{kind} {text} ({provenance})"


def tension_lines(
    active: dict[str, ClaimRecord],
    notes: tuple[tuple[str, str, str], ...],
    *,
    unresolved_only: bool = False,
) -> list[str]:
    """Render tensions and resolution history, or only unresolved review work.

    Computing it from all active claims is what keeps a conflict visible when
    neither of its two claims made the shown window.
    """
    note_map = {frozenset((a, b)): note for a, b, note in notes}
    seen: set[frozenset[str]] = set()
    lines: list[str] = []
    for item in active.values():
        for other_id in item.conflicts_with:
            pair = frozenset((item.id, other_id))
            if pair in seen or other_id not in active:
                continue
            seen.add(pair)
            other = active[other_id]
            note = note_map.get(pair) or f"{item.statement} ↔ {other.statement}"
            if not note.startswith("resolved: ") and any(
                claim.verdict == "stale" or claim.superseded_by for claim in (item, other)
            ):
                continue
            if unresolved_only and note.startswith("resolved: "):
                continue
            lines.append(f"- {item.id} ↔ {other_id}: {note}")
    for a, b, note in notes:
        if unresolved_only and note.startswith("resolved: "):
            continue
        if re.fullmatch(r"[RK]\d+", a):
            requirement_id, claim_id = a, b
        elif re.fullmatch(r"[RK]\d+", b):
            requirement_id, claim_id = b, a
        else:
            continue
        pair = frozenset((requirement_id, claim_id))
        if pair in seen or (claim_id not in active and not note.startswith("resolved: ")):
            continue
        claim = active.get(claim_id)
        if not note.startswith("resolved: ") and claim is not None and (
            claim.verdict == "stale" or claim.superseded_by
        ):
            continue
        seen.add(pair)
        lines.append(f"- {requirement_id} ↔ {claim_id}: {note}")
    return lines


def requirement_conflicts(active: dict[str, ClaimRecord], notes: tuple[tuple[str, str, str], ...]) -> list[str]:
    """The newest unresolved requirement ↔ claim tensions, shown whole at the top of every Brief."""
    lines = [line for line in tension_lines(active, notes, unresolved_only=True) if line[2:3] in {"R", "K"}]
    return lines[-_REQUIREMENT_CONFLICTS_SHOWN:]


CONFLICT_REVIEW_GUIDANCE = (
    "If the wording allows two readings, check both on the same public object, conditions and observable outcome. "
    "Use task text and repository evidence to decide; do not narrow a requirement to match your implementation. "
    "Documentation, a changed test expectation or a different entry point does not resolve the original gap. "
    "A stated choice or a self-test written under your own reading does not close a conflict or fork: run the "
    "distinguishing input when one is given, on the public entry point, and say which reading the result follows. "
    "If still untested, keep that scope explicitly unverified."
)


_COMPACT_CONFLICT_HEADER: Final[str] = (
    "Open conflicts and forks — run the distinguishing input on the public entry point; a stated choice does not close one:"
)


def _requirement_conflict_block(lines: list[str]) -> list[str]:
    if not lines:
        return []
    return [
        "Requirement conflicts and open forks — compare your behavior with the quoted task wording:",
        *lines,
        CONFLICT_REVIEW_GUIDANCE,
    ]


def _truncate(value: str, length: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= length:
        return clean
    return f"{clean[: max(0, length - 1)].rstrip()}…"


def _requirement_digest(value: str, length: int) -> str:
    """Render a bounded head/tail digest plus literal contract tokens.

    The issue text already sits in the agent's prompt, so restating requirement
    prose here only duplicates it — and it is bulky enough that budget pressure
    used to drop the whole requirement section. What the Brief uniquely carries
    is the coverage state; what decides correctness is the backticked
    identifiers and the clause ending, which an acceptance check may compare
    verbatim. Keep both ends and those identifiers; leave the full prose to
    inspect_compass_brief.
    """
    clean = " ".join(value.split())
    tokens = list(dict.fromkeys(_IDENTIFIER_RE.findall(clean)))
    joined = ", ".join(tokens)
    if len(joined) > _MAX_KEPT_IDENTIFIER_CHARS:
        kept: list[str] = []
        size = 0
        for token in tokens:
            if size + len(token) + 2 > _MAX_KEPT_IDENTIFIER_CHARS:
                break
            kept.append(token)
            size += len(token) + 2
        joined = f"{', '.join(kept)}, +{len(tokens) - len(kept)} more"
    token_suffix = f" [{joined}]" if joined else ""
    prose_budget = max(16, length - len(token_suffix))
    if len(clean) <= prose_budget:
        prose = clean
    else:
        body_budget = max(8, prose_budget - 3)
        head_chars = max(4, (body_budget * 3) // 5)
        tail_chars = max(4, body_budget - head_chars)
        prose = f"{clean[:head_chars].rstrip()} … {clean[-tail_chars:].lstrip()}"
    return f"{prose}{token_suffix}".strip()


def _truncate_tension_line(line: str, length: int) -> str:
    prefix, separator, note = line.partition(": ")
    if not separator:
        return _truncate(line, length)
    return f"{prefix}: {_requirement_digest(note, length)}"


def _delivered_tension_count(brief: str, tensions: list[str]) -> int:
    return sum(line.partition(":")[0].removeprefix("- ") in brief for line in tensions)


def _minimal_brief(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    *,
    active: dict[str, ClaimRecord],
    current_step: int | None,
    budget: int,
) -> str:
    """Build a compact critical index instead of slicing a full reminder mid-section."""
    requirements = requirement_entries(goal)
    step = max(snapshot.state.processed_step, current_step or 0)
    top_conflicts = requirement_conflicts(active, snapshot.conflict_notes)
    tensions = [
        _truncate_tension_line(item, 32).removeprefix("- ")
        for item in tension_lines(active, snapshot.conflict_notes)
        if item not in top_conflicts
    ]
    # Requirement tensions outrank claim-to-claim ones when not all fit.
    tensions.sort(key=lambda item: item[0] not in "RK")

    result = _render_minimal_brief(
        snapshot,
        goal,
        requirements=requirements,
        step=step,
        tensions=tensions,
        conflicts=top_conflicts,
        budget=budget,
        include_contract_content=True,
    )
    if len(result) <= budget:
        return result

    # Contract prose is available through inspect_compass_brief.  Before falling back
    # to an omission index, remove only that prose and retain every R/K ID plus
    # its live coverage marker.  This avoids the old head/tail slice silently
    # deleting IDs from the middle of a long contract list.
    result = _render_minimal_brief(
        snapshot,
        goal,
        requirements=requirements,
        step=step,
        tensions=tensions,
        conflicts=top_conflicts,
        budget=budget,
        include_contract_content=False,
    )
    if len(result) <= budget:
        return result
    return _traceable_contract_index(snapshot, goal, requirements=requirements, step=step, active=active, budget=budget)


def _render_minimal_brief(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    *,
    requirements: tuple[tuple[str, str], ...],
    step: int,
    tensions: list[str],
    conflicts: list[str],
    budget: int,
    include_contract_content: bool,
) -> str:
    """Render compact contract rows, optionally dropping only their prose."""
    lines: list[str] = []
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    if snapshot.state.maintenance_degraded_after_step is not None:
        lines.append(
            f"[maintenance degraded: last maintained through step {snapshot.state.processed_step}; "
            "later evidence is pending. Earlier claims are not current guarantees; unverified decisions remain assumptions.]"
        )
    lines.extend(("[Goal & Claims — compact reminder]", f"Goal: {_requirement_digest(goal.description, 80)}"))
    lines.extend(_contract_warning(goal))
    lines.extend(_requirement_conflict_block(conflicts))
    for prefix, title in (("R", "Requirements"), ("K", "Constraints")):
        items = tuple(item for item in requirements if item[0].startswith(prefix))
        if not items:
            continue
        rows = []
        for requirement_id, content in items:
            coverage = snapshot.state.requirement_coverage.get(requirement_id, RequirementCoverage())
            marker = (
                "!"
                if coverage.level in {"none", "self_check", "stated"}
                else "?"
                if coverage.level in {"repo", "new_test"} or is_weak_coverage(coverage, active)
                else "+"
            )
            since = snapshot.state.weak_requirement_since.get(requirement_id)
            age = f"/{max(0, step - since)}steps" if since is not None and marker == "!" else ""
            row = f"{requirement_id}{marker}/{_COVERAGE_LABEL.get(coverage.level, coverage.level)}{age}"
            # Behavioral requirement wording often carries exact output keys
            # that must remain visible.  On the ID-only fallback, retain that
            # bounded R prose while K rows remain traceable by their complete
            # ID list and can be expanded with inspect_compass_brief.
            if include_contract_content or prefix == "R":
                row = f"{row} {_requirement_digest(content, 48)}"
            rows.append(row)
        lines.append(f"{title}: " + "; ".join(rows))
    footer = (
        f"Evidence step: {step}; Steps since last reminder: {max(0, step - snapshot.state.last_brief_step)}. "
        "inspect_compass_brief([...]) returns every full item."
    )
    if tensions:
        # Fill the tension line up to the budget and say how many did not
        # fit, instead of slicing the finished text and losing an unknown number.
        used = len("\n".join(lines)) + len("\nTensions: ") + len(footer) + 1
        shown: list[str] = []
        for index, item in enumerate(tensions):
            more = f"; +{len(tensions) - index} more (inspect_compass_brief([]))"
            if used + len(item) + len(more) > budget:
                break
            shown.append(item)
            used += len(item) + 2
        rest = len(tensions) - len(shown)
        lines.append("Tensions: " + "; ".join(shown) + (f"; +{rest} more (inspect_compass_brief([]))" if rest else ""))
    lines.append(footer)
    return "\n".join(lines).strip()


def _traceable_contract_index(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    *,
    requirements: tuple[tuple[str, str], ...],
    step: int,
    active: dict[str, ClaimRecord],
    budget: int,
) -> str:
    """Return a bounded omission index without silently losing contract IDs."""
    lines: list[str] = []
    if snapshot.state.maintenance_degraded_after_step is not None:
        lines.append(
            f"[maintenance degraded: last maintained through step {snapshot.state.processed_step}; "
            "later evidence is pending. Earlier claims are not current guarantees; unverified decisions remain assumptions.]"
        )
    lines.extend(("[Goal & Claims — compact index]", f"Goal: {_requirement_digest(goal.description, 48)}"))
    lines.extend(_contract_warning(goal))
    rest: list[str] = []
    for prefix, title in (("R", "Requirements"), ("K", "Constraints")):
        items = tuple(item for item in requirements if item[0].startswith(prefix))
        if not items:
            continue
        ids = tuple(item_id for item_id, _content in items)
        rest.append(
            f"{title}: {len(ids)} details omitted; IDs {_compact_id_ranges(ids)} — inspect_compass_brief by ID."
        )
    tension_count = len(tension_lines(active, snapshot.conflict_notes))
    if tension_count:
        rest.append(f"Tensions: {tension_count} details omitted — inspect_compass_brief([]).")
    rest.append(f"Evidence step: {step}; inspect_compass_brief([...]) returns every full item.")
    # Open conflicts and forks are the review work; keep the newest whole ones before omitting them.
    conflicts = requirement_conflicts(active, snapshot.conflict_notes)
    for shown in range(len(conflicts), -1, -1):
        block = [_COMPACT_CONFLICT_HEADER, *conflicts[len(conflicts) - shown:]] if shown else []
        result = "\n".join([*lines, *block, *rest]).strip()
        if len(result) <= budget:
            return result
    lines.extend(rest)

    # Keep progressively smaller *whole* omission markers for custom profiles
    # with tiny budgets.  Never slice a field: at the smallest sizes the
    # ellipsis is the explicit contract that the complete index was omitted.
    counts = []
    for prefix in ("R", "K"):
        ids = tuple(item_id for item_id, _content in requirements if item_id.startswith(prefix))
        if ids:
            counts.append(f"{prefix}:{len(ids)} omitted ({_compact_id_ranges(ids)})")
    if counts:
        compact = f"Contract details omitted: {'; '.join(counts)}; inspect_compass_brief by ID."
        if len(compact) <= budget:
            return compact
    compact_counts = "/".join(
        f"{prefix}{sum(item_id.startswith(prefix) for item_id, _content in requirements)}"
        for prefix in ("R", "K")
        if any(item_id.startswith(prefix) for item_id, _content in requirements)
    )
    traceable = f"{compact_counts}→inspect_compass_brief" if compact_counts else "…→inspect_compass_brief"
    if len(traceable) <= budget:
        return traceable
    categories = "/".join(
        prefix for prefix in ("R", "K") if any(item_id.startswith(prefix) for item_id, _content in requirements)
    )
    omission = f"{categories}→…" if categories else "…"
    if len(omission) <= budget:
        return omission
    return "…"


def _compact_id_ranges(ids: tuple[str, ...]) -> str:
    """Represent sequential contract IDs without losing their boundaries."""
    if not ids:
        return "none"
    ranges: list[str] = []
    start = ids[0]
    previous = ids[0]
    for item_id in ids[1:]:
        same_prefix = item_id[:1] == previous[:1]
        consecutive = item_id[1:].isdigit() and previous[1:].isdigit() and int(item_id[1:]) == int(previous[1:]) + 1
        if same_prefix and consecutive:
            previous = item_id
            continue
        ranges.append(start if start == previous else f"{start}-{previous}")
        start = previous = item_id
    ranges.append(start if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def _contract_warning(goal: GoalState) -> tuple[str, ...]:
    if any(item.tag == "extraction" and "regex_fallback" in item.content for item in goal.conclusions):
        return ("[Contract extraction degraded: R/K may omit behavior. Check the original task; coverage is incomplete.]",)
    return ()


# ---- V1.14: plain fixed-field Brief -------------------------------------------------------------------------
_LITE_CLAIMS_MAX: Final[int] = 12  # ponytail: fixed row cap; rank-by-relevance is the upgrade if key claims drop
_LITE_STATUS: Final[dict[str, str]] = {
    "none": "no evidence",
    "stated": "only claimed",
    "self_check": "own script only",
    "repo": "code read only",
    "new_test": "test you added",
    "repo_test": "repo test",
}
_LITE_CONFLICT: Final[dict[str, str]] = {
    "fork (boundary not yet run)": "boundary case not run yet",
    "fork (your reading is unverified)": "your reading may differ from the task text",
    "requirement": "your claim may break the requirement",
}
_LITE_SOURCE: Final[dict[str, str]] = {
    "agent_only": "your own statement",
    "self_check": "your own script",
    "new_test": "test you added",
    "repo_test": "repo test",
    "repo": "code read",
    "issue": "task text",
}


def render_active_brief_with_actions(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    current_step: int | None = None,
    phase: str = "",
    repo_path: str = "",
    max_already_read: int = 4,
    brief_budget_chars: int = 2500,
    detail_entry: str = CANONICAL_DETAIL_ENTRY,
    detail_renderer=None,
) -> tuple[str, tuple[str, ...]]:
    """V1.14 layout; the V1.9 renderer is the fallback when even the shortest V1.14 Brief is over budget."""
    budget = max(1, brief_budget_chars)
    step = max(snapshot.state.processed_step, current_step if current_step is not None else
               (evidence[-1].sequence if evidence else 0))
    finish = (lambda b: detail_renderer(b)) if detail_renderer else (lambda b: b.replace(CANONICAL_DETAIL_ENTRY, detail_entry))
    for claims in range(_LITE_CLAIMS_MAX, -1, -1):
        for already_read in (max_already_read, 0):
            brief = finish(_render_lite(snapshot, goal, evidence, step=step, repo_path=repo_path,
                                        claims_max=claims, max_already_read=already_read))
            if len(brief) <= budget:
                return brief, ("v114_lite", f"claims_{claims}")
    return _render_v19_with_actions(
        snapshot, goal, evidence, current_step=current_step, phase=phase, repo_path=repo_path,
        max_already_read=max_already_read, brief_budget_chars=budget, detail_entry=detail_entry,
        detail_renderer=detail_renderer,
    )


def _lite_source(claim: ClaimRecord) -> str:
    # agent_only on an observation means a tool result the host could not classify as a test, not a statement.
    return "command output" if claim.kind == "observation" and claim.grounding == "agent_only" else _LITE_SOURCE[claim.grounding]


_RISKY_DECISION = re.compile(
    r"^(Reading of the task|Dismissed failure|Changed test|Assumption|Claims done)\b"
    r"|dismiss|assum|interpret|expectation was changed|changed (the )?(test|expectation)", re.I)
_FOCUS_MAX: Final[int] = 5
_ROTATE: Final[int] = 4  # ponytail: fixed window; weight toward untested clauses if evidence ever keeps test bodies


def _render_lite(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    step: int,
    repo_path: str,
    claims_max: int,
    max_already_read: int,
) -> str:
    """V1.15: the whole contract on the first Brief; afterwards only what needs attention."""
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    live = {k: v for k, v in active.items() if v.verdict != "stale" and not v.superseded_by and v.kind != "activity"}
    requirements = requirement_entries(goal)
    text_of = dict(requirements)
    dropped = {rid for rid, text in requirements if text.startswith("[dropped at step")}
    conflicts = tension_lines(active, snapshot.conflict_notes, unresolved_only=True)
    description = _RUNTIME_GOAL_RE.sub("", goal.description).strip(" ;-:") or "(not set)"
    flat = lambda text: " ".join(text.split())
    newest = lambda c: max((parse_evidence_id(r) or 0 for r in c.evidence_refs), default=c.created_step)
    by_new = sorted(live.values(), key=newest, reverse=True)

    # A check is failing when the newest run it cites ended in error (a later passing rerun clears it), or a tool
    # result contradicted it. Only checks count: tests, own scripts, reproductions — not a grep miss or git hiccup.
    failed = {evidence_id(e.sequence) for e in evidence if e.outcome == "error" and (
        e.kind == "test" or is_test_attempt(e.target) or e.action_class in {"self_script", "new_test", "reproduce"})}
    latest_ref = lambda c: max(c.evidence_refs, key=lambda r: parse_evidence_id(r) or 0, default="")
    failing = [c for c in by_new if c.kind == "observation" and (c.verdict == "contradicted" or latest_ref(c) in failed)]
    passing = [c for c in by_new if c.kind == "observation" and c.verdict == "supported"
               and c.grounding in {"new_test", "repo_test"} and c not in failing]
    risky = [c for c in by_new if c.kind == "stated" and c.verdict != "supported" and _RISKY_DECISION.search(c.statement)]
    cap = min(_FOCUS_MAX, claims_max)
    failing, risky = failing[:cap], risky[:cap]

    # Requirements shown after the first Brief: every one in an open conflict, plus a rotating window over the rest,
    # so each requirement comes back in turn. No coverage link decides it (those links proved unreliable).
    pair_ids = lambda line: line[2:].partition(": ")[0].split(" ↔ ")
    hot = [i for line in conflicts for i in pair_ids(line) if re.fullmatch(r"[RK]\d+", i)]
    hot = [rid for rid in dict.fromkeys(hot) if rid in text_of and rid not in dropped]
    pool = [rid for rid, _ in requirements if rid not in hot and rid not in dropped]
    start = (snapshot.state.revision * _ROTATE) % len(pool) if pool else 0
    window = (pool + pool)[start:start + min(_ROTATE, len(pool))]

    lines = [f"=== COMPASS REMINDER (step {step}) ==="]
    if snapshot.state.maintenance_degraded_after_step is not None and snapshot.state.processed_step < step:
        lines.append(f"Note: covers steps up to {snapshot.state.processed_step} only.")
    lines += ["From this session's record only; no new facts, not proof. Details: inspect_compass_brief([\"R2\"]) or ([\"C-012\"]).",
              "", "GOAL", description, *_contract_warning(goal)]
    changes = [c.content for c in goal.conclusions if c.tag == "scope" and c.content.startswith("Contract change at step")]
    if changes:
        lines += ["", "CONTRACT CHANGES (corrected during this run)", *changes]

    if snapshot.state.last_brief_step == 0:  # first Brief: the whole contract once
        for title, prefix in (("REQUIREMENTS", "R"), ("CONSTRAINTS (each binds on its own)", "K")):
            rows = [(rid, t) for rid, t in requirements if rid.startswith(prefix) and rid not in dropped]
            if rows:
                lines += ["", title, *(f"{rid} | {flat(t)}" for rid, t in rows)]
    else:
        if hot:
            lines += ["", "REQUIREMENTS IN CONFLICT", *(f"{rid} | {flat(text_of[rid])}" for rid in hot)]
        if window:
            lines += ["", f"REQUIREMENTS THIS ROUND ({len(window)} of {len(pool)}; each comes back in turn — does the code still meet them?)",
                      *(f"{rid} | {flat(text_of[rid])}" for rid in window)]
        rest = tuple(rid for rid in pool if rid not in window)
        if rest:
            lines += [f"Not shown this round ({_compact_id_ranges(rest)}): your task text; inspect_compass_brief by ID."]

    todo = [f"Settle {' vs '.join(pair_ids(line))}: run the input that tells the two readings apart, on the public entry point."
            for line in conflicts]
    todo += [f"{c.id} is still failing: fix it, or say why it does not matter." for c in failing]
    todo += [f"{c.id} changed a test or dismissed a failure: the original expectation is still unchecked."
             for c in risky if re.search(r"^(Changed test|Dismissed failure)|dismiss|expectation was changed|changed (the )?(test|expectation)", c.statement, re.I)]
    if todo:
        lines += ["", "NEXT CHECKS", *(f"{n}. {t}" for n, t in enumerate(todo[:_FOCUS_MAX], 1))]

    if conflicts:
        lines += ["", "CONFLICTS (a stated choice or a self-written test does not settle one)"]
        for line in conflicts:
            pair, _, note = line[2:].partition(": ")
            kind, _, detail = note.partition(": ")
            lines.append(f"{pair.replace(' ↔ ', ' vs ')} | {_LITE_CONFLICT.get(kind, kind)}")
            if detail:
                lines.append(f"  detail: {flat(detail)}")
            lines += [f"  {cid} says: {flat(live[cid].statement)}" for cid in pair.split(" ↔ ") if cid in live]

    if failing:
        lines += ["", "FAILING OR UNRESOLVED CHECKS (newest first)", *(f"{c.id} | {flat(c.statement)}" for c in failing)]
    if passing:
        lines += ["", f"LATEST PASSING CHECK | {passing[0].id} | {flat(passing[0].statement)}"]
    if risky:
        lines += ["", "UNVERIFIED CHOICES THAT DECIDE CORRECTNESS", *(f"{c.id} | {flat(c.statement)}" for c in risky)]
    shown = len(failing) + bool(passing) + len(risky)
    if len(live) > shown:
        lines.append(f"(+{len(live) - shown} other claims: inspect_compass_brief([]))")

    already = render_previously_read(evidence, root=repo_path, max_files=max_already_read)
    if already:
        lines += ["", "ALREADY READ", *already]
    return "\n".join(lines).strip()


def _render_lite_v114(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    step: int,
    repo_path: str,
    claims_max: int,
    max_already_read: int,
) -> str:
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    live = {k: v for k, v in active.items() if v.verdict != "stale" and not v.superseded_by}
    requirements = requirement_entries(goal)
    ages = {rid: age for age, rid, _, _ in weak_requirements(snapshot, requirements, current_step=step, minimum_age=0)}
    conflicts = tension_lines(active, snapshot.conflict_notes, unresolved_only=True)
    description = _RUNTIME_GOAL_RE.sub("", goal.description).strip(" ;-:") or "(not set)"

    def status(rid: str) -> str:
        level = snapshot.state.requirement_coverage.get(rid, RequirementCoverage()).level
        return f"{_LITE_STATUS.get(level, level)}, not confirmed" if rid in ages and level in {"new_test", "repo_test"} \
            else _LITE_STATUS.get(level, level) if rid in ages else "passing test"

    lines = [f"=== COMPASS REMINDER (step {step}) ==="]
    if snapshot.state.maintenance_degraded_after_step is not None and snapshot.state.processed_step < step:
        lines.append(f"Note: this reminder covers steps up to {snapshot.state.processed_step} only; later steps are not in it yet.")
    lines += [
        "Built only from what this session already recorded. It adds no new facts and is not proof.",
        'Look up any ID for full detail: inspect_compass_brief(["R2"]) or inspect_compass_brief(["C-012"]).',
        "", "GOAL", description, *_contract_warning(goal),
    ]
    changes = [c.content for c in goal.conclusions if c.tag == "scope" and c.content.startswith("Contract change at step")]
    if changes:
        lines += ["", "CONTRACT CHANGES (the goal/requirements below were corrected during this run)", *changes]
    dropped = {rid for rid, text in requirements if text.startswith("[dropped at step")}

    # NEXT CHECKS: IDs only, grouped by what is missing; the detail lives in the sections below.
    todo: list[str] = []
    todo += [f"Settle conflict {line[2:].partition(': ')[0].replace(' ↔ ', ' vs ')}: run the input that tells the "
             "two readings apart, on the public entry point (see CONFLICTS)." for line in conflicts]
    todo += [f"{c.id} was contradicted by a tool result; do not rely on it." for c in live.values() if c.verdict == "contradicted"]
    groups: dict[str, list[str]] = {}
    for rid, _ in requirements:
        if rid in ages and rid not in dropped:
            groups.setdefault(status(rid), []).append(rid)
    advice = {"code read only": "reading code does not show the behavior; run a test that exercises it",
              "no evidence": "nothing checks it yet"}
    for label, ids in groups.items():
        todo.append(f"{_compact_id_ranges(tuple(ids))}: {label} — {advice.get(label, 'no passing test shows it yet')}.")
    if todo:
        lines += ["", "NEXT CHECKS", *(f"{n}. {line}" for n, line in enumerate(todo, 1))]

    for title, prefix in (("REQUIREMENTS (status = strongest evidence so far)", "R"),
                          ("CONSTRAINTS (each one binds on its own)", "K")):
        rows = [(rid, text) for rid, text in requirements if rid.startswith(prefix)]
        if rows:
            lines += ["", title]
            lines += [f"{rid} | {'dropped' if rid in dropped else status(rid)} | {' '.join(text.split())}" for rid, text in rows]

    if conflicts:
        lines += ["", "CONFLICTS (not settled; a choice you stated or a test written to your own reading does not settle one)"]
        for line in conflicts:
            pair, _, note = line[2:].partition(": ")
            kind, _, detail = note.partition(": ")
            lines.append(f"{pair.replace(' ↔ ', ' vs ')} | {_LITE_CONFLICT.get(kind, kind)}")
            if detail:
                lines.append(f"  detail: {' '.join(detail.split())}")
            lines += [f"  {cid} says: {' '.join(live[cid].statement.split())}"
                      for cid in pair.split(" ↔ ") if cid in live]

    failed = {evidence_id(e.sequence) for e in evidence if e.outcome == "error" and (e.kind == "test" or is_test_attempt(e.target))}
    ranked = sorted((c for c in live.values() if c.kind != "activity"), key=lambda c: _claim_sort_key(c, failed))
    half = (claims_max + 1) // 2
    sections = (
        ("TOOL RESULTS (what commands and tests showed; ID | check status | source | text)",
         [c for c in ranked if c.kind == "observation"][:half]),
        ("YOUR DECISIONS AND PLANS (not checked unless marked supported; ID | check status | source | text)",
         [c for c in ranked if c.kind == "stated"][:claims_max - half]),
    )
    shown = 0
    for title, rows in sections:
        if rows:
            lines += ["", title]
            lines += [f"{c.id} | {c.verdict} | {_lite_source(c)} | {' '.join(c.statement.split())}" for c in rows]
            shown += len(rows)
    if len(ranked) > shown:
        lines.append(f"(+{len(ranked) - shown} older claims: inspect_compass_brief([]))")

    already = render_previously_read(evidence, root=repo_path, max_files=max_already_read)
    if already:
        lines += ["", "ALREADY READ", *already]
    return "\n".join(lines).strip()
