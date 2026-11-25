"""Offline adversarial extraction and native delivery checks; no live provider calls."""

# ruff: noqa: RUF001

import json
from dataclasses import replace

import pytest

from compass.gate.controller import STATE, GateError
from compass.ports import ModelReply
from compass.task_contract.extraction import _checked_issue_extraction, _interactive_contract, requirement_entries
from tests.integration.compass.test_contract_assessment import AssessmentModel
from tests.integration.compass.test_followup_refresh import FIRST, SECOND
from tests.integration.compass.test_maintenance_refresh import gate_at, observation

THIRD = "现在任务改为只审查分页实现和测试覆盖，列出尚未覆盖的情况。暂时不要修改任何文件，也不要添加测试。保留函数签名、不添加依赖的约束继续有效。请调用 update_compass_brief 更新并返回当前 Brief。"
FOURTH = "现在允许修改文件，但只补充缺失测试，不改 pagination.py。已有测试不能删除或放宽。请调用 update_compass_brief 更新并返回当前 Brief。"
TURNS = (FIRST, SECOND, THIRD, FOURTH)
GOALS = ("复现并说明分页问题", "修复分页并补充边界测试", "审查实现和测试覆盖", "只补充缺失测试")
BAD = "Page numbers start at 1. The first page currently skips items. [where: pagination.py paginate(items, page=1, page_size=3)]"


def reply(requirement, constraints=(), *more):
    return json.dumps(
        {
            "goal": "Check the task",
            "requirements": [{"id": f"R{i}", "text": text} for i, text in enumerate((requirement, *more), 1)],
            "constraints": [{"id": f"K{i}", "text": text} for i, text in enumerate(constraints, 1)],
            "signature_changes": [],
        },
        ensure_ascii=False,
    )


def interactive(payload, messages=("Review implementation",), repository=""):
    return _interactive_contract(_checked_issue_extraction(messages[-1], payload), messages, repository)


@pytest.mark.parametrize("text", [BAD, "The parser previously crashes on empty input.", "第一页当前会跳过前面的元素。"])
def test_reported_defect_sentence_moves_to_background(text):
    for payload in (reply(text, (), "Review implementation"), reply("Review implementation", (text,))):
        result = interactive(payload)
        texts = [item.text for item in (*result.requirements, *result.constraints)]
        assert not any(
            marker in item for item in texts for marker in ("currently skips", "previously crashes", "当前会跳过")
        )
        assert "Review implementation" in texts
        assert result.symptoms and any(
            part in context.text
            for context in result.symptoms
            for part in ("currently skips", "previously crashes", "当前会跳过")
        )
    assert interactive(reply(BAD, (), "Review implementation")).requirements[0].text == (
        "Page numbers start at 1. [where: pagination.py paginate(items, page=1, page_size=3)]"
    )


def test_evaluation_path_keeps_model_wording():
    assert _checked_issue_extraction("Review implementation", reply(BAD)).requirements[0].text == BAD


def test_repository_copies_keep_obligations_and_annotate_source():
    readme = "4|Page numbers start at 1. The first page currently skips items.\n5|Fix the pagination helper without changing its public signature."
    result = interactive(
        reply(
            "Page numbers start at 1.",
            ("Fix the pagination helper without changing its public signature.",),
            "只补充缺失测试",
        ),
        ("只补充缺失测试，不改 pagination.py。",),
        readme,
    )
    assert [item.text for item in result.requirements] == ["(repo) Page numbers start at 1.", "只补充缺失测试"]
    assert [item.text for item in result.constraints] == [
        "(repo) Fix the pagination helper without changing its public signature."
    ]


@pytest.mark.parametrize(
    "text",
    [
        "The parser must raise ValueError for invalid input.",
        "Reproduce why the first page currently skips items and explain the cause.",
        "Verify that the parser previously failed but now returns an empty list.",
        "当前必须跳过无效元素。",
        "复现第一页当前会跳过元素的问题。",
        "当前已有的缓存数据不能丢失。",
        "原来通过的测试不能失败。",
        "目前的配置项不得被删除或出现错误。",
        "Do not break callers that currently fail fast on bad input.",
    ],
)
def test_normative_errors_and_diagnostic_deliverables_remain_requirements(text):
    assert _checked_issue_extraction("Review implementation", reply(text)).requirements[0].text == text
    assert interactive(reply(text)).requirements[0].text == text


