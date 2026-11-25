"""Regression checks for contract provenance and explicit coverage assessment."""

import json
from dataclasses import replace

import pytest

from compass.claims.maintenance import _validate_requirement_assessment
from compass.claims.models import ClaimSnapshot, ClaimState
from compass.claims.store import ClaimDelta
from compass.model_calls.policy import ResponseSchemaError
from compass.ports import ModelReply
from compass.task_contract.extraction import _checked_issue_extraction
from tests.integration.compass.test_maintenance_refresh import gate_at, observation


class AssessmentModel:
    def __init__(self, failures=0, empty=False):
        self.failures = failures
        self.empty = empty
        self.calls = []
        self.extractions = 0
        self.extraction_system = ""

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        title = response_schema["title"]
        if title == "IssueRequirementExtraction":
            self.extractions += 1
            self.extraction_system = system_prompt
            return ModelReply(
                json.dumps(
                    {
                        "goal": "Check pagination",
                        "requirements": [{"id": "R1", "text": "pagination tests must pass"}],
                        "constraints": [],
                        "signature_changes": [],
                    }
                )
            )
        if title != "ClaimMaintenanceDelta":
            return ModelReply(json.dumps({key: [] for key in response_schema["required"]}))
        data, _ = json.JSONDecoder().raw_decode(user_prompt.split("INPUT:\n", 1)[1])
        self.calls.append(data)
        payload = {key: [] for key in response_schema["required"]}
        if self.empty:
            ref = None
        elif data["current_claims"]:
            ref = data["current_claims"][0]["id"]
        else:
            item = (data["evidence_delta"] or data["retrieved_evidence"])[0]
            payload["new_claims"] = [
                {
                    "statement": "pagination tests passed",
                    "kind": "observation",
                    "evidence_refs": [item["id"]],
                    "evidence_digest": "Tests passed",
                    "verdict": "supported",
                    "assumes_external_provision": False,
                }
            ]
            ref = "<new:0>"
        if len(self.calls) > self.failures:
            payload["requirement_coverage"] = [
                {"requirement_id": row["id"], "covered_by": [ref] if ref else []}
                for row in data["goal"]["requirements"]
            ]
        return ModelReply(json.dumps(payload))


async def setup_gate(tmp_path, model):
    gate = gate_at(tmp_path, model)
    gate.capture_user_input("task", "pagination tests must pass")
    await gate.update_requirements()
    gate.observe(observation("test"))
    return gate


def test_chinese_constraints_are_split_and_where_annotation_deduplicated():
    issue = "分页有问题。请先复现并解释原因。暂时不要修改文件。保留现有函数签名。"
    response = {
        "goal": "Explain the issue",
        "requirements": [],
        "signature_changes": [],
        "constraints": [
            {"id": "K1", "text": "暂时不要修改文件。 [where: pagination.py]"},
            {"id": "K2", "text": "保留现有函数签名。 [where: pagination.py paginate]"},
        ],
    }
    result = _checked_issue_extraction(issue, json.dumps(response))
    assert [entry.text for entry in result.constraints] == [entry["text"] for entry in response["constraints"]]
    assert all("分页有问题" not in entry.text for entry in result.constraints)


@pytest.mark.asyncio
async def test_missing_assessment_is_repaired_before_committing(tmp_path):
    model = AssessmentModel(failures=1)
    gate = await setup_gate(tmp_path, model)
    result = await gate.update_brief()
    assert len(model.calls) == 2
    assert result["processed_evidence_seq"] == 1
    assert not result["maintenance_error"]
    assert gate.session.claims.snapshot().state.requirement_coverage["R1"].claim_ids == ("C-001",)
    assert len(gate.session.claims.snapshot().claims) == 1
    assert (await gate.update_brief())["maintenance_due"] is False


@pytest.mark.asyncio
async def test_repeated_missing_assessment_retains_evidence_for_retry(tmp_path):
    model = AssessmentModel(failures=99)
    gate = await setup_gate(tmp_path, model)
    result = await gate.update_brief()
    assert result["maintenance_error"]
    assert result["processed_evidence_seq"] == 0
    assert not gate.session.claims.snapshot().claims
    assert len(model.calls) == 2
    model.failures = 0
    assert (await gate.update_brief())["processed_evidence_seq"] == 1


@pytest.mark.asyncio
async def test_explicit_empty_assessment_is_valid_and_does_not_invent_support(tmp_path):
    gate = await setup_gate(tmp_path, AssessmentModel(empty=True))
    result = await gate.update_brief()
    assert not result["maintenance_error"]
    row = gate.session.claims.snapshot().state.requirement_coverage["R1"]
    assert row.claim_ids == () and row.level == "none"
    assert not gate.session.claims.snapshot().claims
    assert (await gate.update_brief())["maintenance_due"] is False


