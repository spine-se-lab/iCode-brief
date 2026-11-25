"""New acceptance checks: legacy sidebar interactions inside the Compass modal."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from textual.app import App
from textual.widgets import Button, Static, TabbedContent

from chrys.app.tui.theme import TuiVariableDefaultsMixin
from compass.adapters.icode.commands import CompassStatus
from compass.adapters.icode.tui.details import DetailScreen, EntryList
from compass.adapters.icode.tui.presentation import brief_entries
from compass.adapters.icode.tui.status import CompassStatusDialog
from compass.claims.models import ClaimRecord, ClaimSnapshot, ClaimState, RequirementCoverage
from compass.evidence.models import EvidenceRecord
from compass.task_contract.models import GoalConclusion, GoalState


def sample():
    goal = GoalState(
        "[bold]Fix pagination[/bold]",
        (
            GoalConclusion("First page starts at item 1", tag="criteria"),
            GoalConclusion("Empty pages return []", tag="criteria"),
            GoalConclusion("Do not mutate input", tag="constraint"),
        ),
    )
    claim = ClaimRecord(
        "C-001",
        "First-page test passed",
        "observation",
        "verify",
        1,
        grounding="repo_test",
        verdict="supported",
        evidence_refs=("E-001",),
    )
    hidden = replace(claim, id="C-002", statement="Historical claim")
    claims = ClaimSnapshot(
        ClaimState(
            revision=3, active_ids=("C-001",), requirement_coverage={"R1": RequirementCoverage(("C-001",), "repo_test")}
        ),
        (claim, hidden),
    )
    evidence = (EvidenceRecord(1, "test", "ok", "shell", summary="Tests passed", excerpt="10 tests OK"),)
    return CompassStatus(
        goal.description,
        (),
        (),
        (),
        3,
        1,
        0,
        1,
        "ready",
        "test_result",
        "",
        "[red]Literal Brief[/red]",
        goal_state=goal,
        claim_snapshot=claims,
        evidence_records=evidence,
    )


class PopupApp(TuiVariableDefaultsMixin, App):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog

    def on_mount(self):
        self.push_screen(self.dialog)


def test_projection_uses_core_groups_active_claims_and_real_links():
    s = sample()
    groups, lookup = brief_entries(s.goal_state, s.claim_snapshot, s.evidence_records)
    assert tuple(groups) == (
        "goal",
        "requirements",
        "constraints",
        "claims",
        "tensions",
        "already-read",
        "attention",
        "freshness",
    )
    assert len(groups["requirements"]) == 2
    assert [item.id for item in groups["claims"]] == ["C-001"]
    assert lookup["R1"].links == ("C-001",)
    assert "E-001" in lookup["C-001"].links
    assert "10 tests OK" in lookup["E-001"].body
    assert lookup["R2"].meta == "No related claims recorded"
    assert "Related claims are not proof of completion" not in lookup["R1"].body


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(100, 38), (60, 25)])
async def test_popup_tab_navigation_preview_and_maintenance(size):
    maintain = Mock()
    dialog = CompassStatusDialog(sample(), on_maintain=maintain)
    app = PopupApp(dialog)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        zones = dialog.query_one(TabbedContent)
        assert zones.active == "zone-goal"
        for key in ("maintain", "preview", "close"):
            assert dialog.query_one(f"#compass-{key}", Button).is_on_screen
        assert "[bold]" in dialog.lookup["goal"].body_text().plain
        await pilot.click(zones.get_tab("zone-requirements"))
        assert app.screen is dialog
        assert zones.active == "zone-requirements"
        assert dialog.query_one("#compass-maintain", Button).region.height == 1
        assert dialog.query_one("#compass-maintain", Button).region.width < dialog.region.width // 2
        assert str(dialog.query_one("#compass-maintain", Button).label) == "Update Brief"
        assert zones.active == "zone-requirements"
        listing = dialog.query_one("#compass-requirements-list", EntryList)
        listing.focus()
        await pilot.press("enter")
        await pilot.pause()
        details = app.screen
        assert isinstance(details, DetailScreen)
        assert details.entries[details.index].id == "R1"
        await pilot.press("right")
        assert details.entries[details.index].id == "R2"
        await pilot.press("left")
        links = details.query_one("#detail-links", EntryList)
        links.focus()
        await pilot.press("enter")
        assert details.entries[details.index].id == "C-001"
        links = details.query_one("#detail-links", EntryList)
        links.highlighted = links.get_option_index("E-001")
        links.focus()
        await pilot.press("enter")
        assert details.entries[details.index].id == "E-001"
        await pilot.press("backspace")
        assert details.entries[details.index].id == "C-001"
        await pilot.press("escape")
        assert app.screen is dialog
        assert listing.has_focus
        assert zones.active == "zone-requirements"
        await pilot.click("#compass-preview")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert "[red]Literal Brief[/red]" in str(app.screen.query_one("#detail-body", Static).render())
        await pilot.press("escape")
        maintain.assert_not_called()
        await pilot.click("#compass-maintain")
        maintain.assert_called_once_with()
        await pilot.press("escape")
        assert app.screen is not dialog


@pytest.mark.asyncio
async def test_empty_group_busy_state_and_snapshot_refresh_keep_selection():
    state = sample()
    dialog = CompassStatusDialog(state, on_maintain=Mock(), refresh=lambda: state)
    app = PopupApp(dialog)
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        dialog.query_one(TabbedContent).active = "zone-tensions"
        await pilot.pause()
        assert dialog.query_one("#compass-tensions-empty").is_on_screen
        dialog.query_one(TabbedContent).active = "zone-claims"
        await pilot.pause()
        await pilot.click("#compass-preview")
        state = replace(state, phase="busy", revision=4)
        await pilot.press("escape")
        await pilot.pause()
        assert dialog.snapshot.revision == 4
        assert dialog.query_one("#compass-maintain", Button).disabled
        assert dialog.query_one(TabbedContent).active == "zone-claims"


@pytest.mark.asyncio
async def test_native_status_maintain_returns_to_refreshed_popup(tmp_path, monkeypatch):
    from chrys.service.llm.mock import MockResponse
    from compass.adapters.icode.tui.command import MaintenanceDialog, dispatch
    from tests.integration.compass.support import ROOT_TASK, host_for
    from tests.integration.compass.test_captured_requirements import update
    from tests.support.waiting import wait_for

    host, _, probe, _, _ = await host_for(tmp_path, monkeypatch, [update(), MockResponse(text="done")])
    try:
        await host.run_until_final(ROOT_TASK, timeout=10)

        class CommandApp(TuiVariableDefaultsMixin, App):
            def on_mount(self):
                dispatch(self, "status", engine=host.engine, session_id=host.engine.session_id, busy=False)

        app = CommandApp()
        calls = len(probe.prompts)
        async with app.run_test(size=(100, 38)) as pilot:
            await wait_for(lambda: isinstance(app.screen, CompassStatusDialog) and app.screen.is_mounted, pilot=pilot)
            assert len(probe.prompts) == calls
            status = app.screen
            await pilot.click("#compass-maintain")
            await wait_for(
                lambda: isinstance(app.screen, MaintenanceDialog) and app.screen.is_mounted and app.screen.finished,
                pilot=pilot,
            )
            await pilot.press("escape")
            assert app.screen is status
            assert status.snapshot.goal
            assert len(probe.prompts) == calls  # unchanged inputs need no new extraction
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_narrow_popup_tabs_reach_every_module_without_opening_a_dialog():
    dialog = CompassStatusDialog(sample())
    app = PopupApp(dialog)
    async with app.run_test(size=(60, 25)) as pilot:
        await pilot.pause()
        tabs = dialog.query_one(TabbedContent)
        tabs.get_tab("zone-goal").focus()
        for key in ("requirements", "constraints", "claims", "tensions", "already-read", "attention", "freshness"):
            await pilot.press("right")
            assert tabs.active == "zone-" + key
            assert app.screen is dialog
            assert tabs.get_tab(tabs.active).is_on_screen


def test_detail_hierarchy_bolds_labels_without_bolding_literal_content():
    state = sample()
    _, lookup = brief_entries(state.goal_state, state.claim_snapshot, state.evidence_records)
    entry = lookup["R1"]
    body = entry.body_text()
    assert any(body.plain[span.start : span.end] == "Requirement" for span in body.spans)
    start = body.plain.index("First page")
    assert not any(span.start <= start < span.end for span in body.spans)


@pytest.mark.asyncio
async def test_unselected_entry_titles_are_bold_and_colored_in_actual_rendering():
    dialog = CompassStatusDialog(sample())
    app = PopupApp(dialog)
    async with app.run_test(size=(100, 38)) as pilot:
        dialog.query_one(TabbedContent).active = "zone-requirements"
        await pilot.pause()
        listing = dialog.query_one("#compass-requirements-list", EntryList)
        rows = [listing.render_line(y) for y in range(listing.size.height)]
        title = next(segment for row in rows for segment in row if "R2" in segment.text)
        content = next(segment for row in rows for segment in row if "Empty pages" in segment.text)
        assert title.style.bold is True
        assert title.style.color is not None
        assert title.style.color != content.style.color
        assert content.style.bold is not True


def test_record_status_is_hidden_without_changing_stored_contracts():
    state = sample()
    groups, lookup = brief_entries(state.goal_state, state.claim_snapshot, state.evidence_records)
    assert groups["goal"][0].body == state.goal_state.description
    assert "pending" not in groups["goal"][0].body
    assert groups["requirements"][0].summary == "First page starts at item 1"
    assert groups["constraints"][0].summary == "Do not mutate input"
    for identity in ("R1", "R2", "K1"):
        assert "Record status" not in lookup[identity].body
        assert "pending" not in lookup[identity].body
    assert all(item.status == "pending" for item in state.goal_state.conclusions)


@pytest.mark.parametrize(
    ("coverage", "refs"),
    [
        (None, "No related claims recorded"),
        (RequirementCoverage(), "No related claims recorded"),
        (RequirementCoverage(("C-001", "C-002")), "C-001, C-002"),
        (RequirementCoverage(("C-001",), "repo_test"), "C-001"),
    ],
)
def test_contract_details_keep_links_without_evidence_support(coverage, refs):
    status = sample()
    snapshot = status.claim_snapshot
    state = replace(snapshot.state, requirement_coverage={"R1": coverage, "K1": coverage} if coverage else {})
    _, lookup = brief_entries(status.goal_state, replace(snapshot, state=state), status.evidence_records)
    for identity in ("R1", "K1"):
        entry = lookup[identity]
        assert f"Associated Claims\n  {refs}" in entry.body
        assert "Evidence support" not in entry.body
        assert entry.meta == (f"Related claims: {refs}" if coverage and coverage.claim_ids else refs)
        assert "pass/fail result" not in entry.body
        assert "none ·" not in entry.meta
        assert entry.links == (coverage.claim_ids if coverage else ())
    assert state.requirement_coverage == ({"R1": coverage, "K1": coverage} if coverage else {})
