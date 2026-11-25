"""Explicit tool maintenance must not strand completed tests or revised requirements."""

import json
from unittest.mock import AsyncMock

import pytest

from compass.adapters.icode.storage import Artifacts
from compass.evidence.observation import ToolObservation
from compass.gate.controller import STATE, CompassGate
from compass.ports import ModelReply
from compass.provenance import SOURCE_ROOT, source_constants


class Model:
    def __init__(self):
        self.maintenance = []
        self.extractions = 0

    async def complete(self, *, user_prompt, response_schema, **kwargs):
        title = response_schema["title"]
        if title == "IssueRequirementExtraction":
            self.extractions += 1
            return ModelReply(
                json.dumps(
                    {
                        "requirements": [{"id": "R1", "text": "pagination tests must pass"}],
                        "constraints": [],
                        "signature_changes": [],
                    }
                )
            )
        if title != "ClaimMaintenanceDelta":
            return ModelReply(json.dumps({k: [] for k in response_schema.get("required", [])}))
        data = json.loads(user_prompt.split("INPUT:\n", 1)[1])
        self.maintenance.append(data)
        payload = {k: [] for k in response_schema["required"]}
        if data["evidence_delta"]:
            item = data["evidence_delta"][-1]
            payload["new_claims"] = [
                {
                    "statement": "pagination tests passed",
                    "kind": "observation",
                    "evidence_refs": [item["id"]],
                    "evidence_digest": "pagination tests passed",
                    "verdict": "supported",
                    "assumes_external_provision": False,
                }
            ]
            ref = "<new:0>"
        else:
            ref = data["current_claims"][0]["id"]
        payload["requirement_coverage"] = [{"requirement_id": "R1", "covered_by": [ref]}]
        return ModelReply(json.dumps(payload))


def gate_at(path, model):
    gate = CompassGate(model=model, artifacts=Artifacts(), directory=path / "compass", cwd=path, session_id="refresh")
    gate.bind()
    return gate


def observation(kind="read", exit_code=0):
    if kind == "test":
        return ToolObservation(
            tool_name="shell",
            tool_kind="shell",
            arguments={"command": "python -m unittest -v"},
            result="pagination tests passed\nRan 10 tests\nOK",
            exit_code=exit_code,
        )
    return ToolObservation(
        tool_name="read_file",
        tool_kind="filesystem.read",
        arguments={"path": "pagination.py"},
        result="pagination tests passed",
    )


async def initialized(path):
    model = Model()
    gate = gate_at(path, model)
    gate.capture_user_input("first", "pagination tests must pass")
    await gate.update_requirements()
    return gate, model


@pytest.mark.asyncio
async def test_short_task_test_result_is_processed_once_without_revising_requirements(tmp_path):
    gate, model = await initialized(tmp_path)
    for _ in range(4):
        gate.observe(observation())
    await gate.update_brief()
    assert gate.session.claims.snapshot().state.processed_evidence_seq == 4
    gate.observe(observation("test"))
    before = len(model.maintenance)
    result = await gate.update_brief()
    assert result["reason"] == "test_result" and result["outcome"] == "updated"
    assert result["processed_evidence_seq"] == 5
    assert len(model.maintenance) == before + 1
    assert model.maintenance[-1]["evidence_delta"][-1]["exit_code"] == 0
    assert gate.session.claims.snapshot().state.requirement_coverage["R1"].claim_ids
    assert (await gate.update_brief())["maintenance_due"] is False
    assert len(model.maintenance) == before + 1


@pytest.mark.asyncio
async def test_revised_requirements_reassess_processed_evidence_after_restart(tmp_path):
    gate, model = await initialized(tmp_path)
    gate.observe(observation("test"))
    await gate.update_brief()
    evidence = gate.session.evidence.snapshot()
    # Repository context changed since the initial extraction; no fresh user
    # message remains to accidentally make the core's no-work guard pass.
    await gate.update_requirements()
    assert gate.state()[STATE]["requirements_pending"] is True
    assert not gate.session.user_inputs.batch()
    assert not gate.session.claims.snapshot().state.requirement_coverage
    gate = gate_at(tmp_path, model)
    result = await gate.update_brief()
    assert result["reason"] == "requirements_changed" and result["outcome"] == "updated"
    assert gate.session.evidence.snapshot() == evidence
    assert model.maintenance[-1]["evidence_delta"] == []
    assert model.maintenance[-1]["retrieved_evidence"]
    assert gate.session.claims.snapshot().state.requirement_coverage["R1"].claim_ids
    calls = len(model.maintenance)
    assert (await gate.update_brief())["maintenance_due"] is False
    assert len(model.maintenance) == calls


