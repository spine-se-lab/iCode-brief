"""Historical Brief navigation reads archives without triggering maintenance."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import Button

from compass.adapters.icode.commands import brief_history
from compass.adapters.icode.storage import Artifacts
from compass.adapters.icode.tui.details import DetailScreen
from compass.adapters.icode.tui.status import CompassStatusDialog
from tests.integration.compass.test_popup_groups import PopupApp, sample


def test_archives_are_numeric_ordered_read_only_and_session_scoped(tmp_path):
    artifacts = Artifacts()
    directory = tmp_path / "ledger" / "claims" / "briefs"
    directory.mkdir(parents=True)
    for revision in (11, 1, 3):
        artifacts.write_text(directory / f"brief-{revision:04}.txt", f"saved {revision}")
    (directory / "brief-invalid.txt").write_text("ignored")
    runtime = SimpleNamespace(directory=tmp_path, artifacts=artifacts)
    before = {p: p.read_bytes() for p in directory.iterdir()}
    assert brief_history(runtime) == ((1, "saved 1"), (3, "saved 3"), (11, "saved 11"))
    assert {p: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("archives", [(), ((1, "old one"), (3, "old three"))])
async def test_preview_opens_current_then_navigates_saved_revisions(archives):
    loader, maintain = Mock(return_value=archives), Mock()
    dialog = CompassStatusDialog(sample(), load_briefs=loader, on_maintain=maintain)
    app = PopupApp(dialog)
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        loader.assert_not_called()
        await pilot.click("#compass-preview")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        assert screen.entries[screen.index].id == "brief-current"
        assert screen.query_one("#detail-next", Button).disabled
        for revision, body in reversed(archives):
            await pilot.press("left")
            assert screen.entries[screen.index].body == body
            assert f"Saved revision {revision}" in screen.entries[screen.index].context
        assert screen.query_one("#detail-previous", Button).disabled
        for index, _ in enumerate(archives):
            if index == 0:
                await pilot.click("#detail-next")
            else:
                await pilot.press("right")
        assert screen.entries[screen.index].id == "brief-current"
        assert screen.entries[screen.index].body == "[red]Literal Brief[/red]"
        await pilot.press("escape")
        assert app.screen is dialog
        loader.assert_called_once_with()
        maintain.assert_not_called()


def test_history_refuses_linked_archive(tmp_path):
    directory = tmp_path / "ledger" / "claims" / "briefs"
    directory.mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_text("not a brief")
    link = directory / "brief-0001.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(OSError):
        brief_history(SimpleNamespace(directory=tmp_path, artifacts=Artifacts()))


@pytest.mark.asyncio
async def test_unreadable_history_keeps_current_preview_available():
    loader = Mock(side_effect=OSError("unsafe archive"))
    dialog = CompassStatusDialog(sample(), load_briefs=loader)
    app = PopupApp(dialog)
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.click("#compass-preview")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert len(app.screen.entries) == 1
        assert app.screen.entries[0].id == "brief-current"


def test_compact_brief_rows_are_separate_and_literal_without_losing_text():
    import re

    from compass.adapters.icode.tui.brief_text import brief_text

    source = (
        "[Goal & Claims — compact reminder]\nGoal: Support [red]literal[/red] values.\n"
        "Requirements: R1?/code-read Preserve `a; b`; R2!/no-evidence/80steps Keep … original.\n"
        "Constraints: K1+/repo-tested Do not remove tests; K2?/code-read Keep signatures.\n"
    )
    rendered = brief_text(source)
    assert re.sub(r"\s+", "", rendered.plain) == re.sub(r"\s+", "", source)
    for identity in ("R1", "R2", "K1", "K2"):
        assert f"\n  {identity}" in rendered.plain
        assert any(rendered.plain[span.start : span.end] == identity for span in rendered.spans)
    assert "`a; b`" in rendered.plain
    assert "[red]literal[/red]" in rendered.plain
    assert all(span.style == "bold" for span in rendered.spans)


@pytest.mark.asyncio
async def test_current_and_historical_briefs_both_have_styled_section_labels():
    from dataclasses import replace

    from textual.widgets import Static

    current = "Goal: Complete current goal\nRequirements:\n  ? R1 code-read — Full requirement."
    old = "Requirements: R1?/code-read First; R2!/no-evidence Second."
    dialog = CompassStatusDialog(replace(sample(), brief=current), load_briefs=lambda: ((1, old),))
    app = PopupApp(dialog)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.click("#compass-preview")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        for source in (current, old):
            entry = screen.entries[screen.index]
            assert entry.body == source
            rendered = entry.body_text()
            assert any(rendered.plain[span.start : span.end] == "Requirements:" for span in rendered.spans)
            assert "Requirements:" in str(screen.query_one("#detail-body", Static).render())
            await pilot.press("left")
