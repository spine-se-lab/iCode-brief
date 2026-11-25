# ruff: noqa: RUF001
"""Contract preservation: original reviewer counterexamples and context round trips."""

import pytest

from compass.gate.controller import task_messages
from compass.task_contract.extraction import (
    ExtractedConstraint,
    ExtractedRequirement,
    IssueExtraction,
    _interactive_contract,
)


def test_mixed_tool_sentence_keeps_latest_edit_restriction():
    inputs = [
        {"text": "修复分页问题。"},
        {"text": "现在只补测试，不要修改 pagination.py，请调用 update_compass_brief 返回 Brief。"},
    ]
    text = "\n".join(task_messages(inputs))
    assert "只补测试" in text and "不要修改 pagination.py" in text


def test_request_to_fix_compass_tool_is_a_real_task():
    result = task_messages([{"text": "修复 update_compass_brief，确保需求变更时刷新 Brief。"}])
    assert result, "A task about a Compass function must not disappear"


def test_explicit_readme_delegation_preserves_behavior():
    behavior = "Empty input must return an empty list."
    extraction = IssueExtraction(
        requirements=(ExtractedRequirement("Explain the changes."), ExtractedRequirement(behavior)),
        constraints=(),
        signature_changes=(),
    )
    result = _interactive_contract(
        extraction,
        ("Implement all requirements in README and explain the changes.",),
        "README: " + behavior,
    )
    assert [item.text for item in result.requirements] == ["Explain the changes.", "(repo) " + behavior]


def test_only_defect_entry_is_not_revived_by_nonempty_fallback():
    symptom = "The first page currently skips items."
    extraction = IssueExtraction(
        requirements=(ExtractedRequirement(symptom),),
        constraints=(),
        signature_changes=(),
    )
    result = _interactive_contract(extraction, ("Fix the pagination issue.",), "README: " + symptom)
    assert symptom not in [item.text for item in result.requirements]
    assert result.symptoms[0].text.removeprefix("(repo) ") == symptom


@pytest.mark.parametrize(
    "constraint",
    [
        "Don't break callers that currently fail fast on bad input.",
        "原来通过的测试不许失败。",
    ],
)
@pytest.mark.parametrize("user_text", [None, "Preserve the repository contract."])
def test_normative_variants_are_not_silently_deleted(constraint, user_text):
    extraction = IssueExtraction(
        requirements=(ExtractedRequirement("Review the implementation."),),
        constraints=(ExtractedConstraint(constraint),),
        signature_changes=(),
    )
    result = _interactive_contract(extraction, (user_text or constraint,), "")
    assert constraint in [item.text for item in result.constraints]


@pytest.mark.parametrize(
    "message",
    [
        "请调用 update_compass_brief 更新并返回当前 Brief。",
        "Call update_compass_brief without modifying pagination.py.",
        "Do not call update_compass_brief until tests finish.",
        "Don't break callers that currently fail fast on bad input.",
        "禁止调用 update_compass_brief。",
    ],
)
def test_qualified_or_tool_only_messages_are_preserved(message):
    assert task_messages([{"text": message}]) == (message,)


@pytest.mark.parametrize(
    "message",
    [
        "只补测试，不改 pagination.py，请调用 update_compass_brief 更新并返回当前 Brief。",
        "Please call update_compass_brief, don't edit pagination.py.",
    ],
)
def test_only_unqualified_invocation_clause_is_separated(message):
    text = "\n".join(task_messages([{"text": message}]))
    assert "pagination.py" in text
    assert "update_compass_brief" not in text


def test_user_quoted_and_ambiguous_clauses_remain_active():
    clauses = (
        "The parser currently fails fast on malformed inputs.",
        "The callers that currently fail fast have to keep doing so.",
    )
    for clause in clauses:
        extraction = IssueExtraction(requirements=(ExtractedRequirement(clause),))
        result = _interactive_contract(extraction, (clause,), "")
        assert result.requirements == extraction.requirements
        assert not result.symptoms


