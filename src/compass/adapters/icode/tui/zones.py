# Copyright (c) 2026 Chrys. All rights reserved.

"""Native tabs switch Compass modules inside the same dialog."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TabPane


@dataclass(frozen=True)
class ZoneSpec:
    key: str
    title: str
    create: Callable[[], Iterable[Widget]]
    show_count: bool = True
    description: str = ""


class BriefZone(TabPane):
    DEFAULT_CSS = """
    BriefZone { height: 1fr; padding: 0 1; }
    BriefZone > .module-title { height: 1; text-style: bold; color: $accent; margin-bottom: 1; }
    BriefZone > Vertical { height: 1fr; min-height: 1; }
    BriefZone Static { height: auto; width: 100%; }
    """

    def __init__(self, spec: ZoneSpec):
        self.spec = spec
        self.body = Vertical(*spec.create(), id=f"zone-{spec.key}-body")
        super().__init__(spec.title, id=f"zone-{spec.key}")

    def compose(self) -> ComposeResult:
        yield Static(self.spec.title, classes="module-title")
        yield self.body


class BriefZones(Vertical):
    """One scrollable content pane; native tabs handle overflow and keyboard access."""

    DEFAULT_CSS = """
    BriefZones { height: 1fr; min-height: 5; margin: 1 0; }
    BriefZones > TabbedContent { height: 1fr; }
    BriefZones ContentTabs Tab { text-style: bold; }
    BriefZones ContentSwitcher { height: 1fr; min-height: 1; }
    """

    def __init__(self, specs: tuple[ZoneSpec, ...], *, initially_open: str):
        super().__init__()
        self.initially_open = initially_open
        self.zones = tuple(BriefZone(spec) for spec in specs)

    def compose(self) -> ComposeResult:
        with TabbedContent(initial=f"zone-{self.initially_open}"):
            yield from self.zones

    def update_counts(self, counts: dict[str, int]) -> None:
        for zone in self.zones:
            tab = self.query_one(TabbedContent).get_tab(zone)
            tab.label = zone.spec.title + (f" ({counts[zone.spec.key]})" if zone.spec.show_count else "")
            tab.tooltip = zone.spec.description or None
