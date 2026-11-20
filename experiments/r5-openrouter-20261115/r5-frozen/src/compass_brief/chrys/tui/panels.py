# Copyright (c) 2026 Chrys. All rights reserved.

"""Sidebar panels contributed by the Claim Layer.

The Brief groups the current working projection. The legacy Evidence renderer
remains available, but is not contributed as a sidebar tab.

These are adapters. Every decision they display was already made in
the host-independent functional packages; what lives here is Textual and Rich, plus the one
translation the Feature seam requires: a panel cannot reach a backend itself, so
a button press leaves as a :class:`FeatureActionRequested` for the TUI Feature
to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from chrys.app.tui.features import FeatureActionRequested, FeaturePanel
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets.sidebar.empty_state import SidebarEmptyStateLabel
from rich.table import Table
from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static

from compass_brief.chrys.tui.details import DetailEntry, DetailLauncher, DetailScreen, EntryList
from compass_brief.chrys.tui.presentation import brief_entries
from compass_brief.chrys.tui.zones import BriefZones, ZoneSpec
from compass_brief.evidence.models import MAX_EVIDENCE_DISPLAY_RECORDS
from compass_brief.intervention.maintenance import MaintenanceStatus

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from compass_brief.claims.models import ClaimRecord, ClaimState
    from compass_brief.evidence.models import EvidenceRecord
    from compass_brief.task_contract.models import GoalConclusion

FEATURE_NAME: Final = "compass_brief"
ACTION_MAINTAIN: Final = "maintain"
"""Actions these panels can ask the backend for.

