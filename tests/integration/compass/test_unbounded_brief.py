# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Complete default Briefs survive extraction, delivery, persistence and UI wrapping."""

import json
from dataclasses import asdict
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from rich.console import Console

from compass.adapters.icode.commands import status_snapshot
from compass.adapters.icode.tui.details import DetailEntry, EntryPrompt
from compass.adapters.icode.tui.presentation import brief_entries, coverage_summary
from compass.briefing.render import render_active_brief, render_active_brief_with_actions
from compass.claims.models import ClaimRecord, ClaimSnapshot, ClaimState, RequirementCoverage
from compass.evidence.models import EvidenceRecord
from compass.task_contract.extraction import (
    ExtractedConstraint,
    ExtractedRequirement,
    IssueExtraction,
    generate_issue_layer,
    requirement_entries,
)
from compass.task_contract.models import GoalConclusion, GoalState, parse_goal_state, serialize_goal_state

from .test_maintenance_refresh import gate_at, initialized, observation


def large_state():
    goal = GoalState(
        description="Complete goal " + "important middle clause " * 100 + "GOAL-END",
        conclusions=tuple(
            GoalConclusion(f"Requirement {i}: " + "preserve this clause; " * 80 + f"END-{i}", tag="criteria")
            for i in range(70)
        )
        + tuple(GoalConclusion(f"Constraint {i}: " + "do not omit; " * 80, tag="constraint") for i in range(10))
        + tuple(GoalConclusion(f"Symptom {i}: " + "background detail " * 40, tag="symptom") for i in range(5)),
    )
    claims = tuple(
        ClaimRecord(
            f"C-{i:03}",
            f"Claim {i}: " + "recorded observation " * 50 + f"CLAIM-END-{i}",
            "observation",
            "verify",
            i,
            evidence_refs=tuple(f"E-{n:03}" for n in range(1, 9)),
            verdict="supported",
        )
        for i in range(1, 41)
    )
    snapshot = ClaimSnapshot(ClaimState(active_ids=tuple(c.id for c in claims)), claims)
    evidence = tuple(
        EvidenceRecord(
            i,
            "observation",
            "ok",
            "read_file",
            tool_kind="filesystem.read",
            target=f"module_{i}.py",
            files=(f"module_{i}.py",),
            read_full=True,
            action_class="read",
        )
        for i in range(1, 15)
    )
    return goal, snapshot, evidence


def test_default_brief_keeps_every_full_entry_beyond_old_limits():
    goal, snapshot, evidence = large_state()
    brief, actions = render_active_brief_with_actions(snapshot, goal, evidence)
    assert len(brief) > 100_000
    assert goal.description in brief
    for item in goal.conclusions:
        assert item.content in brief
    for claim in snapshot.claims:
        assert claim.statement in brief
        assert ", ".join(claim.evidence_refs) in brief
    for record in evidence:
        assert record.target in brief
    assert "tensions_suppressed_0" in actions
    assert " … " not in brief
    # Legacy evaluation callers can still explicitly opt into a finite budget.
    assert len(render_active_brief(snapshot, goal, evidence, brief_budget_chars=500)) <= 500


def test_long_goal_and_requirements_survive_extraction_and_storage():
    title = "Resolve all issues " + "with this full detail " * 100 + "TITLE-END"
    requirements = tuple(ExtractedRequirement(f"Route {i} must " + "preserve behavior " * 80) for i in range(70))
    constraints = (ExtractedConstraint("Do not " + "drop this invariant " * 100),)
    goal = generate_issue_layer(
        title,
        extraction=IssueExtraction(
            requirements=requirements, constraints=constraints, signature_changes=(), goal=title
        ),
    )
    restored = parse_goal_state(serialize_goal_state(goal))
    assert restored.description == title
    contents = dict(requirement_entries(restored)).values()
    for item in (*requirements, *constraints):
        assert item.text in contents


@pytest.mark.parametrize("listed", [False, True])
def test_fallback_extraction_does_not_drop_late_requirements(listed):
    requirements = [f"Route {i} must return response {i}." for i in range(25)]
    issue = "Requirements:\n" + "\n".join("- " + item if listed else item for item in requirements)
    contents = dict(requirement_entries(generate_issue_layer(issue))).values()
    assert all(item in contents for item in requirements)


def test_requirement_rows_wrap_all_text_and_associations():
    text = "\n".join(f"Clause {i} must remain visible." for i in range(40))
    meta = coverage_summary(RequirementCoverage(tuple(f"C-{i:03}" for i in range(20))), compact=True)
    entry = DetailEntry("R1", "R1", text, text, meta)
    stream = StringIO()
    Console(file=stream, width=32, color_system=None, force_terminal=False, _environ={}).print(EntryPrompt(entry))
    rendered = " ".join(stream.getvalue().split())
    for i in range(40):
        assert f"Clause {i} must remain visible." in rendered
    assert "C-019" in rendered and "…" not in rendered


def test_evidence_details_do_not_clip_the_stored_excerpt():
    excerpt = "raw recorded line\n" * 400 + "EVIDENCE-END"
    evidence = (EvidenceRecord(1, "observation", "ok", "read_file", excerpt=excerpt),)
    _, lookup = brief_entries(GoalState(), ClaimSnapshot(), evidence)
    assert excerpt in lookup["E-001"].body


@pytest.mark.asyncio
async def test_default_gate_delivers_full_brief_without_size_folding_and_restores(tmp_path):
    gate, model = await initialized(tmp_path)
    _, snapshot, _ = large_state()
    session = gate.session
    for _ in range(8):
        gate.observe(observation())
    await gate.flush_observations(session)
    await session.claims.restore(json.loads(json.dumps(asdict(snapshot))))
    # A real maintenance call must succeed without invoking size/count folding.
    session._runner._fold_if_possible = AsyncMock(side_effect=AssertionError("Unexpected automatic folding"))
    gate.observe(observation("test"))
    result = await gate.update_brief()
    assert result["outcome"] == "updated"
    assert result["maintenance_error"] == ""
    session._runner._fold_if_possible.assert_not_called()
    for claim in snapshot.claims:
        assert claim.statement in result["brief"]
    assert len(result["brief"]) > 6000
    inspected = await session.inspect()
    for claim in snapshot.claims:
        assert claim.statement in inspected
    reopened = gate_at(tmp_path, model)
    before = json.dumps(reopened.state(), sort_keys=True)
    preview = status_snapshot(SimpleNamespace(gate=reopened))
    assert reopened.session is None
    assert json.dumps(reopened.state(), sort_keys=True) == before
    for claim in snapshot.claims:
        assert claim.statement in preview.brief
    restored = await reopened.ready()
    for claim in snapshot.claims:
        assert claim.statement in restored.brief_for_request()
