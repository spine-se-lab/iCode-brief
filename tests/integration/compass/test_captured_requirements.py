"""Original user text and repository-aware extraction through native Extension calls."""

import json

import pytest

from chrys.extensions import ToolError, TurnContext
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.invoker.origin import current_invocation_origin
from chrys.service.llm.mock import MockResponse
from compass import session as core_module
from compass.adapters.icode.tool_session import STATE
from compass.adapters.icode.tools import extension
from compass.provenance import SOURCE_ROOT, sha, source_constants
from compass.state import STATE_KEY
from tests.integration.compass.support import host_for, read
from tests.integration.compass.test_migration import normalized_tree
from tests.integration.compass.test_tool_interface import runtime_for

TASK = "修复空输入。\n保留现有接口，不引入新依赖。"  # noqa: RUF001 -- preserve the original Unicode input


def update():
    return MockResponse(tool_calls=[("update_compass_requirements", "requirements", {})])


@pytest.mark.asyncio
async def test_initial_then_exploration_updates_but_unchanged_call_skips(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [update(), read(), update(), update(), MockResponse(text="done")]
    )
    try:
        assert (await host.run_until_final(TASK, timeout=10)).text == "done"
        runtime = runtime_for(host)
        assert [item["text"] for item in runtime.gate.state()[STATE]["user_inputs"]] == [TASK]
        assert len(probe.extractions) == 2
        assert all(TASK in prompt for prompt in probe.extractions)
        assert "<repository_context>" not in probe.extractions[0]
        assert "def f(items): return list(items)" in probe.extractions[1]
        assert len(runtime.gate.session.evidence.snapshot()) == 1
        assert runtime.gate.session.claims.snapshot().state.last_maintenance_attempt_step == 0
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_capture_alone_calls_no_model_and_followup_keeps_original(tmp_path, monkeypatch):
    followup = "Also support empty strings."
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [MockResponse(text="understood"), update(), MockResponse(text="updated")]
    )
    try:
        await host.run_until_final(TASK, timeout=10)
        assert not probe.prompts
        await host.run_until_final(followup, timeout=10)
        runtime = runtime_for(host)
        assert [item["text"] for item in runtime.gate.state()[STATE]["user_inputs"]] == [TASK, followup]
        assert len(probe.extractions) == 1
        assert TASK in probe.extractions[0] and followup in probe.extractions[0]
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_retry_does_not_duplicate_original_input(tmp_path, monkeypatch, stored):
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [update(), MockResponse(text="done")], failures={1}, stored=stored
    )
    try:
        assert (await host.run_until_final(TASK, timeout=10)).text == "done"
        assert len(runtime_for(host).gate.state()[STATE]["user_inputs"]) == 1
        assert len(probe.extractions) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_checkpoint_preserves_inputs_and_extraction_deduplication(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [update(), read(), update(), MockResponse(text="done")]
    )
    try:
        await host.run_until_final(TASK, timeout=10)
        runtime = runtime_for(host)
        runtime.gate.state()[STATE] = json.loads(json.dumps(runtime.gate.state()[STATE]))
        runtime.gate.session = None
        await runtime.update_requirements()
        assert len(probe.extractions) == 2
        assert runtime.gate.state()[STATE]["user_inputs"][0]["text"] == TASK
        assert len(runtime.gate.session.evidence.snapshot()) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_failed_extraction_does_not_cache_success(tmp_path, monkeypatch):
    original = core_module.extract_issue_structure
    attempts = []

    async def fail_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("synthetic extraction failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(core_module, "extract_issue_structure", fail_once)
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [update(), update(), update(), MockResponse(text="done")]
    )
    try:
        await host.run_until_final(TASK, timeout=10)
        assert len(attempts) == 2
        assert len(probe.extractions) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_child_cannot_capture_prompt_into_main_task(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(TASK, timeout=10)
        runtime = runtime_for(host)
        before = json.dumps(runtime.gate.state()[STATE])
        context = TurnContext.for_testing(
            tmp_path, prompt="child instruction", session_id=runtime.session_id, session_data_dir=runtime.directory
        )
        origin = InvocationOrigin(kind="sub_agent", session_id=runtime.session_id, invocation_id="child", parent=None)
        token = current_invocation_origin.set(origin)
        try:
            assert await extension.contexts[0].func(context) is None
            with pytest.raises(ToolError):
                runtime.capture_user_input(context.prompt, origin)
        finally:
            current_invocation_origin.reset(token)
        assert json.dumps(runtime.gate.state()[STATE]) == before
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_invalid_dedup_state_rejected_before_live_state_changes(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    try:
        await host.run_until_final(TASK, timeout=10)
        core = runtime_for(host).gate.session
        before = core.goals.snapshot()
        snapshot = json.loads(json.dumps(core.contribute_session_state()))
        snapshot[STATE_KEY]["extracted_input"] = [123, "context"]
        with pytest.raises(ValueError, match="invalid task extraction input"):
            await core.restore_session_state(snapshot)
        assert core.goals.snapshot() == before
    finally:
        await host.shutdown()


def test_explicit_approved_core_behavior_deltas():
    source_constants(SOURCE_ROOT)
    manifest = json.loads((SOURCE_ROOT / "provenance.json").read_text())
    changed = {path for path, record in manifest["files"].items() if "adapted_ast_sha256" in record}
    assert changed == {
        "session.py",
        "claims/maintenance.py",
        "claims/store.py",
        "task_contract/extraction.py",
        "briefing/render.py",
        "briefing/evidence.py",
        "claims/inspection.py",
        "evidence/observation.py",
    }
    for path, record in manifest["files"].items():
        assert sha(normalized_tree((SOURCE_ROOT / path).read_text())) == record.get(
            "adapted_ast_sha256", record["original_ast_sha256"]
        )
    assert len(manifest["original"]["files_sha256"]) == 72


@pytest.mark.asyncio
async def test_new_user_requirement_updates_without_new_repository_evidence(tmp_path, monkeypatch):
    followup = "Also support empty strings."
    host, _, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [update(), MockResponse(text="created"), update(), update(), MockResponse(text="updated")],
    )
    try:
        await host.run_until_final(TASK, timeout=10)
        await host.run_until_final(followup, timeout=10)
        assert len(probe.extractions) == 2
        assert TASK in probe.extractions[1] and followup in probe.extractions[1]
        assert not runtime_for(host).gate.session.evidence.snapshot()
    finally:
        await host.shutdown()