Named here rather than in the Feature adapter because the panel is what raises
them; the adapter only has to recognise the maintenance request.
"""

__all__ = [
    "ACTION_MAINTAIN",
    "FEATURE_NAME",
    "ClaimViewState",
    "EvidencePanel",
    "EvidenceViewState",
    "GoalAndClaimsPanel",
    "GoalAndClaimsViewState",
    "GoalViewState",
]


@dataclass(frozen=True, slots=True)
class GoalViewState:
    """View state for the Goal panel (full-state payload)."""

    description: str = ""
    conclusions: tuple[GoalConclusion, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimViewState:
    """Full Claim Table projection displayed beside the current goal."""

    state: ClaimState | None = None
    items: tuple[ClaimRecord, ...] = ()
    current_step: int = 0
    maintenance: MaintenanceStatus | None = None

    @property
    def active_items(self) -> tuple[ClaimRecord, ...]:
        """Return the active records in authoritative projection order."""
        if self.state is None:
            return ()
        by_id = {item.id: item for item in self.items}
        return tuple(by_id[item] for item in self.state.active_ids if item in by_id)

    @property
    def pending_steps(self) -> int:
        """Return pending Evidence, never raw-file or host invocation counts."""
        if self.maintenance is not None:
            return self.maintenance.pending_evidence
        processed = self.state.processed_evidence_seq if self.state is not None else 0
        return max(0, self.current_step - processed)


def _maintenance_status_text(claims: ClaimViewState, *, compact=False) -> Text:
    """Render core progress; never recalculate cooldown/trigger decisions here."""
    progress = claims.maintenance
    state = claims.state
    failed = state is not None and not state.last_maintenance_ok
    fallback = "failed" if failed else "waiting" if claims.pending_steps else "idle"
    phase = progress.phase if progress else fallback
    if compact:
        label = {"running": "Maintaining · ", "queued": "Queued · ", "failed": "Failed · "}.get(phase, "")
        return Text(
            f"{label}Pending: {claims.pending_steps} evidence · rev {state.revision if state else 0}",
            no_wrap=True,
            overflow="ellipsis",
        )
    label = {
        "idle": "Current" if state and state.revision else "Not maintained yet",
        "waiting": "Collecting evidence",
        "queued": "Maintenance queued",
        "running": "Maintaining",
        "failed": "Maintenance failed",
    }[phase]
    lines = [f"{label} · Pending: {claims.pending_steps} evidence"]
    if state:
        lines[0] += f" · revision {state.revision}"
    reasons = {
        "bootstrap": "initial window",
        "test": "test results",
        "error": "errors",
        "change_burst": "changes",
        "observation_accumulate": "observations",
        "max_steps": "maximum wait",
    }
    if progress and progress.reason:
        reason = reasons.get(progress.reason, progress.reason)
        if phase == "waiting" and progress.remaining:
            lines.append(f"{reason}: at most {progress.remaining} more evidence before automatic maintenance")
        elif phase == "running":
            lines.append(reason)
    error = progress.error if progress else state.last_maintenance_error if state else ""
    if error:
        lines.append(f"{error}\nMaintain retries pending evidence.")
    return Text("\n".join(lines))


_KIND_MARKS: Final[dict[str, str]] = {
    "observation": "•",
    "test": "T",
    "change": "Δ",
    "error": "!",
}
_PHASE_LABELS: Final[dict[str, str]] = {}
_MAX_TEXT_DISPLAY: Final[int] = 140


@dataclass(frozen=True, slots=True)
class GoalAndClaimsViewState:
    """Both halves of the Goal & Claims tab in one routed projection.

    Either half may be ``None``: Goal and Claims change on different events, and
    a Claim commit must not blank a Goal that did not change with it.
    """

    goal: GoalViewState | None = None
    claims: ClaimViewState | None = None
    brief: BriefViewState | None = None


@dataclass(frozen=True, slots=True)
class BriefViewState:
    """Current working sections supplied by core, not parsed from model prose."""

    text: str = ""
    tensions: tuple[str, ...] = ()
    already_read: tuple[str, ...] = ()
    attention: tuple[str, ...] = ()
    freshness: str = ""


class BriefPreviewScreen(ModalScreen):
    """Inspect the current generated supplement, with no backend operation."""

    BINDINGS = (("escape", "dismiss", "Close"),)
    DEFAULT_CSS = """
    BriefPreviewScreen { align: center middle; }
    BriefPreviewScreen > Vertical {
        width: 90%; height: 85%; border: round $primary; background: $surface; padding: 1 2;
    }
    BriefPreviewScreen #preview-title { height: auto; text-style: bold; }
    BriefPreviewScreen #preview-help { height: auto; color: $text-muted; margin-bottom: 1; }
    BriefPreviewScreen VerticalScroll { height: 1fr; }
    BriefPreviewScreen #model-brief-preview { height: auto; }
    BriefPreviewScreen #preview-close { height: 1; min-height: 1; border: none; margin-top: 1; }
    """

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Model Brief preview", id="preview-title")
            yield Static("Current supplement, not a record of the last sent request. No model call.", id="preview-help")
            with VerticalScroll():
                yield Static(Text(self._text or "No model Brief yet. Start a task first."), id="model-brief-preview")
            yield Button("Close (Esc)", id="preview-close", compact=True)

    @on(Button.Pressed, "#preview-close")
    def close_preview(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss()


@dataclass(frozen=True, slots=True)
class EvidenceViewState:
    """View state for the Evidence panel (full-list payload)."""

    items: tuple[EvidenceRecord, ...] = ()
    claim_refs: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _evidence_renderable(
    state: EvidenceViewState,
    *,
    success_style: object | None = None,
    error_style: object | None = None,
    change_style: object | None = None,
    selected_ref: str = "",
) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=4, justify="center")
    grid.add_column(ratio=1)

    current_phase = ""
    refs_by_evidence = dict(state.claim_refs)
    for item in state.items[-MAX_EVIDENCE_DISPLAY_RECORDS:]:
        phase = item.phase or "workflow"
        if phase != current_phase:
            current_phase = phase
            grid.add_row(Text(""), Text(""))
            grid.add_row(Text(""), Text(_PHASE_LABELS.get(phase, phase), style="dim"))

        mark = _KIND_MARKS.get(item.kind, "?")
        style = None
        if item.outcome == "error":
            style = error_style
        elif item.kind == "test":
            style = success_style
        elif item.kind == "change":
            style = change_style
        mark_text = Text(f"[{mark}]", style=style)

        target = f" — {item.target}" if item.target else ""
        summary = _bounded(f"{item.summary or item.tool_name}{target}")
        evidence_ref = f"E-{item.sequence:03d}"
        line = Text(f"{evidence_ref} {summary}")
        claim_refs = refs_by_evidence.get(evidence_ref, ())
        if claim_refs:
            line.append(f"  ← {' '.join(claim_refs)}", style="dim")
        if evidence_ref == selected_ref:
            line.stylize("bold")
        grid.add_row(mark_text, line)
        if item.excerpt:
            grid.add_row(Text(""), Text(f"← {_bounded(item.excerpt)}", style="dim"))
    return grid


def _bounded(value: str) -> str:
    text = " ".join(value.split())
    return f"{text[:_MAX_TEXT_DISPLAY]}…" if len(text) > _MAX_TEXT_DISPLAY else text


class GoalAndClaimsPanel(FeaturePanel, can_focus=False):
    """On-demand Brief: stable groups, one expanded body, a fixed maintenance control."""

    feature_name = FEATURE_NAME
    goal_state: reactive[GoalViewState | None] = reactive(None, always_update=True)
    claim_state: reactive[ClaimViewState | None] = reactive(None, always_update=True)
    brief_state: reactive[BriefViewState | None] = reactive(None, always_update=True)
    _MAX_SYNC_RETRIES = 3

    DEFAULT_CSS = """
    GoalAndClaimsPanel { height: 100%; width: 100%; padding: 0 1; }
    GoalAndClaimsPanel > #brief-status {
        height: 1; color: $text-muted; margin-top: 1;
    }
    GoalAndClaimsPanel > #brief-status:focus { background: $primary-muted; }
    GoalAndClaimsPanel > #brief-actions { height: 1; margin-top: 1; }
    GoalAndClaimsPanel #brief-actions > Button {
        width: 1fr; height: 1; min-height: 1; border: none;
    }
    GoalAndClaimsPanel #claims-organize { background: $primary; color: $text; margin-right: 1; }
    GoalAndClaimsPanel #claims-organize:hover { background: $primary-lighten-1; }
    GoalAndClaimsPanel #brief-preview { background: $panel; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._render_state: GoalViewState | None = GoalViewState()
        self._render_claim_state = ClaimViewState()
        self._render_brief_state = BriefViewState()
        self._groups = {}
        self._lookup = {}
        self._sync_pending = False
        self._sync_retries = 0

    def compose(self) -> ComposeResult:
        yield BriefZones(
            (
                ZoneSpec("goal", "Goal", lambda: (Static(id="goal-empty"), EntryList(id="goal-body"))),
                ZoneSpec(
                    "requirements",
                    "Requirements",
                    lambda: (Static(id="requirements-empty"), EntryList(id="requirements-body")),
                ),
                ZoneSpec(
                    "constraints",
                    "Constraints",
                    lambda: (Static(id="constraints-empty"), EntryList(id="constraints-body")),
                ),
                ZoneSpec(
                    "claims",
                    "Claims",
                    lambda: (
                        Static(id="claims-empty"),
                        EntryList(id="claim-list", show_meta=False),
                    ),
                ),
                ZoneSpec("tensions", "Tensions", lambda: (Static(id="tensions-empty"), EntryList(id="tensions-body"))),
                ZoneSpec(
                    "already-read",
                    "Read coverage",
                    lambda: (Static(id="already-read-empty"), EntryList(id="already-read-body")),
                    description="Distinct files with confirmed read ranges; not all searched, tested or accessed files.",
                ),
                ZoneSpec(
                    "attention", "Weak coverage", lambda: (Static(id="attention-empty"), EntryList(id="attention-body"))
                ),
                ZoneSpec(
                    "freshness",
                    "Evidence status",
                    lambda: (Static(id="freshness-empty"), EntryList(id="freshness-body")),
                    show_count=False,
                ),
            ),
            initially_open="goal",
        )
        yield DetailLauncher(id="brief-status")
        with Horizontal(id="brief-actions"):
            yield Button("Maintain", id="claims-organize", variant="primary", compact=True)
            yield Button("Preview Brief", id="brief-preview", compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "claims-organize":
            self.post_message(FeatureActionRequested(feature_name=FEATURE_NAME, action=ACTION_MAINTAIN))
        elif event.button.id == "brief-preview":
            self.app.push_screen(BriefPreviewScreen(self._render_brief_state.text))

    @on(OptionList.OptionSelected)
    def on_entry_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option_list, EntryList) and event.option.id in self._lookup:
            event.stop()
            self.app.push_screen(
                DetailScreen(
                    event.option_list.entries,
                    event.option.id,
                    self._lookup,
                    revision=self._render_claim_state.state.revision if self._render_claim_state.state else 0,
                    owner=self,
                )
            )

    @on(DetailLauncher.Selected)
    def on_status_selected(self, event):
        event.stop()
        entry = DetailEntry("maintenance", "Maintenance", "", _maintenance_status_text(self._render_claim_state).plain)
        self.app.push_screen(
            DetailScreen(
                (entry,),
                entry.id,
                {},
                owner=self,
                revision=self._render_claim_state.state.revision if self._render_claim_state.state else 0,
            )
        )

    def on_mount(self) -> None:
        if self.goal_state is not None:
            self._render_state = self.goal_state
        if self.claim_state is not None:
            self._render_claim_state = self.claim_state
        if self.brief_state is not None:
            self._render_brief_state = self.brief_state
        self._request_sync()

    def set_feature_state(self, state: object) -> None:
        """Apply one backend projection.

        The host routes a single object per panel, so Goal and Claims arrive
        together. Either half may be absent — a Goal exists long before the
        first Claim — and an absent half must leave the other one alone rather
        than blanking it.
        """
        if isinstance(state, GoalViewState):
            self.goal_state = state
        elif isinstance(state, ClaimViewState):
            self.claim_state = state
        elif isinstance(state, GoalAndClaimsViewState):
            if state.goal is not None:
                self.goal_state = state.goal
            if state.claims is not None:
                self.claim_state = state.claims
            if state.brief is not None:
                self.brief_state = state.brief

    def watch_brief_state(self, state: BriefViewState | None) -> None:
        if state is None:
            return
        self._render_brief_state = state
        self._sync_retries = 0
        self._sync_state()

    def watch_goal_state(self, state: GoalViewState | None) -> None:
        if state is None:
            return
        if state != self._render_state and self.is_mounted:
            screen = self.app.screen
            if isinstance(screen, DetailScreen) and screen.owner is self:
                screen.dismiss()
        self._render_state = state
        self._sync_retries = 0
        self._sync_state()

    def watch_claim_state(self, state: ClaimViewState | None) -> None:
        if state is None:
            return
        self._render_claim_state = state
        self._sync_retries = 0
        self._sync_state()

    def _request_sync(self) -> None:
        if self._sync_pending:
            return
        self._sync_pending = self.call_after_refresh(self._sync_state)

    def _retry_sync(self) -> None:
        if self._sync_retries >= self._MAX_SYNC_RETRIES:
            return
        self._sync_retries += 1
        self._request_sync()

    def _sync_state(self) -> None:
        self._sync_pending = False
        goal = self._render_state
        if goal is None:
            return
        try:
            zones = self.query_one(BriefZones)
            self.query_one("#goal-body", EntryList)
            # Child sections may not have completed composition yet.
            self.query_one("#zone-claims-header", Button)
        except NoMatches:
            self._retry_sync()
            return

        claims = self._render_claim_state
        brief = self._render_brief_state
        self._groups, self._lookup = brief_entries(goal, claims, brief)
        empty_messages = {
            "goal": "No goal yet. Start a task to populate the Brief.",
            "requirements": "No requirements yet.",
            "constraints": "No constraints yet.",
            "claims": "No active claims yet.",
            "tensions": "No current tensions recorded.",
            "already-read": "No verified file-read coverage yet.",
            "attention": "No overdue weak-coverage reminders. This does not prove requirements are met.",
            "freshness": "No evidence recorded yet.",
        }
        for key, entries in self._groups.items():
            listing = self.query_one("#claim-list" if key == "claims" else f"#{key}-body", EntryList)
            listing.set_entries(entries)
            listing.display = bool(entries)
            empty = self.query_one(f"#{key}-empty", Static)
            empty.update(Text(empty_messages[key], style="dim"))
            empty.display = not entries
        zones.update_counts({key: len(entries) for key, entries in self._groups.items()})
        self.query_one("#brief-status", Static).update(_maintenance_status_text(claims, compact=True))
        self.query_one("#claims-organize", Button).disabled = (
            claims.maintenance is not None and claims.maintenance.phase in {"running", "queued"}
        )
        self._sync_retries = 0


