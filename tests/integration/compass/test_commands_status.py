"""New offline checks for manual Compass operations and read-only status UI."""

import asyncio
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from textual.app import App
from textual.widgets import Static

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.commands import maintain, status_snapshot
from compass.adapters.icode.tui.command import MaintenanceDialog, dispatch
from compass.adapters.icode.tui.details import DetailScreen
from compass.adapters.icode.tui.status import CompassStatusDialog
from tests.integration.compass.support import ROOT_TASK, host_for
from tests.integration.compass.test_automatic_maintenance import observation
from tests.integration.compass.test_captured_requirements import update
from tests.integration.compass.test_independent_persistence import reopen
from tests.integration.compass.test_tool_interface import runtime_for
from tests.support.tui_app_harness import ShutdownOnlyEngine, make_chrys_app
from tests.support.waiting import wait_for


def files(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_manual_command_enters_both_operations_even_when_nothing_is_due(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        requirements = AsyncMock(wraps=runtime.update_requirements)
        brief = AsyncMock(wraps=runtime.update_brief)
        monkeypatch.setattr(runtime, "update_requirements", requirements)
        monkeypatch.setattr(runtime, "update_brief", brief)
        first = await maintain(runtime)
        second = await maintain(runtime)
        assert requirements.await_count == brief.await_count == 2
        assert first["maintenance_due"] is second["maintenance_due"] is False
        assert len(probe.prompts) == 1  # initial requirement extraction only
        assert runtime.gate.state()["compass_tools"]["user_inputs"][0]["text"] == ROOT_TASK
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_manual_command_initializes_and_checks_due_evidence_once(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        for _ in range(4):
            runtime.gate.observe(observation(tmp_path))
        result = await maintain(runtime)
        assert result["maintenance_due"] is True
        assert len(probe.extractions) == 1
        assert status_snapshot(runtime).queued_observations == 0
        assert (await maintain(runtime))["maintenance_due"] is False
        assert len(probe.extractions) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_status_never_processes_pending_or_writes_state(tmp_path, monkeypatch, restart):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    restored = None
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        runtime.gate.observe(observation(tmp_path))
        if restart:
            await runtime.aclose()
            restored = reopen(runtime)
            runtime = restored
        before = await asyncio.to_thread(files, runtime.directory)
        calls = len(probe.prompts)
        first = status_snapshot(runtime)
        second = status_snapshot(runtime)
        assert first == second
        assert first.goal == ROOT_TASK
        assert first.contracts
        assert first.queued_observations == 1 and not first.evidence
        assert len(probe.prompts) == calls
        assert await asyncio.to_thread(files, runtime.directory) == before
        if restart:
            assert runtime.gate.session is None
    finally:
        if restored:
            await restored.aclose()
        await host.shutdown()


class StatusApp(TuiVariableDefaultsMixin, App):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog

    def on_mount(self):
        self.push_screen(self.dialog)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 38), (60, 25)])
