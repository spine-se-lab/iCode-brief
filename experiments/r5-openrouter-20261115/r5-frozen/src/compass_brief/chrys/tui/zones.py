# Copyright (c) 2026 Chrys. All rights reserved.

"""A small data-driven accordion. Domain projections belong to the panel."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button


@dataclass(frozen=True)
class ZoneSpec:
    key: str
    title: str
    create: Callable[[], Iterable[Widget]]
    show_count: bool = True
    description: str = ""


class BriefZone(Vertical):
    DEFAULT_CSS = """
    BriefZone { height: auto; min-height: 1; }
    BriefZone > Button {
        width: 100%; height: 1; min-height: 1; border: none;
        text-align: left; content-align: left middle; padding: 0 1; background: $panel;
    }
    BriefZone > Button:focus { text-style: bold; background: $primary-muted; }
    BriefZone > VerticalScroll {
        height: 1fr; min-height: 1; padding: 1 1 0 1;
        overflow-x: hidden; scrollbar-size: 1 1;
    }
    BriefZone Static { height: auto; width: 100%; }
    """

    def __init__(self, spec: ZoneSpec, *, expanded: bool = False):
        self.spec = spec
        self.count = 0
        self.body = VerticalScroll(*spec.create(), id=f"zone-{spec.key}-body")
        self.body.display = expanded
        super().__init__(id=f"zone-{spec.key}")
        self.styles.height = "1fr" if expanded else "auto"

    def compose(self) -> ComposeResult:
        header = Button(self._label(), id=f"zone-{self.spec.key}-header", compact=True)
        header.tooltip = self.spec.description or None
        # Disclosure controls must respond to consecutive toggles immediately.
        header.active_effect_duration = 0
        yield header
        yield self.body

    def _label(self) -> str:
        count = f" ({self.count})" if self.spec.show_count else ""
        return f"{'▾' if self.body.display else '▸'} {self.spec.title}{count}"

    def set_projection_count(self, count: int) -> None:
        self.count = count
        self.query_one(Button).label = self._label()

    def set_expanded(self, expanded: bool) -> None:
        if self.body.display == expanded:
            return
        if not expanded and self.body.has_focus_within:
            self.query_one(Button).focus()
        self.body.display = expanded
        self.styles.height = "1fr" if expanded else "auto"
        self.query_one(Button).label = self._label()


class BriefZones(Vertical):
    """At most one scrollable body, with all group headers always accessible."""

    DEFAULT_CSS = "BriefZones { height: 1fr; min-height: 5; }"

    def __init__(self, specs: tuple[ZoneSpec, ...], *, initially_open: str):
        super().__init__()
        self.zones = tuple(BriefZone(spec, expanded=spec.key == initially_open) for spec in specs)
        self.fallback_key = initially_open

    def compose(self) -> ComposeResult:
        yield from self.zones

    def on_button_pressed(self, event: Button.Pressed) -> None:
        zone = next((zone for zone in self.zones if event.button.parent is zone), None)
        if zone is None:
            return
        event.stop()
        expand = not zone.body.display
        for candidate in self.zones:
            candidate.set_expanded(candidate is zone and expand)

    def update_counts(self, counts: dict[str, int]) -> None:
        for zone in self.zones:
            previous = zone.count
            zone.set_projection_count(counts[zone.spec.key])
            if previous and not zone.count and zone.body.display:
                for candidate in self.zones:
                    candidate.set_expanded(candidate.spec.key == self.fallback_key)