@pytest.mark.asyncio
async def test_already_processed_checkpoint_without_coverage_can_be_reassessed(tmp_path):
    model = AssessmentModel()
    gate = await setup_gate(tmp_path, model)
    await gate.update_brief()
    snapshot = gate.session.claims.snapshot()
    # Simulate the observed old checkpoint: processed evidence and active claims,
    # but no accepted R/K association rows and requirements_pending=false.
    gate.session.claims._snapshot = replace(snapshot, state=replace(snapshot.state, requirement_coverage={}))
    gate.save()
    restored = gate_at(tmp_path, model)
    result = await restored.update_brief()
    assert result["reason"] == "requirements_unassessed"
    assert model.calls[-1]["evidence_delta"] == []
    assert restored.session.claims.snapshot().state.requirement_coverage["R1"].claim_ids
    assert result["processed_evidence_seq"] == 1


@pytest.mark.asyncio
async def test_policy_upgrade_reextracts_once_without_new_user_message(tmp_path):
    model = AssessmentModel()
    gate = await setup_gate(tmp_path, model)
    await gate.update_requirements()
    calls = model.extractions
    del gate.state()["compass_tools"]["contract_policy_version"]
    gate.save()
    restored = gate_at(tmp_path, model)
    await restored.update_requirements()
    assert model.extractions == calls + 1
    assert "baseline failures and current test outcomes" in model.extraction_system
    await restored.update_requirements()
    assert model.extractions == calls + 1


@pytest.mark.asyncio
async def test_ordinary_read_still_waits_for_original_batch_window(tmp_path):
    model = AssessmentModel()
    gate = gate_at(tmp_path, model)
    gate.capture_user_input("task", "pagination tests must pass")
    await gate.update_requirements()
    gate.observe(observation())
    assert (await gate.update_brief())["maintenance_due"] is False
    assert model.calls == []


@pytest.mark.parametrize("rows", [(), (("R1", ("C-999",)),), (("R2", ()),), (("R1", ()), ("R1", ()))])
def test_incomplete_unknown_or_duplicate_links_fail_validation(rows):
    with pytest.raises(ResponseSchemaError):
        _validate_requirement_assessment(
            ClaimDelta(requirement_coverage=rows), frozenset({"R1"}), ClaimSnapshot(ClaimState())
        )


def test_literal_chinese_bundle_already_represented_is_not_added_again():
    issue = "请先读 README、实现和测试，复现问题并说明原因，暂时不要修改文件。保留现有函数签名，不添加依赖，也不要修改输入列表。"  # noqa: RUF001
    constraints = ["保留现有函数签名", "不添加依赖", "也不要修改输入列表", "暂时不要修改文件"]
    payload = {
        "goal": "Explain pagination",
        "requirements": [{"id": "R1", "text": issue.split("。")[0] + "。 [where: pagination.py]"}],
        "constraints": [{"id": f"K{i}", "text": text} for i, text in enumerate(constraints, 1)],
        "signature_changes": [],
    }
    result = _checked_issue_extraction(issue, json.dumps(payload))
    assert [item.text for item in result.constraints] == constraints


def test_deduplication_does_not_drop_an_unrepresented_condition():
    issue = "如果调用失败，不要修改输入列表。"  # noqa: RUF001
    payload = {
        "goal": "Handle failure",
        "requirements": [],
        "constraints": [{"id": "K1", "text": "不要修改输入列表"}],
        "signature_changes": [],
    }
    result = _checked_issue_extraction(issue, json.dumps(payload))
    assert issue in [item.text for item in result.constraints]


@pytest.mark.asyncio
async def test_read_only_extraction_marks_user_input_separately_from_repo_context():
    from compass.task_contract.extraction import extract_issue_structure

    class CapturingModel(AssessmentModel):
        async def complete(self, **kwargs):
            self.prompt = kwargs["user_prompt"]
            return await super().complete(**kwargs)

    model = CapturingModel()
    await extract_issue_structure(
        model,
        "Explain only; do not edit.",
        timeout_seconds=10,
        repository_context="README says fix pagination",
    )
    assert "<original_issue>\nExplain only; do not edit.\n</original_issue>" in model.prompt
    assert "<repository_context>\nREADME says fix pagination" in model.prompt
    assert "not necessarily a code change" in model.extraction_system
    assert "original user's language" in model.extraction_system
