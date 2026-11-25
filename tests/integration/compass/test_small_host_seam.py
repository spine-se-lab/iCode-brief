"""New checks for the reduced host seam, using native builds and offline models."""

import pytest

from chrys.service.llm.mock import MockResponse
from compass.adapters.icode import host as compass_host
from compass.adapters.icode.tool_session import STATE, command_session
from tests.integration.compass.support import host_for, read
from tests.integration.compass.test_tool_interface import runtime_for, update


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_native_build_preserves_capture_retry_exploration_and_cleanup(tmp_path, monkeypatch, stored):
    host, _, probe, _, _ = await host_for(
        tmp_path,
        monkeypatch,
        [update(), read(), update(), update(), MockResponse(text="done")],
        failures={1},
        stored=stored,
    )
    runtime = None
    try:
        assert (await host.run_until_final("Fix empty input", timeout=10)).text == "done"
        runtime = runtime_for(host)
        assert runtime.model.profile.id == "mock-profile"
        assert [item["text"] for item in runtime.gate.state()[STATE]["user_inputs"]] == ["Fix empty input"]
        assert len(probe.extractions) == 2
        assert len(runtime.gate.session.evidence.snapshot()) == 1
        assert command_session(runtime.directory, runtime.session_id) is runtime
    finally:
        await host.shutdown()
    assert runtime.closed and runtime.model.closed
    assert command_session(runtime.directory, runtime.session_id) is None


@pytest.mark.asyncio
async def test_disabled_native_build_does_not_construct_compass(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="ordinary")])
    host._agent_registry.resolve_selector("Headless").extensions.clear()
    try:
        assert (await host.run_until_final("Explain", timeout=10)).text == "ordinary"
        directory = host.engine.session_dir / "extensions" / "compass"
        assert command_session(directory, host.engine.session_id) is None
        assert not directory.exists()
        assert not probe.prompts
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_later_build_failure_closes_prepared_compass(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="unused")])
    original = compass_host.prepare
    captured = []

    async def fail_after_prepare(model, extensions, prepared, report_usage, origin):
        await original(model, extensions, prepared, report_usage, origin)
        where = next(c for c in extensions.contexts if c.extension_id == "compass").turn_context("")
        captured.append(command_session(where.session_data_dir, where.session_id))
        raise RuntimeError("synthetic later build failure")

    monkeypatch.setattr(compass_host, "prepare", fail_after_prepare)
    try:
        with pytest.raises(RuntimeError, match="synthetic later build failure"):
            await host.start()
        assert len(captured) == 1 and captured[0].closed and captured[0].model.closed
        assert command_session(captured[0].directory, captured[0].session_id) is None
    finally:
        await host.shutdown()
