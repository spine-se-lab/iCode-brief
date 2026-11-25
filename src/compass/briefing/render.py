# Copyright (c) 2026 Chrys. All rights reserved.

"""Mechanical rendering of the active Goal & Claims Brief."""

from __future__ import annotations

import re
from itertools import zip_longest
from typing import Final

from compass.briefing.evidence import render_previously_read
from compass.briefing.policy import weak_requirements
from compass.claims.coverage import is_weak_coverage
from compass.claims.models import (
    ClaimGrounding,
    ClaimRecord,
    ClaimSnapshot,
    RequirementCoverage,
    parse_claim_id,
    parse_evidence_id,
    statement_excerpt,
)
from compass.evidence.actions import is_test_attempt
from compass.evidence.models import EvidenceRecord, evidence_id
from compass.task_contract.extraction import requirement_entries
from compass.task_contract.models import GoalState

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
    max_already_read: int | None = None,
    brief_budget_chars: int | None = None,
    detail_entry: str = CANONICAL_DETAIL_ENTRY,
    detail_renderer=None,
) -> str:
    """Render the complete active projection; explicit budgets retain legacy evaluation behavior."""
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


def render_active_brief_with_actions(
    snapshot: ClaimSnapshot,
    goal: GoalState,
    evidence: tuple[EvidenceRecord, ...],
    *,
    current_step: int | None = None,
    phase: str = "",
    repo_path: str = "",
    max_already_read: int | None = None,
    brief_budget_chars: int | None = None,
    detail_entry: str = CANONICAL_DETAIL_ENTRY,
    detail_renderer=None,
) -> tuple[str, tuple[str, ...]]:
    """Render the Brief and report every deterministic budget action applied."""
    if brief_budget_chars is None:
        brief, actions = _render_canonical_brief(
            snapshot,
            goal,
            evidence,
            current_step=current_step,
            phase=phase,
            repo_path=repo_path,
            max_already_read=None,
            brief_budget_chars=None,
        )
        reference = _reference_contract(snapshot, goal, budget=None)
        focus = _verification_focus(snapshot, goal, evidence, budget=None)
        brief = "\n\n".join(section for section in (reference, brief, focus) if section)
        rendered = detail_renderer(brief) if detail_renderer else brief.replace(CANONICAL_DETAIL_ENTRY, detail_entry)
        return rendered, actions
    budget = max(1, brief_budget_chars)
    canonical_budget = budget
    while True:
        reference = (
            _reference_contract(snapshot, goal, budget=canonical_budget * 2 // 5) if canonical_budget >= 1400 else ""
        )
        focus = (
            _verification_focus(snapshot, goal, evidence, budget=min(650, canonical_budget // 3))
            if canonical_budget >= 1400
            else ""
        )
        brief, actions = _render_canonical_brief(
            snapshot,
            goal,
            evidence,
            current_step=current_step,
            phase=phase,
            repo_path=repo_path,
            max_already_read=max_already_read,
            brief_budget_chars=canonical_budget
            - len(focus)
            - (2 if focus else 0)
            - len(reference)
            - (2 if reference else 0),
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


def _reference_contract(snapshot: ClaimSnapshot, goal: GoalState, *, budget: int | None) -> str:
    """Prioritize derived clarifications, without promoting them to verified facts."""
    conclusions = [c for tag in ("criteria", "constraint") for c in goal.conclusions if c.tag == tag]
    entries = [
        entry
        for entry, conclusion in zip(requirement_entries(goal), conclusions, strict=True)
        if conclusion.reference_derived
    ]
    if not entries:
        return ""
    offset = snapshot.state.revision % len(entries)
    entries = entries[offset:] + entries[:offset]
    lines = ["Priority contract clarifications — check against issue/source; not verification:"]
    for identity, content in entries:
        line = f"- {identity}: {content}"
        if budget is not None and len("\n".join([*lines, line])) > budget:
            continue  # Keep full clauses; the canonical index still exposes every ID.
        lines.append(line)
        if budget is not None and len(lines) == 4:
            break
    return "\n".join(lines) if len(lines) > 1 else ""


def _failed_check_claims(snapshot: ClaimSnapshot, evidence: tuple[EvidenceRecord, ...]):
    """Follow merged provenance to the original failed scope, not the latest passing result.

    A merge hides children from the active projection but does not resolve them.
    Explicit correction/supersession still retires a check through the existing
    claim protocol. Missing legacy children and cyclic data grant no evidence.
    """
    records = {item.id: item for item in snapshot.claims}
    failed = {evidence_id(item.sequence): item for item in evidence if item.outcome == "error"}
    behavior_claims = {
        ref
        for identity, coverage in snapshot.state.requirement_coverage.items()
        if identity.startswith("R")
        for ref in coverage.claim_ids
    }
    for parent in snapshot.claims:
        if parent.id not in snapshot.state.active_ids:
            continue
        pending, seen = [(parent.id, False)], set()
        while pending:
            identity, expanded = pending.pop()
            if identity in seen and not expanded:
                continue
            seen.add(identity)
            item = records.get(identity)
            if item is None or item.verdict == "stale" or item.superseded_by:
                continue
            if not expanded:
                pending.append((identity, True))
                pending.extend((child, False) for child in reversed(item.supporting_claim_ids))
                continue
            refs = tuple(
                ref
                for ref in item.evidence_refs
                if ref in failed
                and (
                    failed[ref].kind == "test"
                    or is_test_attempt(failed[ref].target)
                    or (parent.id in behavior_claims and failed[ref].action_class in {"reproduce", "self_script"})
                )
            )
            # Prefer the original leaf's scope over its compressed parent text.
            if item.kind == "observation" and refs:
                yield parent, item, refs


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
    # Rank existing facts, not inferred defects. A nonzero grep exit is not a
    # failed behavior check; require a test attempt, or a reproduction/self-check
    # already associated with a behavior requirement. Later references alone do
    # not establish that the original error was resolved for the same input.
    candidates: list[tuple[int, str, str, str]] = []
    for parent, item, refs in _failed_check_claims(snapshot, evidence):
        identities = tuple(dict.fromkeys((parent.id, item.id, *(refs if budget is None else refs[:2]))))
        candidates.append(
            (
                0,
                ", ".join(identities),
                item.statement,
                "Same-scope resolution needed; changed tests or unrelated passes do not resolve this error.",
            )
        )
        if budget is not None:
            break  # Bounded evaluation leaves room for separate contract checks.
    for tension in tension_lines(active, snapshot.conflict_notes, unresolved_only=True):
        refs, _, note = tension.removeprefix("- ").partition(": ")
        candidates.append(
            (4 if refs.startswith("K") else 1, refs, note, "Which check resolves these conflicting scopes?")
        )
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
        for entry in pair
        if entry is not None
    ]
    if contract:
        offset = snapshot.state.revision % len(contract)
        contract = contract[offset:] + contract[:offset]
    for identity, content in contract:
        coverage = snapshot.state.requirement_coverage.get(identity, RequirementCoverage())
        links = [ref for ref in coverage.claim_ids if ref in active]
        if budget is not None:
            links = links[-2:]
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
    if budget is None:
        return "\n".join(
            [
                "Verification focus — questions, not findings:",
                *(
                    f"- {refs}: {content} — {question}"
                    for _, refs, content, question in sorted(candidates, key=lambda item: item[0])
                ),
            ]
        )
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
        allowance = min(
            budget - len("\n".join(lines)) - 1, max(row_budget, budget * 2 // 3) if priority == 0 else row_budget
        )
        text_budget = allowance - len(prefix) - len(suffix)
        if text_budget < 20:
            continue
        lines.append(prefix + statement_excerpt(" ".join(content.split()), text_budget) + suffix)
        contract_shown |= priority == 3
        if budget is not None and len(lines) == 4:
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
    max_already_read: int | None,
    brief_budget_chars: int | None,
) -> tuple[str, tuple[str, ...]]:
    unbounded = brief_budget_chars is None
    latest_evidence_step = evidence[-1].sequence if evidence else 0
    effective_current_step = max(
        snapshot.state.processed_step,
        current_step if current_step is not None else latest_evidence_step,
    )
    active = {item.id: item for item in snapshot.claims if item.id in snapshot.state.active_ids}
    shown = [
        active[item]
        for item in snapshot.state.active_ids
        if item in active
        and (
            unbounded
            or (active[item].kind != "activity" and active[item].verdict != "stale" and not active[item].superseded_by)
        )
    ]
    # A grep miss or shell/poll timeout is not by itself an unresolved behavior failure.
    # Preserve old failed checks ahead of routine newer observations; age alone cannot resolve them.
    failed_refs = {
        evidence_id(item.sequence)
        for item in evidence
        if item.outcome == "error" and (item.kind == "test" or is_test_attempt(item.target))
    }
    failed_claim_ids = frozenset(parent.id for parent, _item, _refs in _failed_check_claims(snapshot, evidence))
    shown.sort(key=lambda item: _claim_sort_key(item, failed_refs, failed_claim_ids))
    actions: list[str] = []
    all_tensions = tension_lines(active, snapshot.conflict_notes)
    tension_count = len(all_tensions)
    already_read_limit = None if unbounded else min(4, max(0, 4 if max_already_read is None else max_already_read))
    if max_already_read is not None and already_read_limit is not None and max_already_read > already_read_limit:
        actions.append("already_read_4")

    # Keep a small window of concrete observations/risks while shortening prose.
    # Otherwise a long contract can consume the entire Brief and hide every fact.
    claim_limit = len(shown)
    priority_claims = [item for item in shown if _priority_claim(item)]
    claim_floor = min(3, len(priority_claims))
    requirement_chars = None if unbounded else _REQUIREMENT_LABEL_CHARS
    tension_chars = None if unbounded else 180

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
    if unbounded:
        return brief, (f"tensions_rendered_{tension_count}", "tensions_suppressed_0")
    budget = max(1, brief_budget_chars)
    while len(brief) > budget and already_read_limit > 0:
        already_read_limit -= 1
        actions.append(f"already_read_{already_read_limit}")
        brief = _render(claim_limit)
    while len(brief) > budget and claim_limit > claim_floor:
        claim_limit -= 1
        brief = _render(claim_limit)
    while len(brief) > budget and requirement_chars > 64:
        requirement_chars = max(64, requirement_chars - 16)
        actions.append(f"requirement_chars_{requirement_chars}")
        brief = _render(claim_limit)
    while len(brief) > budget and tension_chars > 48:
        tension_chars = max(48, tension_chars - 16)
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
        brief = (
            _minimal_brief(
                snapshot,
                goal,
                active=active,
                current_step=effective_current_step,
                budget=budget - len(priority_block),
            )
            + priority_block
        )
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
    max_already_read: int | None,
    requirement_chars: int | None,
    tension_chars: int | None,
) -> str:
    visible_claims = shown[:claim_limit]
    requirements = requirement_entries(goal)
    description = (
        goal.description if requirement_chars is None else _RUNTIME_GOAL_RE.sub("", goal.description).strip(" ;-:")
    ) or "(not set)"
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
    lines.extend(_symptom_context(goal, compact=False, complete=requirement_chars is None))
    top_conflicts = requirement_conflicts(
        active, snapshot.conflict_notes, limit=None if requirement_chars is None else _REQUIREMENT_CONFLICTS_SHOWN
    )
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
        title="Requirements:"
        if requirement_chars is None
        else 'Requirements (issue text is in your prompt; full text: inspect_compass_brief(["R2"])):',
        items=behavior_requirements,
        content_chars=requirement_chars,
    )
    _append_requirement_section(
        lines,
        snapshot,
        title="Constraints (independently binding):"
        if requirement_chars is None
        else 'Constraints (independently binding; full text: inspect_compass_brief(["K1"])):',
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
        lines.extend(
            _claim_line(item, statement_chars=None if requirement_chars is None else _CLAIM_TEXT_CHARS)
            for item in visible_claims
        )
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
    content_chars: int | None,
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


def attention_lines(snapshot, requirements, *, current_step=None, minimum_age=24, content_chars=None) -> list[str]:
    """The same weak-coverage reminders for model context and sidebar groups."""
    overdue = weak_requirements(snapshot, requirements, current_step=current_step, minimum_age=minimum_age)
    lines = []
    for age, requirement_id, severity, content in overdue:
        lines.append(
            f"- [{severity}] {requirement_id} has stayed weak for {age} steps — {_requirement_digest(content, content_chars)}"
        )
    return lines


def _priority_claim(item: ClaimRecord) -> bool:
    return item.verdict != "stale" and (
        item.kind in {"observation", "stated"} or item.verdict == "contradicted" or bool(item.conflicts_with)
    )


def _claim_sort_key(
    item: ClaimRecord, failed_refs: set[str], failed_claim_ids: frozenset[str] = frozenset()
) -> tuple[int, ...]:
    # Only failed checks raise visibility, not every failed shell operation.
    latest_ref = max(item.evidence_refs, key=lambda ref: parse_evidence_id(ref) or 0, default="")
    risk = (
        item.verdict == "contradicted"
        or bool(item.conflicts_with)
        or item.id in failed_claim_ids
        or bool(set(item.evidence_refs) & failed_refs)
    )
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


def _claim_line(item: ClaimRecord, *, statement_chars: int | None) -> str:
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
    if statement_chars is None:
        provenance = ", ".join(item.evidence_refs)
        if item.supporting_claim_ids:
            provenance += ("; " if provenance else "") + "merged from " + ", ".join(item.supporting_claim_ids)
        return f"- {item.id}{status}{kind} {item.statement} ({provenance or 'no tool evidence'})"
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
        if (
            not note.startswith("resolved: ")
            and claim is not None
            and (claim.verdict == "stale" or claim.superseded_by)
        ):
            continue
        seen.add(pair)
        lines.append(f"- {requirement_id} ↔ {claim_id}: {note}")
    return lines


def requirement_conflicts(
    active: dict[str, ClaimRecord],
    notes: tuple[tuple[str, str, str], ...],
    *,
    limit: int | None = _REQUIREMENT_CONFLICTS_SHOWN,
) -> list[str]:
    """The newest unresolved requirement ↔ claim tensions, shown whole at the top of every Brief."""
    lines = [line for line in tension_lines(active, notes, unresolved_only=True) if line[2:3] in {"R", "K"}]
    return lines if limit is None else lines[-limit:]


CONFLICT_REVIEW_GUIDANCE = (
    "If the wording allows two readings, check both on the same public object, conditions and observable outcome. "
    "Use task text and repository evidence to decide; do not narrow a requirement to match your implementation. "
    "Documentation, a changed test expectation or a different entry point does not resolve the original gap. "
    "If still untested, keep that scope explicitly unverified."
)


def _requirement_conflict_block(lines: list[str]) -> list[str]:
    if not lines:
        return []
    return [
        "Requirement conflicts — compare your behavior with the quoted task wording:",
        *lines,
        CONFLICT_REVIEW_GUIDANCE,
    ]


def _truncate(value: str, length: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= length:
        return clean
    return f"{clean[: max(0, length - 1)].rstrip()}…"


def _requirement_digest(value: str, length: int | None) -> str:
    """Render a bounded head/tail digest plus literal contract tokens.

    The issue text already sits in the agent's prompt, so restating requirement
    prose here only duplicates it — and it is bulky enough that budget pressure
    used to drop the whole requirement section. What the Brief uniquely carries
    is the coverage state; what decides correctness is the backticked
    identifiers and the clause ending, which an acceptance check may compare
    verbatim. Keep both ends and those identifiers; leave the full prose to
    inspect_compass_brief.
    """
    if length is None:
        return value
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


def _truncate_tension_line(line: str, length: int | None) -> str:
    if length is None:
        return line
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
    lines.extend(_symptom_context(goal, compact=True))
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
    lines.extend(_symptom_context(goal, compact=True))
    for prefix, title in (("R", "Requirements"), ("K", "Constraints")):
        items = tuple(item for item in requirements if item[0].startswith(prefix))
        if not items:
            continue
        ids = tuple(item_id for item_id, _content in items)
        lines.append(
            f"{title}: {len(ids)} details omitted; IDs {_compact_id_ranges(ids)} — inspect_compass_brief by ID."
        )
    tension_count = len(tension_lines(active, snapshot.conflict_notes))
    if tension_count:
        lines.append(f"Tensions: {tension_count} details omitted — inspect_compass_brief([]).")
    lines.append(f"Evidence step: {step}; inspect_compass_brief([...]) returns every full item.")
    result = "\n".join(lines).strip()
    if len(result) <= budget:
        return result

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
        return (
            "[Contract extraction degraded: R/K may omit behavior. Check the original task; coverage is incomplete.]",
        )
    return ()


def _symptom_context(goal: GoalState, *, compact: bool, complete: bool = False) -> tuple[str, ...]:
    symptoms = [item.content for item in goal.conclusions if item.tag == "symptom"]
    if not symptoms:
        return ()
    if compact:
        return (
            f'Reported symptoms: {len(symptoms)} background entries, not requirements; inspect_compass_brief(["goal"]).',
        )
    return (
        "",
        "Reported symptoms (background, not requirements or verified current behavior):",
        *(
            "- " + _requirement_digest(text, None if complete else 240)
            for text in (symptoms if complete else symptoms[:3])
        ),
        f'Full symptom context ({len(symptoms)} entries): inspect_compass_brief(["goal"]).',
    )
