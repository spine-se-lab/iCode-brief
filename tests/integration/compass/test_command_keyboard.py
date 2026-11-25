"""Regression coverage for typing the Compass command and subcommand in native TUI."""

import pytest

from chrys.service.llm.mock import MockResponse
from compass.adapters.icode.tui.command import MaintenanceDialog
from compass.adapters.icode.tui.status import CompassStatusDialog
from tests.integration.compass.support import host_for
from tests.support.tui_app_harness import ShutdownOnlyEngine, make_chrys_app
from tests.support.waiting import wait_for


@pytest.mark.asyncio
async def test_typed_compass_subcommand_and_manual_maintenance(tmp_path, monkeypatch):
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
            await pilot.press("/", "c", "o", "m", "p", "a", "s", "s", "space", "s", "t", "a", "t", "u", "s", "enter")
            await wait_for(
                lambda: isinstance(app.screen, CompassStatusDialog) and app.screen.is_mounted, pilot=pilot, timeout=5
            )
            assert not probe.prompts
            await pilot.press("escape")
            await pilot.press(*"/compass", "enter")
            await wait_for(
                lambda: isinstance(app.screen, MaintenanceDialog) and app.screen.is_mounted and app.screen.finished,
                pilot=pilot,
                timeout=5,
            )
            assert len(probe.extractions) == 1
            assert len(client.call_history) == 1
            await pilot.press("escape")
            notices = []
            original_notify = app.notify

            def notify(message, **kwargs):
                notices.append(str(message))
                return original_notify(message, **kwargs)

            monkeypatch.setattr(app, "notify", notify)
            await pilot.press(*"/compass", "space", *"nonsense", "enter")
            await wait_for(lambda: notices, pilot=pilot)
            assert any("Use /compass or /compass status" in message for message in notices)
            assert app.screen is app._main_screen
            assert len(probe.extractions) == 1
            assert len(client.call_history) == 1
    finally:
        await host.shutdown()
