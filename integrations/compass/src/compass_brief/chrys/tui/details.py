"""Read-only list/detail interaction shared by all Brief groups."""

from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option


@dataclass(frozen=True)
class DetailEntry:
    id: str
    title: str
    summary: str
    body: str
    meta: str = ""
    links: tuple[str, ...] = ()
    emphasis: tuple[tuple[int, int], ...] = ()

    def body_text(self) -> Text:
        """Style only labels supplied by the presenter, never interpret user markup."""
        text = Text(self.body)
        for start, end in self.emphasis:
            text.stylize("bold", start, end)
        return text


@dataclass
class EntryPrompt:
    entry: DetailEntry
    selected: bool = False
    show_meta: bool = True

    def __rich_console__(self, console, options):
        yield Text(
            ("> " if self.selected else "  ") + self.entry.title, style="bold reverse" if self.selected else "bold"
        )
        # Width-aware clipping of the original text, never model summarization.
        lines = Text(" ".join(self.entry.summary.split())).wrap(console, max(1, options.max_width))
        for index, line in enumerate(lines[:2]):
            if index == 1 and len(lines) > 2:
                line.truncate(max(1, options.max_width - 1))
                line.append("…")
            yield line
        if self.entry.meta and self.show_meta:
            yield Text(self.entry.meta, style="dim", no_wrap=True, overflow="ellipsis")
        yield Text("")


class EntryList(OptionList):
    """Compact rows; selection emphasizes only their title, not a paragraph."""

    DEFAULT_CSS = """
    EntryList { height: 1fr; min-height: 1; border: none; padding: 0; background: transparent; }
    EntryList:focus { border: none; background-tint: transparent; }
    EntryList > .option-list--option-highlighted,
    EntryList:focus > .option-list--option-highlighted {
        background: transparent; color: $text; text-style: none;
    }
    EntryList > .option-list--option-hover,
    EntryList:focus > .option-list--option-hover { background: transparent; }
    """

    def __init__(self, *, show_meta=True, **kwargs):
        super().__init__(**kwargs)
        self.show_meta = show_meta
        self.entries: tuple[DetailEntry, ...] = ()

    def set_entries(self, entries: tuple[DetailEntry, ...]):
        if entries == self.entries:
            return
        old_id = (
            self.entries[self.highlighted].id
            if self.highlighted is not None and self.highlighted < len(self.entries)
            else None
        )
        old_scroll = self.scroll_y
        self.entries = entries
        self.clear_options()
        self.add_options(Option(EntryPrompt(entry, show_meta=self.show_meta), id=entry.id) for entry in entries)
        if old_id and any(entry.id == old_id for entry in entries):
            self.highlighted = self.get_option_index(old_id)
            self.call_after_refresh(self.scroll_to, y=old_scroll, animate=False)
        elif entries:
            self.highlighted = 0
        self._paint_selection()

    def on_option_list_option_highlighted(self, event):
        if event.option_list is self:
            self._paint_selection()

    def _paint_selection(self):
        for index, entry in enumerate(self.entries):
            self.replace_option_prompt_at_index(index, EntryPrompt(entry, index == self.highlighted, self.show_meta))


class DetailLauncher(Static, can_focus=True):
    """A one-line status with keyboard/mouse access to its full explanation."""

    BINDINGS = (("enter", "inspect", "Details"),)

    class Selected(Message):
        pass

    def action_inspect(self):
        self.post_message(self.Selected())

    def on_click(self):
        self.action_inspect()


class DetailScreen(ModalScreen):
    """A snapshot reader with sibling navigation and explicit return history."""

    BINDINGS = (
        ("escape", "dismiss", "Close"),
        ("left", "previous", "Previous"),
        ("right", "next", "Next"),
        ("backspace", "back", "Back"),
    )
    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    DetailScreen > Vertical {
        width: 90%; height: 85%; border: round $primary; background: $surface; padding: 1 2;
    }
    DetailScreen #detail-title { height: auto; text-style: bold; }
    DetailScreen #detail-context { height: auto; color: $text-muted; }
    DetailScreen #detail-scroll { height: 1fr; margin-top: 1; }
    DetailScreen #detail-body { height: auto; }
    DetailScreen #detail-links-title { height: auto; margin-top: 1; text-style: bold; }
    DetailScreen #detail-links { height: auto; }
    DetailScreen #detail-actions { height: 1; margin-top: 1; }
    DetailScreen #detail-actions Button { width: 1fr; min-width: 1; height: 1; min-height: 1; border: none; }
    """

    def __init__(
        self,
        entries: tuple[DetailEntry, ...],
        selected: str,
        lookup: dict[str, DetailEntry],
        *,
        revision: int = 0,
        owner=None,
    ):
        super().__init__()
        self.entries, self.lookup = entries, lookup
        self.index = next(i for i, entry in enumerate(entries) if entry.id == selected)
        self.revision, self.owner = revision, owner
        self.history = []

    def compose(self):
        with Vertical():
            yield Static(id="detail-title")
            yield Static(id="detail-context")
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail-body")
                yield Static("Related entries — Enter to inspect", id="detail-links-title")
                yield EntryList(id="detail-links")
            with Horizontal(id="detail-actions"):
                yield Button("Back", id="detail-back", compact=True)
                yield Button("Previous", id="detail-previous", compact=True)
                yield Button("Next", id="detail-next", compact=True)
                yield Button("Close (Esc)", id="detail-close", compact=True)

    def on_mount(self):
        self._show()

    def _show(self, scroll_y=0):
        entry = self.entries[self.index]
        self.query_one("#detail-title", Static).update(Text(entry.title))
        self.query_one("#detail-context", Static).update(
            Text(f"{self.index + 1} / {len(self.entries)} · Snapshot rev {self.revision} · Read only")
        )
        self.query_one("#detail-body", Static).update(entry.body_text())
        related = tuple(self.lookup[key] for key in entry.links if key in self.lookup)
        links = self.query_one("#detail-links", EntryList)
        links.set_entries(related)
        links.display = bool(related)
        self.query_one("#detail-links-title").display = bool(related)
        self.query_one("#detail-back", Button).disabled = not self.history
        self.query_one("#detail-previous", Button).disabled = self.index == 0
        self.query_one("#detail-next", Button).disabled = self.index == len(self.entries) - 1
        self.query_one("#detail-scroll").call_after_refresh(
            self.query_one("#detail-scroll").scroll_to, y=scroll_y, animate=False
        )

    @on(OptionList.OptionSelected, "#detail-links")
    def inspect_related(self, event):
        event.stop()
        if event.option.id not in self.lookup:
            return
        self.history.append((self.entries, self.index, self.query_one("#detail-scroll").scroll_y))
        self.entries = self.query_one("#detail-links", EntryList).entries
        self.index = next(i for i, entry in enumerate(self.entries) if entry.id == event.option.id)
        self._show()

    def action_previous(self):
        if self.index > 0:
            self.index -= 1
            self._show()

    def action_next(self):
        if self.index + 1 < len(self.entries):
            self.index += 1
            self._show()

    def action_back(self):
        if self.history:
            self.entries, self.index, scroll_y = self.history.pop()
            self._show(scroll_y)

    def on_button_pressed(self, event):
        event.stop()
        actions = {
            "detail-back": self.action_back,
            "detail-previous": self.action_previous,
            "detail-next": self.action_next,
            "detail-close": self.dismiss,
        }
        if action := actions.get(event.button.id):
            action()