@pytest.mark.asyncio
async def test_ordinary_reads_remain_batched_and_observation_is_passive(tmp_path):
    gate, model = await initialized(tmp_path)
    gate.observe(observation())
    assert not model.maintenance
    result = await gate.update_brief()
    assert result["maintenance_due"] is False
    assert result["remaining_evidence_steps"] > 0
    assert not model.maintenance


@pytest.mark.asyncio
async def test_no_evidence_does_not_manufacture_maintenance(tmp_path):
    gate, model = await initialized(tmp_path)
    gate.capture_user_input("followup", "Also keep the API unchanged")
    await gate.update_requirements()
    assert (await gate.update_brief())["maintenance_due"] is False
    assert not model.maintenance


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_refresh_remains_pending(tmp_path, monkeypatch, cancel):
    import asyncio

    gate, _model = await initialized(tmp_path)
    gate.observe(observation("test"))
    await gate.update_brief()
    await gate.update_requirements()
    original = gate.session.maintain
    failure = asyncio.CancelledError if cancel else RuntimeError
    monkeypatch.setattr(gate.session, "maintain", AsyncMock(side_effect=failure("synthetic")))
    with pytest.raises(failure):
        await gate.update_brief()
    assert gate.state()[STATE]["requirements_pending"] is True
    monkeypatch.setattr(gate.session, "maintain", original)
    assert (await gate.update_brief())["outcome"] == "updated"
    assert gate.state()[STATE]["requirements_pending"] is False


@pytest.mark.asyncio
async def test_legacy_checkpoint_with_cleared_coverage_is_repaired(tmp_path):
    gate, model = await initialized(tmp_path)
    gate.observe(observation("test"))
    await gate.update_brief()
    await gate.update_requirements()
    del gate.state()[STATE]["requirements_pending"]
    gate.save()
    restored = gate_at(tmp_path, model)
    assert (await restored.update_brief())["reason"] == "requirements_changed"
    assert restored.session.claims.snapshot().state.requirement_coverage["R1"].claim_ids


def test_core_changes_are_opt_in_and_provenance_matches():
    source_constants(SOURCE_ROOT)
    from inspect import signature

    from compass.claims.maintenance import ClaimMaintenance
    from compass.session import CompassSession

    assert signature(CompassSession._maintain).parameters["reassess_requirements"].default is False
    assert signature(ClaimMaintenance._maintain_batch).parameters["reassess_requirements"].default is False


@pytest.mark.asyncio
async def test_default_core_maintenance_still_skips_without_new_work(tmp_path):
    gate, model = await initialized(tmp_path)
    gate.observe(observation("test"))
    await gate.update_brief()
    before = len(model.maintenance)
    await gate.update_requirements()
    await gate.session.maintain(phase="evaluation")
    assert len(model.maintenance) == before
    assert not gate.session.claims.snapshot().state.requirement_coverage
    assert (await gate.update_brief())["outcome"] == "updated"


@pytest.mark.asyncio
async def test_snapshot_reads_do_not_run_pending_refresh(tmp_path):
    from types import SimpleNamespace

    from compass.adapters.icode.commands import status_snapshot

    gate, model = await initialized(tmp_path)
    gate.observe(observation("test"))
    await gate.flush_observations(gate.session)
    runtime = SimpleNamespace(gate=gate)
    before = (gate.directory / "session.json").read_bytes()
    calls = len(model.maintenance)
    snapshot = status_snapshot(runtime)
    assert snapshot.phase == "ready" and snapshot.reason == "test_result"
    assert snapshot.remaining_steps == 0 and snapshot.processed_evidence == 0
    assert (gate.directory / "session.json").read_bytes() == before
    assert len(model.maintenance) == calls


@pytest.mark.asyncio
async def test_failed_test_is_due_but_never_mechanically_marked_pass(tmp_path):
    gate, model = await initialized(tmp_path)
    gate.observe(observation("test", exit_code=1))
    await gate.flush_observations(gate.session)
    decision = gate.maintenance_decision(gate.session)
    assert decision.trigger and decision.reason == "test_result"
    assert gate.session.evidence.snapshot()[-1].exit_code == 1
    assert not gate.session.claims.snapshot().state.requirement_coverage
    assert not model.maintenance