def test_background_survives_goal_persistence_status_and_brief():
    from compass.adapters.icode.tui.presentation import brief_entries
    from compass.briefing.render import render_active_brief
    from compass.claims.models import ClaimSnapshot
    from compass.task_contract.extraction import generate_issue_layer, requirement_entries
    from compass.task_contract.models import parse_goal_state, serialize_goal_state

    symptom = "The first page currently skips items."
    entry = ExtractedRequirement("Pages start at 1. " + symptom + " [where: pagination.py]", True)
    extraction = _interactive_contract(
        IssueExtraction(requirements=(entry,), goal="Fix pagination"), ("Fix pagination",), ""
    )
    goal = generate_issue_layer("Fix pagination", extraction=extraction)
    restored = parse_goal_state(serialize_goal_state(goal))
    assert restored == goal
    background = [item for item in restored.conclusions if item.tag == "symptom"]
    assert len(background) == 1 and background[0].content == symptom + " [where: pagination.py]"
    assert background[0].reference_derived is True
    assert all("currently skips" not in text for _, text in requirement_entries(restored))
    groups, _ = brief_entries(restored, ClaimSnapshot(), ())
    assert groups["goal"][0].body == restored.description
    assert symptom not in groups["goal"][0].body
    rendered = render_active_brief(ClaimSnapshot(), restored, (), brief_budget_chars=8000)
    assert "background, not requirements" in rendered and symptom in rendered
    compact = render_active_brief(ClaimSnapshot(), restored, (), brief_budget_chars=500)
    assert len(compact) <= 500


@pytest.mark.asyncio
async def test_symptom_only_result_reports_missing_requirements_and_retains_context(tmp_path):
    import json

    from compass.gate.controller import GateError
    from compass.ports import ModelReply
    from compass.task_contract.extraction import requirement_entries
    from tests.integration.compass.test_contract_assessment import AssessmentModel
    from tests.integration.compass.test_maintenance_refresh import gate_at

    class SymptomModel(AssessmentModel):
        async def complete(self, **kwargs):
            assert kwargs["response_schema"]["title"] == "IssueRequirementExtraction"
            return ModelReply(
                json.dumps(
                    {
                        "goal": "Fix pagination",
                        "requirements": [{"id": "R1", "text": "The first page currently skips items."}],
                        "constraints": [],
                        "signature_changes": [],
                    }
                )
            )

    gate = gate_at(tmp_path, SymptomModel())
    gate.capture_user_input("task", "Fix pagination")
    with pytest.raises(GateError, match="reported symptoms remain as background"):
        await gate.update_brief()
    goal = gate.session.goals.snapshot()
    assert not requirement_entries(goal)
    assert any(item.tag == "symptom" and "currently skips" in item.content for item in goal.conclusions)
    restored = gate_at(tmp_path, SymptomModel())
    await restored.ready()
    assert restored.session.goals.snapshot() == goal


def test_repository_source_marker_does_not_override_latest_scope():
    requirement = "Empty input must return an empty list."
    constraint = "Preserve the existing function signature."
    result = _interactive_contract(
        IssueExtraction(
            requirements=(ExtractedRequirement(requirement),), constraints=(ExtractedConstraint(constraint),)
        ),
        ("Follow README; only add tests.",),
        requirement + "\n" + constraint,
    )
    assert result.requirements[0].text == "(repo) " + requirement
    assert result.constraints[0].text == "(repo) " + constraint


def test_repo_prefix_does_not_hide_defect_or_user_obligation():
    symptom = "The first page currently skips items."
    extraction = IssueExtraction(requirements=(ExtractedRequirement("(repo) " + symptom),))
    result = _interactive_contract(extraction, ("Fix pagination.",), symptom)
    assert not result.requirements
    assert result.symptoms[0].text == "(repo) " + symptom
    result = _interactive_contract(extraction, (symptom,), symptom)
    assert result.requirements == extraction.requirements
    assert not result.symptoms


def test_existing_fail_fast_behavior_is_not_assumed_to_be_a_defect():
    behavior = "The parser currently fails fast on malformed inputs."
    extraction = IssueExtraction(requirements=(ExtractedRequirement(behavior),))
    result = _interactive_contract(extraction, ("Preserve compatibility.",), "")
    assert result.requirements == extraction.requirements
    assert not result.symptoms