def test_duplicate_catch_all_removed_but_condition_and_missing_clause_retained():
    clauses = ("保留函数签名。", "不添加依赖。", "不要修改输入列表。")
    combined = "保留函数签名、不添加依赖、不要修改输入列表。"
    conditional = "仅在兼容模式下，保留函数签名、不添加依赖、不要修改输入列表。"
    missing = "保留函数签名、不添加依赖、保留文件权限。"
    result = _checked_issue_extraction(
        "Review implementation", reply("Review implementation", (*clauses, combined, conditional, missing))
    )
    texts = [item.text for item in result.constraints]
    assert combined not in texts
    assert all(item in texts for item in (*clauses, conditional, missing))


class FourTurnModel(AssessmentModel):
    def __init__(self):
        super().__init__()
        self.attempts = {}
        self.always_bad = False
        self.prompts = []

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        if response_schema["title"] != "IssueRequirementExtraction":
            return await super().complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        phase = next(i for i in reversed(range(4)) if TURNS[i][:20] in user_prompt)  # tool-call sentences are filtered
        self.attempts[phase] = self.attempts.get(phase, 0) + 1
        self.prompts.append(user_prompt)
        # Every extraction carries the observed bad README sentence; post-processing must preserve it as background.
        if self.always_bad:
            return ModelReply("not json")
        constraints = ["保留函数签名。", "不添加依赖。", "不要修改输入列表。"]
        constraints += {
            0: ["暂时不要修改文件。"],
            1: ["已有测试不能删或放宽。"],
            2: ["暂时不要修改任何文件。", "也不要添加测试。"],
            3: ["不改 pagination.py。", "已有测试不能删除或放宽。"],
        }[phase]
        data = json.loads(reply(BAD, constraints, GOALS[phase]))
        data["goal"] = GOALS[phase]
        return ModelReply(json.dumps(data, ensure_ascii=False))


@pytest.mark.asyncio
async def test_invalid_followup_keeps_contract_cursor_and_policy_until_retry(tmp_path):
    model = FourTurnModel()
    gate = gate_at(tmp_path, model)
    gate.capture_user_input("first", FIRST)
    gate.observe(observation("test"))
    await gate.update_brief()
    old_goal = gate.session.goals.snapshot()
    old_claims = gate.session.claims.snapshot()
    gate.capture_user_input("second", SECOND)
    model.always_bad = True
    with pytest.raises(GateError, match="previous contract retained"):
        await gate.update_brief()
    assert gate.session.goals.snapshot() == old_goal
    assert gate.session.claims.snapshot() == old_claims
    assert gate.requirements_outdated(gate.session)
    model.always_bad = False
    result = await gate.update_brief()
    assert result["requirements_refreshed"]
    assert GOALS[1] in result["brief"]
    assert "Reported symptoms" in result["brief"]
    assert "currently skips" not in str(requirement_entries(gate.session.goals.snapshot()))


@pytest.mark.asyncio
async def test_policy_two_checkpoint_reextracts_and_removes_old_requirement_conflict(tmp_path):
    model = FourTurnModel()
    gate = gate_at(tmp_path, model)
    gate.capture_user_input("first", FIRST)
    gate.observe(observation("test"))
    await gate.update_brief()
    goal = gate.session.goals.snapshot()
    await gate.session.goals.replace(
        replace(
            goal,
            conclusions=tuple(
                replace(item, content=BAD) if item.tag == "criteria" else item for item in goal.conclusions
            ),
        )
    )
    claims = gate.session.claims
    claims._snapshot = replace(claims.snapshot(), conflict_notes=(("R1", "C-001", "old defect conflict"),))
    gate.state()[STATE]["contract_policy_version"] = 2
    gate.save()
    archives = gate.directory / "ledger" / "claims" / "briefs"
    before = {p.name: p.read_bytes() for p in archives.glob("*.txt")}
    restored = gate_at(tmp_path, model)
    result = await restored.update_brief()
    assert result["requirements_refreshed"]
    assert not restored.session.claims.snapshot().conflict_notes
    assert "currently skips" not in str(requirement_entries(restored.session.goals.snapshot()))
    assert restored.state()[STATE]["contract_policy_version"] == 5
    assert all((archives / name).read_bytes() == contents for name, contents in before.items())
    assert not (await restored.update_brief())["requirements_refreshed"]