class EvidencePanel(FeaturePanel, can_focus=False):
    """The read-only ledger: what the session actually observed."""

    feature_name = FEATURE_NAME

    evidence_state: reactive[EvidenceViewState | None] = reactive(None, always_update=True)
    _MAX_SYNC_RETRIES = 3

    DEFAULT_CSS = """
    EvidencePanel {
        height: 100%;
        width: 100%;
        padding: 0 1;
    }
    EvidencePanel > Static {
        height: auto;
    }
    EvidencePanel > .evidence-label {
        text-style: bold;
        color: $text-muted;
    }
    EvidencePanel > #evidence-content {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $border-opacity;
        scrollbar-size: 1 1;
        overflow-x: hidden;
        padding: 0 0 0 1;
        display: none;
    }
    EvidencePanel #evidence-body {
        width: 100%;
        height: auto;
    }
    EvidencePanel > #evidence-empty {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $border-opacity;
        color: $text-muted;
        content-align: center middle;
    }
    SidebarPanel.-shell-active EvidencePanel > #evidence-content,
    SidebarPanel.-shell-active EvidencePanel > #evidence-empty {
        border: round $tui-border-warning-darken-1 $border-opacity;
    }
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._render_state: EvidenceViewState | None = None
        self._selected_ref = ""
        self._sync_pending = False
        self._sync_retries = 0

    def compose(self) -> ComposeResult:
        yield Static("Evidence", classes="evidence-label")
        with VerticalScroll(id="evidence-content"):
            yield Static(id="evidence-body")
        yield SidebarEmptyStateLabel("No evidence yet", id="evidence-empty")

    def on_mount(self) -> None:
        if self.evidence_state is not None:
            self._render_state = self.evidence_state
        self._request_sync()

    def set_feature_state(self, state: object) -> None:
        """Apply one backend projection of the ledger."""
        if isinstance(state, EvidenceViewState):
            self.evidence_state = state

    def watch_evidence_state(self, state: EvidenceViewState | None) -> None:
        if state is None:
            return
        self._render_state = state
        self._sync_retries = 0
        self._sync_state()

    def focus_evidence(self, evidence_ref: str) -> None:
        """Highlight and scroll toward a canonical Evidence ID."""
        self._selected_ref = evidence_ref
        self._sync_state()

    def _request_sync(self) -> None:
        if self._sync_pending:
            return
        self._sync_pending = self.call_after_refresh(self._sync_state)

    def _retry_sync(self) -> None:
        if self._sync_retries >= self._MAX_SYNC_RETRIES:
            return
        self._sync_retries += 1
        self._request_sync()

    def _sync_state(self) -> None:
        self._sync_pending = False
        state = self._render_state
        if state is None:
            return
        try:
            label = self.query_one(".evidence-label", Static)
            scroller = self.query_one("#evidence-content", VerticalScroll)
            body = self.query_one("#evidence-body", Static)
            empty = self.query_one("#evidence-empty", Static)
        except NoMatches:
            self._retry_sync()
            return

        if not state.items:
            label.update(Text("Evidence"))
            body.update(Text(""))
            scroller.display = False
            empty.display = True
            self._sync_retries = 0
            return

        errors = sum(1 for item in state.items if item.outcome == "error")
        tests = sum(1 for item in state.items if item.kind == "test")
        label.update(Text(f"Evidence ({len(state.items)}, {tests} tests, {errors} errors)"))

        success = self.app.theme_variables.get("success", "green")
        error = self.app.theme_variables.get("error", "red")
        warning = self.app.theme_variables.get("warning", "yellow")
        body.update(
            _evidence_renderable(
                state,
                success_style=rich_style_from_textual_color(success, bold=True),
                error_style=rich_style_from_textual_color(error, bold=True),
                change_style=rich_style_from_textual_color(warning, bold=True),
                selected_ref=self._selected_ref,
            )
        )
        selected_offset = _evidence_row_offset(state, self._selected_ref)
        if selected_offset is not None:
            scroller.scroll_to(y=selected_offset, animate=False, force=True)
        scroller.display = True
        empty.display = False
        self._sync_retries = 0


def _evidence_row_offset(state: EvidenceViewState, evidence_ref: str) -> int | None:
    """Return an approximate rendered row offset for Evidence navigation."""
    if not evidence_ref:
        return None
    row = 0
    current_phase = ""
    for item in state.items[-MAX_EVIDENCE_DISPLAY_RECORDS:]:
        phase = item.phase or "workflow"
        if phase != current_phase:
            current_phase = phase
            row += 2
        if f"E-{item.sequence:03d}" == evidence_ref:
            return row
        row += 1 + bool(item.excerpt)
    return None