async def test_status_dialog_groups_plain_text_and_escape(tmp_path, monkeypatch, size):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        snapshot = replace(
            status_snapshot(runtime_for(host)), goal="[bold]literal goal[/bold]", brief="<brief>Read only</brief>"
        )
        dialog = CompassStatusDialog(snapshot)
        app = StatusApp(dialog)
        calls = len(probe.prompts)
        async with app.run_test(size=size) as pilot:
            await wait_for(lambda: dialog.is_mounted)
            assert "[bold]literal goal[/bold]" in str(dialog.query_one("#compass-goal-body", Static).render())
            await pilot.click("#compass-preview")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            assert "<brief>Read only</brief>" in str(app.screen.query_one("#detail-body", Static).render())
            await pilot.press("escape")
            assert dialog.query_one("#compass-close").is_on_screen
            assert dialog.query_one("#compass-summary").region.width <= size[0]
            await pilot.press("escape")
            assert not isinstance(app.screen, CompassStatusDialog)
        assert len(probe.prompts) == calls
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_native_registry_dispatches_both_commands_without_adding_user_turn(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)

        class DisplayEngine(ShutdownOnlyEngine):
            session_id = host.engine.session_id
            session_dir = host.engine.session_dir

        app = make_chrys_app(tmp_path / "ui", engine=DisplayEngine())
        async with app.run_test(size=(100, 38)) as pilot:
            await wait_for(
                lambda: (
                    app._main_screen is not None
                    and app.screen is app._main_screen
                    and not app._main_screen._state.run.agent_loading
                ),
                pilot=pilot,
            )
            main = app._main_screen
            assert isinstance(main, MainScreen)
            main.query_one(InputBar).post_message(InputBar.UserSubmitted("/compass status"))
            await wait_for(lambda: isinstance(app.screen, CompassStatusDialog) and app.screen.is_mounted)
            assert probe.prompts == []
            await pilot.press("escape")
            main.query_one(InputBar).post_message(InputBar.UserSubmitted("/compass"))
            await wait_for(
                lambda: isinstance(app.screen, MaintenanceDialog) and app.screen.is_mounted and app.screen.finished
            )
            assert len(probe.extractions) == 1
            assert len(client.call_history) == 1
            assert [item["text"] for item in runtime.gate.state()["compass_tools"]["user_inputs"]] == [ROOT_TASK]
            await pilot.press("escape")
            assert app.screen is main
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_dispatch_refuses_wrong_session_busy_unknown_and_disabled(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        app = Mock(spec=App)
        app.screen_stack = ()
        for arg, engine, session_id, busy in [
            ("force", host.engine, host.engine.session_id, False),
            ("", host.engine, "another-session", False),
            ("", host.engine, host.engine.session_id, True),
            ("", None, None, False),
        ]:
            dispatch(app, arg, engine=engine, session_id=session_id, busy=busy)
        assert app.notify.call_count == 4
        app.push_screen.assert_not_called()
        assert not probe.prompts
        dispatch(app, "status", engine=host.engine, session_id=host.engine.session_id, busy=True)
        assert isinstance(app.push_screen.call_args.args[0], CompassStatusDialog)
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_first_command_without_a_user_task_does_not_extract_slash_text(tmp_path, monkeypatch):
    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [])
    try:
        await host.start()
        app = Mock(spec=App)
        app.screen_stack = ()
        dispatch(app, "", engine=host.engine, session_id=host.engine.session_id, busy=False)
        app.push_screen.assert_not_called()
        app.notify.assert_called_once()
        assert "Send a task" in app.notify.call_args.args[0]
        assert not probe.prompts
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_maintenance_modal_owns_worker_and_does_not_dismiss_mid_operation(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        original = runtime.update_requirements

        async def blocked():
            entered.set()
            await release.wait()
            return await original()

        monkeypatch.setattr(runtime, "update_requirements", blocked)
        dialog = MaintenanceDialog(runtime)
        app = StatusApp(dialog)
        async with app.run_test() as pilot:
            await wait_for(entered.is_set)
            await pilot.press("escape")
            assert app.screen is dialog and not dialog.finished
            release.set()
            await wait_for(lambda: dialog.finished)
            await pilot.press("escape")
            assert app.screen is not dialog
    finally:
        release.set()
        await host.shutdown()


@pytest.mark.asyncio
async def test_maintenance_failure_keeps_exception_details_out_of_ui(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        monkeypatch.setattr(runtime, "update_requirements", AsyncMock(side_effect=RuntimeError("secret-token")))
        dialog = MaintenanceDialog(runtime)
        async with StatusApp(dialog).run_test() as pilot:
            await wait_for(lambda: dialog.finished)
            result = str(dialog.query_one("#compass-maintenance-result", Static).render())
            assert "RuntimeError" in result and "secret-token" not in result
            await pilot.press("escape")
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_app_shutdown_cancels_maintenance_worker(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    entered, cancelled = asyncio.Event(), asyncio.Event()
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)

        async def blocked():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(runtime, "update_requirements", blocked)
        async with StatusApp(MaintenanceDialog(runtime)).run_test():
            await wait_for(entered.is_set)
        assert cancelled.is_set()
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_core_maintenance_error_is_not_reported_as_success(tmp_path, monkeypatch):
    host, _, _, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)
        runtime = runtime_for(host)
        monkeypatch.setattr(
            runtime,
            "update_brief",
            AsyncMock(return_value='{"maintenance_error":"provider-secret", "brief":"last saved brief"}'),
        )
        dialog = MaintenanceDialog(runtime)
        async with StatusApp(dialog).run_test():
            await wait_for(lambda: dialog.finished)
            result = str(dialog.query_one("#compass-maintenance-result", Static).render())
            assert "reported an error" in result and "provider-secret" not in result
            assert "last saved brief" in str(dialog.query_one("#compass-maintenance-brief", Static).render())
    finally:
        await host.shutdown()
