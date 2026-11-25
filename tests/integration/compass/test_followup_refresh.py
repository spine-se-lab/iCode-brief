"""Two-turn pagination regression: refresh the contract before returning a Brief."""

import json
from dataclasses import replace

import pytest

from compass.claims.store import ClaimDelta
from compass.gate.controller import GateError
from compass.ports import ModelReply
from compass.task_contract.extraction import requirement_entries
from tests.integration.compass.test_contract_assessment import AssessmentModel
from tests.integration.compass.test_maintenance_refresh import gate_at, observation

FIRST = "这个项目的分页有问题：第一页会跳过前面的元素。请先读 README、实现和测试，复现问题并说明原因，暂时不要修改文件。保留现有函数签名，不添加依赖，也不要修改输入列表。"  # noqa: RUF001
SECOND = "现在修复它。另外补上超出最后一页返回空列表、负数页码和负数页大小抛 ValueError、原列表不被修改这几个测试。已有测试不能删或放宽。跑完全部测试，说明改动和结果。"  # noqa: RUF001


class DialogueModel(AssessmentModel):
    def __init__(self):
        super().__init__()
        self.fail = False
        self.dialogues = []

    async def complete(self, *, system_prompt, user_prompt, response_schema, max_output_tokens, timeout_seconds):
        if response_schema["title"] != "IssueRequirementExtraction":
            return await super().complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        if self.fail:
            raise RuntimeError("synthetic extraction failure")
        self.extractions += 1
        self.dialogues.append((system_prompt, user_prompt))
        revised = SECOND in user_prompt
        return ModelReply(
            json.dumps(
                {
                    "goal": "Fix pagination and test boundaries" if revised else "Reproduce pagination without edits",
                    "requirements": [
                        {
                            "id": "R1",
                            "text": "First page and boundary tests must pass"
                            if revised
                            else "Reproduce and explain the pagination failure",
                        }
                    ],
                    "constraints": [
                        {"id": "K1", "text": "保留现有函数签名。"},
                        {"id": "K2", "text": "不添加依赖。"},
                        {"id": "K3", "text": "不要修改输入列表。"},
                        *([] if revised else [{"id": "K4", "text": "暂时不要修改文件。"}]),
                    ],
                    "signature_changes": [],
                },
                ensure_ascii=False,
            )
        )


async def initial(path):
    model = DialogueModel()
    gate = gate_at(path, model)
    gate.capture_user_input("first", FIRST)
    gate.observe(observation("test"))
    result = await gate.update_brief()
    assert not result["maintenance_error"]
    return gate, model


@pytest.mark.asyncio
async def test_brief_refreshes_followup_without_requirements_tool_and_survives_restart(tmp_path):
    gate, model = await initial(tmp_path)
    gate.capture_user_input("second", SECOND)
    result = await gate.update_brief()
    assert result["requirements_refreshed"] is True
    assert result["reason"] == "requirements_changed"
    assert not result["maintenance_error"]
    assert "Fix pagination" in result["brief"]
    rows = str(requirement_entries(gate.session.goals.snapshot()))
    assert "暂时不要修改文件" not in rows
    assert "保留现有函数签名" in rows and "不要修改输入列表" in rows
    assert "已有测试不能删或放宽" in rows
    system, prompt = model.dialogues[-1]
    assert "<ordered_user_messages>" in prompt
    assert FIRST in prompt and SECOND in prompt
    assert "supersede" in system
    count = model.extractions
    restored = gate_at(tmp_path, model)
    assert (await restored.update_brief())["requirements_refreshed"] is False
    assert model.extractions == count


@pytest.mark.asyncio
async def test_failed_followup_keeps_previous_contract_and_retries(tmp_path):
    gate, model = await initial(tmp_path)
    previous = gate.session.goals.snapshot()
    cursor = gate.session.claims.snapshot().state.processed_evidence_seq
    gate.capture_user_input("second", SECOND)
    model.fail = True
    with pytest.raises(GateError, match="previous contract retained"):
        await gate.update_brief()
    assert gate.session.goals.snapshot() == previous
    assert gate.session.claims.snapshot().state.processed_evidence_seq == cursor
    assert gate.requirements_outdated(gate.session)
    model.fail = False
    assert (await gate.update_brief())["requirements_refreshed"] is True


@pytest.mark.asyncio
async def test_contract_replacement_clears_only_requirement_conflicts_and_keeps_archives(tmp_path):
    gate, _ = await initial(tmp_path)
    claims = gate.session.claims
    claims._snapshot = replace(
        claims.snapshot(),
        claims=(*claims.snapshot().claims, replace(claims.snapshot().claims[0], id="C-002")),
        state=replace(claims.snapshot().state, active_ids=("C-001", "C-002")),
        conflict_notes=(
            ("R1", "C-001", "old readonly conflict"),
            ("C-001", "C-002", "claim conflict"),
            ("K4", "C-001", "old constraint conflict"),
        ),
    )
    folder = gate.directory / "ledger" / "claims" / "briefs"
    before = {p.name: p.read_bytes() for p in folder.glob("*.txt")}
    assert before
    await claims.commit(
        ClaimDelta(),
        phase="task-update",
        evidence_delta_ids=frozenset(),
        processed_evidence_seq=claims.snapshot().state.processed_evidence_seq,
        processed_step=claims.snapshot().state.processed_step,
        replace_requirement_coverage=True,
        append_brief=False,
    )
    assert claims.snapshot().conflict_notes == (("C-001", "C-002", "claim conflict"),)
    assert {p.name: p.read_bytes() for p in folder.glob("*.txt")} == before


@pytest.mark.asyncio
async def test_reading_more_code_does_not_reextract_unchanged_user_task(tmp_path):
    gate, model = await initial(tmp_path)
    count = model.extractions
    gate.observe(observation())
    assert (await gate.update_brief())["requirements_refreshed"] is False
    assert model.extractions == count


@pytest.mark.asyncio
async def test_native_agent_receives_refreshed_brief_without_explicit_requirements_call(tmp_path, monkeypatch):
    from chrys.service.llm.mock import MockResponse
    from compass.adapters.icode.interactive_model import SelectedModel
    from tests.integration.compass.support import host_for

    host, client, _, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [
            MockResponse(tool_calls=[("update_compass_brief", "brief-first", {})]),
            MockResponse(text="diagnosed"),
            MockResponse(tool_calls=[("update_compass_brief", "brief-second", {})]),
            MockResponse(text="fixed"),
        ],
    )
    model = DialogueModel()

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
        await host.run_until_final(FIRST, timeout=10)
        first = requests[-1][-1]
        assert first.call_id == "brief-first"
        assert "Reproduce pagination" in str(first.result)
        await host.run_until_final(SECOND, timeout=10)
        second = requests[-1][-1]
        assert second.call_id == "brief-second"
        assert "Fix pagination" in str(second.result)
        assert "暂时不要修改文件" not in str(second.result)
        assert model.extractions == 2
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_old_policy_checkpoint_refreshes_once_even_with_same_input(tmp_path):
    from compass.gate.controller import STATE

    gate, model = await initial(tmp_path)
    gate.state()[STATE]["contract_policy_version"] = 1
    gate.save()
    count = model.extractions
    restored = gate_at(tmp_path, model)
    assert (await restored.update_brief())["requirements_refreshed"] is True
    assert model.extractions == count + 1
    assert (await restored.update_brief())["requirements_refreshed"] is False