@pytest.mark.asyncio
async def test_four_turn_contracts_reach_native_agent_next_request(tmp_path, monkeypatch):
    from chrys.service.llm.mock import MockResponse
    from compass.adapters.icode.interactive_model import SelectedModel
    from tests.integration.compass.support import host_for
    from tests.integration.compass.test_tool_interface import runtime_for

    responses = [
        response
        for i in range(4)
        for response in (
            MockResponse(tool_calls=[("update_compass_brief", f"brief-{i}", {})]),
            MockResponse(text="finished"),
        )
    ]
    host, client, _, _, _ = await host_for(tmp_path, monkeypatch, responses)
    model = FourTurnModel()

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        return await model.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(SelectedModel, "complete", complete)
    requests = []
    original = client._inner_get_response

    def capture(*, messages, stream, options, **kwargs):
        requests.append(
            [content for message in messages for content in message.contents if content.type == "function_result"]
        )
        return original(messages=messages, stream=stream, options=options, **kwargs)

    monkeypatch.setattr(client, "_inner_get_response", capture)
    try:
        for i, turn in enumerate(TURNS):
            await host.run_until_final(turn, timeout=10)
            result = requests[-1][-1]
            assert result.call_id == f"brief-{i}"
            brief = str(result.result)
            assert GOALS[i] in brief
            assert "Reported symptoms" in brief
            assert "currently skips" not in str(requirement_entries(runtime_for(host).gate.session.goals.snapshot()))
            assert "不添加依赖" in brief
            if i in (1, 3):
                assert "暂时不要修改" not in brief
            if i == 2:
                assert "暂时不要修改任何文件" in brief and "不要添加测试" in brief
            if i == 3:
                assert "不改 pagination.py" in brief and "不要添加测试" not in brief
        assert model.attempts == {0: 1, 1: 1, 2: 1, 3: 1}
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_old_quote_row_is_dropped_and_window_kept(tmp_path):
    class StaleQuoteModel(AssessmentModel):
        async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
            result = await super().complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
            if response_schema["title"] == "ClaimMaintenanceDelta":
                data = json.loads(result.text)
                data["contradiction_scan"] = [
                    {
                        "requirement_id": "R1",
                        "claim_id": "<new:0>",
                        "verdict": "conflict",
                        "requirement_excerpt": "The first page currently skips items",
                        "claim_excerpt": "pagination tests passed",
                        "resolution": "",
                    }
                ]
                return ModelReply(json.dumps(data))
            return result

    model = StaleQuoteModel()
    gate = gate_at(tmp_path, model)
    gate.capture_user_input("task", "pagination tests must pass")
    gate.observe(observation("test"))
    result = await gate.update_brief()
    assert len(model.calls) == 1
    snapshot = gate.session.claims.snapshot()
    assert not snapshot.conflict_notes
    assert not result["maintenance_error"]
    assert snapshot.claims and snapshot.state.processed_evidence_seq > 0


@pytest.mark.asyncio
async def test_current_requirement_conflict_is_not_suppressed(tmp_path):
    class CurrentConflictModel(AssessmentModel):
        async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
            result = await super().complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
            if response_schema["title"] != "ClaimMaintenanceDelta":
                return result
            data = json.loads(result.text)
            data["new_claims"][0]["statement"] = "pagination tests failed"
            data["new_claims"][0]["evidence_digest"] = "pagination tests failed"
            data["contradiction_scan"] = [
                {
                    "requirement_id": "R1",
                    "claim_id": "<new:0>",
                    "verdict": "conflict",
                    "requirement_excerpt": "pagination tests must pass",
                    "claim_excerpt": "pagination tests failed",
                    "resolution": "",
                }
            ]
            return ModelReply(json.dumps(data))

    model = CurrentConflictModel()
    gate = gate_at(tmp_path, model)
    gate.capture_user_input("task", "pagination tests must pass")
    gate.observe(
        replace(observation("test"), result="pagination tests failed\nRan 10 tests\nFAILED (failures=1)", exit_code=1)
    )
    result = await gate.update_brief()
    assert not result["maintenance_error"]
    assert len(model.calls) == 1
    assert gate.session.claims.snapshot().conflict_notes[0][:2] == ("R1", "C-001")
