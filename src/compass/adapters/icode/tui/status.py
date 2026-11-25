"""Compass module navigation, inline content and optional record details."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, OptionList, Static

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.i18n.formatting import format_message
from compass.adapters.icode.commands import CompassStatus

from .details import DetailEntry, DetailLauncher, DetailScreen, EntryList
from .presentation import brief_entries
from .zones import BriefZones, ZoneSpec

_TITLE = msg("compass.status.title", fallback="Compass status")
_READ_ONLY = msg(
    "compass.status.read_only", fallback="Read-only snapshot · Viewing does not call a model or deliver a Brief."
)
_EMPTY = msg("compass.status.empty", fallback="No records yet.")
_MAINTAIN = msg("compass.status.maintain", fallback="Update Brief")
_PREVIEW = msg("compass.status.preview", fallback="Brief & History")
_GOAL = msg("compass.status.goal", fallback="Goal")
_REQUIREMENTS = msg("compass.status.requirements", fallback="Requirements")
_CONSTRAINTS = msg("compass.status.constraints", fallback="Constraints")
_CLAIMS = msg("compass.status.claims", fallback="Claims")
_TENSIONS = msg("compass.status.tensions", fallback="Tensions")
_ALREADY_READ = msg("compass.status.already_read", fallback="Already read")
_WEAK_COVERAGE = msg("compass.status.weak_coverage", fallback="Weak coverage")
_EVIDENCE_STATUS = msg("compass.status.evidence_status", fallback="Evidence status")
_GROUPS = (
    ("goal", _GOAL),
    ("requirements", _REQUIREMENTS),
    ("constraints", _CONSTRAINTS),
    ("claims", _CLAIMS),
    ("tensions", _TENSIONS),
    ("already-read", _ALREADY_READ),
    ("attention", _WEAK_COVERAGE),
    ("freshness", _EVIDENCE_STATUS),
)


def text(message: MessageDef) -> str:
    return format_message(message.bind())


def literal(value: str) -> Text:
    return Text(sanitize_source_text(value))


class CompassStatusDialog(BaseDialog[None]):
    BINDINGS: ClassVar[list] = [localized_binding("escape", "dismiss", CLOSE_BINDING)]
    DEFAULT_CSS = """
    CompassStatusDialog { align: center middle; }
    CompassStatusDialog > Vertical {
        width: 95%; max-width: 150; height: 90%; border: round $primary;
        background: $surface; padding: 0 2;
    }
    CompassStatusDialog Static { height: auto; }
    CompassStatusDialog #compass-status-title { text-style: bold; }
    CompassStatusDialog .compass-note { color: $text-muted; }
    CompassStatusDialog #compass-summary { height: auto; color: $text-muted; }
    CompassStatusDialog #compass-summary:focus { text-style: bold; }
    CompassStatusDialog #compass-actions { height: 1; margin-top: 1; }
    CompassStatusDialog #compass-actions Button {
        width: auto; min-width: 6; height: 1; min-height: 1; border: none; padding: 0 1; margin-right: 1;
    }
    CompassStatusDialog #compass-goal-scroll { height: 1fr; overflow-x: hidden; }
    CompassStatusDialog #compass-goal-body { height: auto; }
    """

    def __init__(
        self,
        snapshot: CompassStatus,
        *,
        on_maintain: Callable[[], None] | None = None,
        refresh: Callable[[], CompassStatus] | None = None,
        load_briefs: Callable[[], tuple[tuple[int, str], ...]] | None = None,
    ) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.on_maintain = on_maintain
        self.load_snapshot = refresh
        self.load_briefs = load_briefs
        self.groups, self.lookup = self._projection()

    def _projection(self):
        snapshot = self.snapshot
        return brief_entries(
            replace(snapshot.goal_state, description=snapshot.goal),
            snapshot.claim_snapshot,
            snapshot.evidence_records,
        )

    def _group_widgets(self, key: str):
        if key == "goal":
            yield VerticalScroll(Static(id="compass-goal-body"), id="compass-goal-scroll")
            return
        yield EntryList(id=f"compass-{key}-list")
        yield Static(text(_EMPTY), id=f"compass-{key}-empty")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(text(_TITLE), id="compass-status-title")
            yield Static(text(_READ_ONLY), classes="compass-note")
            yield BriefZones(
                tuple(ZoneSpec(key, text(title), lambda key=key: self._group_widgets(key)) for key, title in _GROUPS),
                initially_open="goal",
            )
            yield DetailLauncher(id="compass-summary")
            with Horizontal(id="compass-actions"):
                yield Button(
                    text(_MAINTAIN),
                    id="compass-maintain",
                    variant="primary",
                    compact=True,
                    disabled=self.on_maintain is None,
                )
                yield Button(text(_PREVIEW), id="compass-preview", compact=True)
                yield Button(text(CLOSE_BINDING), id="compass-close", compact=True)

    def on_mount(self) -> None:
        self._show_snapshot()

    def on_screen_resume(self) -> None:
        # Preserve expanded group, selection and scroll when a detail closes.
        # Refresh only from the read-only adapter, never flush observations here.
        if self.is_mounted and self.load_snapshot:
            self.snapshot = self.load_snapshot()
            self.groups, self.lookup = self._projection()
            self._show_snapshot()

    def _show_snapshot(self) -> None:
        for key, _ in _GROUPS:
            entries = self.groups[key]
            if key == "goal":
                self.query_one("#compass-goal-body", Static).update(entries[0].body_text() if entries else text(_EMPTY))
                continue
            listing = self.query_one(f"#compass-{key}-list", EntryList)
            listing.set_entries(entries)
            listing.display = bool(entries)
            self.query_one(f"#compass-{key}-empty").display = not entries
        self.query_one(BriefZones).update_counts({key: len(entries) for key, entries in self.groups.items()})
        s = self.snapshot
        self.query_one("#compass-summary", DetailLauncher).update(
            literal(
                f"{s.phase} · Unprocessed evidence: {s.pending_evidence} · Queued observations: {s.queued_observations} · Revision: {s.revision}"
            )
        )
        self.query_one("#compass-maintain", Button).disabled = self.on_maintain is None or s.phase == "busy"

    @on(OptionList.OptionSelected)
    def inspect_entry(self, event: OptionList.OptionSelected) -> None:
        if not isinstance(event.option_list, EntryList) or not event.option.id:
            return
        event.stop()
        self.app.push_screen(
            DetailScreen(event.option_list.entries, event.option.id, self.lookup, revision=self.snapshot.revision)
        )

    def _show_text(self, identity: str, title: str, body: str) -> None:
        entry = DetailEntry(identity, title, "", sanitize_source_text(body))
        self.app.push_screen(DetailScreen((entry,), identity, {identity: entry}, revision=self.snapshot.revision))

    @on(DetailLauncher.Selected)
    def inspect_progress(self, event: DetailLauncher.Selected) -> None:
        event.stop()
        s = self.snapshot
        self._show_text(
            "progress",
            "Maintenance status",
            f"Phase: {s.phase}\nPending evidence: {s.pending_evidence}\nQueued observations: {s.queued_observations}\nProcessed through: {s.processed_evidence}\nTrigger: {s.reason or '—'}\nEvidence steps remaining: {s.remaining_steps}\nLast error: {s.error or '—'}\n\nViewing never processes queued observations or validates code.",
        )

    @on(Button.Pressed, "#compass-preview")
    def preview(self, event: Button.Pressed) -> None:
        event.stop()
        try:
            archives = self.load_briefs() if self.load_briefs else ()
        except OSError, ValueError:
            self.app.notify("Saved Brief history could not be read safely.", severity="warning")
            archives = ()
        entries = tuple(
            DetailEntry(
                f"brief-{revision}",
                "Historical Brief",
                "",
                body,
                context=f"Saved revision {revision} · Not a delivery receipt"
                + (
                    " · Original compact snapshot; omitted text was not saved"
                    if "[Goal & Claims — compact reminder]" in body
                    else ""
                ),
            )
            for revision, body in archives
        )
        current = DetailEntry(
            "brief-current",
            "Current Brief preview",
            "",
            self.snapshot.brief or text(_EMPTY),
            context=f"Current snapshot rev {self.snapshot.revision} · Not a delivery receipt",
        )
        entries += (current,)
        self.app.push_screen(DetailScreen(entries, current.id, {entry.id: entry for entry in entries}))

    @on(Button.Pressed, "#compass-maintain")
    def maintain(self, event: Button.Pressed) -> None:
        event.stop()
        if self.on_maintain:
            self.on_maintain()

    @on(Button.Pressed, "#compass-close")
    def close_status(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss()
