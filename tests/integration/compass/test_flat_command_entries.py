"""Native same-level Compass choices and typed update dispatch."""

from unittest.mock import Mock

import pytest

from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.tui.command import MaintenanceDialog, command_definitions, dispatch
from compass.adapters.icode.tui.status import CompassStatusDialog
from tests.integration.compass.support import host_for
from tests.support.tui_app_harness import ShutdownOnlyEngine, make_chrys_app
from tests.support.waiting import wait_for


@pytest.mark.asyncio
async def test_native_flat_compass_choices_and_typed_status(tmp_path, monkeypatch):
    host, client, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="ready")])
    try:
        await host.run_until_final("Read this repository", timeout=10)

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
            await pilot.press(*"/compass")
            menu = app._main_screen.query_one(SuggestionList)
            await pilot.pause()
            assert [value for value in menu._values if value is not None] == ["compass", "compass status"]
            await pilot.press("down", "enter")
            await wait_for(lambda: isinstance(app.screen, CompassStatusDialog) and app.screen.is_mounted, pilot=pilot)
            assert not probe.prompts
            await pilot.press("escape")
            await pilot.press(*"/compass", "enter")
            await wait_for(
                lambda: isinstance(app.screen, MaintenanceDialog) and app.screen.is_mounted and app.screen.finished,
                pilot=pilot,
            )
            assert len(probe.extractions) == 1
            await pilot.press("escape")
            await pilot.press(*"/compass", "enter")
            await wait_for(
                lambda: isinstance(app.screen, MaintenanceDialog) and app.screen.is_mounted and app.screen.finished,
                pilot=pilot,
            )
            assert len(probe.extractions) == 1
            await pilot.press("escape")
            await pilot.press(*"/compass", "space", *"status", "enter")
            await wait_for(lambda: isinstance(app.screen, CompassStatusDialog) and app.screen.is_mounted, pilot=pilot)
            assert len(client.call_history) == 1
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_busy_maintenance_refused_but_status_remains_read_only(tmp_path, monkeypatch):
    from textual.app import App

    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [MockResponse(text="ready")])
    try:
        await host.run_until_final("Inspect only", timeout=10)
        app = Mock(spec=App)
        app.screen_stack = ()
        dispatch(app, "", engine=host.engine, session_id=host.engine.session_id, busy=True)
        app.push_screen.assert_not_called()
        app.notify.assert_called_once()
        dispatch(app, "status", engine=host.engine, session_id=host.engine.session_id, busy=True)
        assert isinstance(app.push_screen.call_args.args[0], CompassStatusDialog)
        assert not probe.prompts
        entries = command_definitions(Mock())
        assert all(entry.subcommands is None for entry in entries)
        assert entries[1].allow_while_running
    finally:
        await host.shutdown()
