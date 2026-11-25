"""Flat gate architecture: native tools, independent state and unchanged algorithms."""

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.storage import Artifacts
from compass.cli import prepare
from compass.evidence.observation import ToolObservation
from compass.gate.controller import STATE, CompassGate, GateError
from compass.provenance import SOURCE_ROOT, sha, source_constants
from tests.integration.compass.support import ROOT_TASK, MaintenanceProbe, host_for, read, write
from tests.integration.compass.test_captured_requirements import update
from tests.integration.compass.test_migration import normalized_tree
from tests.integration.compass.test_tool_interface import brief, results, runtime_for


def test_flat_layout_preserves_all_recorded_algorithm_bodies_and_prompts():
    assert not (SOURCE_ROOT / "core").exists()
    assert not (SOURCE_ROOT / "runtime").exists()
    manifest = json.loads((SOURCE_ROOT / "provenance.json").read_text())
    assert len(manifest["files"]) == 36
    for path, record in manifest["files"].items():
        assert sha(normalized_tree((SOURCE_ROOT / path).read_text())) == record.get(
            "adapted_ast_sha256", record["original_ast_sha256"]
        ), path
    assert {name: sha(value) for name, value in source_constants(SOURCE_ROOT).items()} == manifest["submission_sha256"]


def test_gate_and_business_modules_do_not_depend_on_host_or_evaluation():
    for path in SOURCE_ROOT.rglob("*.py"):
        if (
            "adapters" in path.parts
            or "evaluation" in path.parts
            or path.name in {"cli.py", "__main__.py", "provenance.py"}
        ):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            assert not any(
                name.startswith(("chrys", "openai", "compass.adapters", "compass.evaluation")) for name in names
            ), path
    adapter = (SOURCE_ROOT / "adapters/icode/tool_session.py").read_text()
    assert "._trigger" not in adapter and ".maintain(" not in adapter and ".start(" not in adapter


def test_gate_import_and_verification_do_not_load_harness_or_icode(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from compass.gate.controller import CompassGate; "
                "from compass.provenance import verify_source, SOURCE_ROOT; verify_source(SOURCE_ROOT); "
                "import sys; assert not any(n == 'chrys' or n.startswith(('chrys.', 'compass.evaluation')) for n in sys.modules)"
            ),
        ],
        stdin=subprocess.DEVNULL,
        env={**os.environ, **{key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA")}},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_native_adapter_routes_initial_and_repository_updates_through_gate(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [update(), read(), update(), update(), brief(), MockResponse(text="done")]
    )
    try:
        assert (await host.run_until_final(ROOT_TASK, timeout=10)).text == "done"
        gate = runtime_for(host).gate
        assert len(probe.extractions) == 2
        assert all(ROOT_TASK in prompt for prompt in probe.extractions)
        assert "def f(items)" in probe.extractions[-1]
        assert len(gate.session.evidence.snapshot()) == 1
        assert gate.state()[STATE]["user_inputs"][0]["text"] == ROOT_TASK
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 4])
async def test_native_brief_initializes_missing_contract_then_obeys_gate(tmp_path, monkeypatch, count):
    host, client, probe, _, _ = await host_for(
        tmp_path, monkeypatch, [*[read() for _ in range(count)], brief(), brief(), MockResponse(text="done")]
    )
    try:
        assert (await host.run_until_final(ROOT_TASK, timeout=10)).text == "done"
        payloads = [json.loads(t) for t in results(client) if t.startswith('{"outcome"')]
        assert len(probe.extractions) == 1
        assert [p["requirements_initialized"] for p in payloads] == [count == 0, False]
        assert [p["maintenance_due"] for p in payloads] == [False, False]
        assert runtime_for(host).gate.session.claims.snapshot().state.revision == int(count == 4)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_native_retry_and_ordinary_write_still_belong_to_host(tmp_path, monkeypatch, stored):
    host, client, probe, _, writes = await host_for(
        tmp_path, monkeypatch, [write(), MockResponse(text="done")], failures={1}, stored=stored
    )
    try:
        assert (await host.run_until_final(ROOT_TASK, timeout=10)).text == "done"
        gate = runtime_for(host).gate
        assert writes == ["a.py"] and not probe.prompts
        assert len(gate.state()[STATE]["user_inputs"]) == 1
        assert not gate.state()[STATE]["pending"]
        assert len(gate.session.evidence.snapshot()) == 1
        assert len(client.requests) == 3  # one retry, one write, one final response
    finally:
        await host.shutdown()


def new_gate(tmp_path, model):
    gate = CompassGate(
        model=model, artifacts=Artifacts(), directory=tmp_path / "compass", cwd=tmp_path, session_id="test"
    )
    gate.bind()
    return gate


@pytest.mark.asyncio
async def test_direct_gate_works_and_restores_without_host_session(tmp_path):
    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    gate.capture_user_input("1", ROOT_TASK)
    await gate.update_requirements()
    gate.observe(
        ToolObservation(
            tool_name="read",
            tool_kind="filesystem.read",
            arguments={"path": "a.py"},
            result="def f(items): return list(items)",
            working_dir=str(tmp_path),
            phase="test",
        )
    )
    await gate.aclose()
    restored = new_gate(tmp_path, model)
    try:
        assert "def f(items)" in await restored.inspect(["E-001"])
        assert len(model.extractions) == 1  # query/restoration does not extract
        await restored.update_requirements()
        await restored.update_requirements()
        assert len(model.extractions) == 2  # changed repository context only
        assert len(restored.state()[STATE]["user_inputs"]) == 1
        assert (tmp_path / "compass/session.json").exists()
    finally:
        await restored.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["goal", "requirements"])
async def test_gate_repairs_partial_contract_despite_matching_cached_input(tmp_path, missing):
    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    gate.capture_user_input("1", ROOT_TASK)
    try:
        await gate.update_requirements()
        goal = gate.session.goals.snapshot()
        broken = (
            replace(goal, description="")
            if missing == "goal"
            else replace(goal, conclusions=tuple(item for item in goal.conclusions if item.tag != "criteria"))
        )
        await gate.session.goals.replace(broken)
        gate.save()
        result = await gate.update_brief()
        assert result["requirements_initialized"] and not result["maintenance_due"]
        assert len(model.extractions) == 2
    finally:
        await gate.aclose()


@pytest.mark.asyncio
async def test_gate_without_user_input_never_invents_task(tmp_path):
    model = MaintenanceProbe()
    gate = new_gate(tmp_path, model)
    try:
        await gate.inspect(["goal"])
        with pytest.raises(GateError, match="No captured user input"):
            await gate.update_brief()
        assert not model.prompts
    finally:
        await gate.aclose()


def test_independent_evaluation_preparation_still_works_offline(tmp_path):
    assert source_constants(SOURCE_ROOT)
    directory = prepare(tmp_path)
    assert (directory / "agents/CompassCode.yaml").is_file()
    assert (directory / "models/dee05ee03600.yaml").is_file()
