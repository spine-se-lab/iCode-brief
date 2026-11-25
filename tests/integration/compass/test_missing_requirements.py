"""Missing task contracts are initialized before policy-driven Brief maintenance."""

import json
from dataclasses import replace

import pytest

from chrys.extensions import ToolError
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.tool_session import STATE
from compass.provenance import SOURCE_ROOT, source_constants
from compass.task_contract.extraction import requirement_entries
from tests.integration.compass.support import ROOT_TASK, host_for, read
from tests.integration.compass.test_captured_requirements import update
from tests.integration.compass.test_tool_interface import brief, query, results, runtime_for


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_count", [0, 4])
async def test_skipped_requirements_tool_initializes_once_then_uses_core_policy(tmp_path, monkeypatch, evidence_count):
    host, client, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [*[read() for _ in range(evidence_count)], brief(), brief(), MockResponse(text="done")],
    )
    try:
        assert (await host.run_until_final(ROOT_TASK, timeout=10)).text == "done"
        core = runtime_for(host).gate.session
        assert core.goals.snapshot().description
        assert any(ref.startswith("R") for ref, _ in requirement_entries(core.goals.snapshot()))
        assert len(probe.extractions) == 1
        payloads = [json.loads(text) for text in results(client) if text.startswith('{"outcome"')]
        assert len(payloads) == 2
        assert payloads[0]["requirements_initialized"] is (evidence_count == 0)
        assert payloads[0]["maintenance_due"] is False
        assert core.claims.snapshot().state.revision == int(evidence_count == 4)
        assert payloads[1]["requirements_initialized"] is False
        assert payloads[1]["maintenance_due"] is False
        if evidence_count == 0:
            assert len(probe.prompts) == 1  # extraction only, no claim maintenance
        else:
            assert "def f(items)" in probe.extractions[0]
        assert len(core.evidence.snapshot()) == evidence_count
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["goal", "requirements"])
async def test_incomplete_contract_is_repaired_even_with_matching_dedup_cache(tmp_path, monkeypatch, missing):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        goal = runtime.gate.session.goals.snapshot()
        broken = (
            replace(goal, description="")
            if missing == "goal"
            else replace(goal, conclusions=tuple(item for item in goal.conclusions if item.tag != "criteria"))
        )
        await runtime.gate.session.goals.replace(broken)
        runtime.gate.save()
        result = json.loads(await runtime.update_brief())
        assert result["requirements_initialized"] is True
        assert result["maintenance_due"] is False
        assert len(probe.extractions) == 2
        repaired = runtime.gate.session.goals.snapshot()
        assert repaired.description and any(ref.startswith("R") for ref, _ in requirement_entries(repaired))
        assert json.loads(await runtime.update_brief())["requirements_initialized"] is False
        assert len(probe.extractions) == 2
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_query_does_not_initialize_or_call_model(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [query(["goal", "R1"]), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        assert not probe.prompts
        assert not runtime_for(host).gate.session.goals.snapshot().description
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_no_original_input_does_not_invent_contract(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        runtime.gate.state()[STATE]["user_inputs"] = []
        with pytest.raises(ToolError, match="No captured user input"):
            await runtime.update_brief()
        assert not probe.prompts
        assert not runtime.gate.session.goals.snapshot().description
    finally:
        await host.shutdown()


def test_recorded_core_provenance_is_valid():
    source_constants(SOURCE_ROOT)
