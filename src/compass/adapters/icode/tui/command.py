"""Native slash-command bridge; all Compass behavior stays outside the host TUI."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Static

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets.chrome.commands import SlashCommandDef
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message
from chrys.service.extensions import EXTENSION_DATA_DIR_NAME
from compass.adapters.icode.commands import brief_history, maintain, status_snapshot
from compass.adapters.icode.tool_session import ToolCompass, command_session

from .brief_text import brief_text
from .status import CompassStatusDialog, literal, text

if TYPE_CHECKING:
    from chrys.orchestration.engine.engine import AgentEngine

_DESCRIPTION = msg("compass.command.view", fallback="View Compass status and Brief history")
_MAINTAIN = msg("compass.command.update", fallback="Update task requirements and Brief")
_MAN = msg(
    "compass.command.manual",
    fallback="/compass checks requirements and Brief maintenance; core policy decides whether a model call is needed. /compass status opens read-only status and Brief history. Both are top-level command suggestions. Neither becomes a user task or marks the Brief as delivered.",
)
_USAGE = msg("compass.command.usage", fallback="Use /compass or /compass status.")
_UNAVAILABLE = msg(
    "compass.command.unavailable",
    fallback="Compass is not enabled for this session. Enable the compass extension on the selected agent first.",
)
_BUSY = msg("compass.command.busy", fallback="Wait for the current agent operation or Compass maintenance to finish.")
_CHANGED = msg(
    "compass.command.changed", fallback="The active session changed. Run the command again in the current session."
)
_NO_TASK = msg(
    "compass.command.no_task",
    fallback="Send a task to the agent first. /compass uses captured user input; the command itself is not a task.",
)
_RUNNING = msg(
    "compass.command.running",
    fallback="Checking requirements and Brief… This may call the selected model. Close becomes available when the operation finishes.",
)
_DONE = msg(
    "compass.command.done",
    fallback="Requirements checked. Maintenance due: {due}. Outcome: {outcome}. Record revision: {revision}. Reason: {reason}. Evidence steps remaining: {remaining}. Evidence processed: {processed}/{latest}.",
)
_FAILED = msg(
    "compass.command.failed",
    fallback="Compass could not complete the operation ({kind}). Earlier saved state is retained.",
)

_MAINTENANCE_ERROR = msg(
    "compass.command.maintenance_error",
    fallback="Brief maintenance reported an error. The last saved Brief is shown below; use /compass status to inspect the recorded state.",
)


def command_definitions(action: Callable[[str], None]) -> tuple[SlashCommandDef, ...]:
    """Parallel top-level choices; typed arguments still use the same dispatcher."""
    return (
        SlashCommandDef(
            "compass",
            _MAINTAIN.bind(),
            action=action,
            allow_while_running=True,
            synopsis="/compass [status]",
            man_page=_MAN.bind(),
        ),
        SlashCommandDef(
            "compass status",
            _DESCRIPTION.bind(),
            action=lambda arg: action("status"),
            allow_while_running=True,
            synopsis="/compass status",
            man_page=_MAN.bind(),
        ),
    )


def dispatch(app: App, arg: str, *, engine: AgentEngine | None, session_id: str | None, busy: bool) -> None:
    """Resolve the current build, never fall back to another session's runtime."""
    arg = arg.strip()
    if arg not in {"", "status"}:
        app.notify(text(_USAGE), severity="warning")
        return
    if engine is None:
        app.notify(text(_UNAVAILABLE), severity="warning")
        return
    if session_id and session_id != engine.session_id:
        app.notify(text(_CHANGED), severity="warning")
        return
    directory = engine.session_dir
    runtime = command_session(directory / EXTENSION_DATA_DIR_NAME / "compass" if directory else None, engine.session_id)
    if runtime is None:
        app.notify(text(_UNAVAILABLE), severity="warning")
        return
    if arg == "status":
        app.push_screen(
            CompassStatusDialog(
                status_snapshot(runtime),
                on_maintain=lambda: dispatch(
                    app,
                    "",
                    engine=engine,
                    session_id=session_id,
                    busy=busy or engine.is_running,
                ),
                refresh=lambda: status_snapshot(runtime),
                load_briefs=lambda: brief_history(runtime),
            )
        )
        return
    if busy or runtime.gate.lock.locked() or any(isinstance(s, MaintenanceDialog) for s in app.screen_stack):
        app.notify(text(_BUSY), severity="warning")
        return
    if not (runtime.gate.state().get("compass_tools") or {}).get("user_inputs"):
        app.notify(text(_NO_TASK), severity="warning")
        return
    app.push_screen(MaintenanceDialog(runtime))


class MaintenanceDialog(BaseDialog[None]):
    """Own the maintenance worker; keep native input covered until it completes.

    Textual cancels the screen's worker on unmount/app shutdown. Errors are
    displayed without dumping provider messages (which may contain credentials).
    """

    BINDINGS: ClassVar[list] = [localized_binding("escape", "close", CLOSE_BINDING)]
    DEFAULT_CSS = """
    MaintenanceDialog { align: center middle; }
    MaintenanceDialog > Vertical {
        width: 85%; max-width: 100; height: 70%; padding: 1 2;
        border: round $primary; background: $surface;
    }
    MaintenanceDialog VerticalScroll { height: 1fr; }
    MaintenanceDialog Static { height: auto; margin-bottom: 1; }
    MaintenanceDialog Button { dock: bottom; }
    """

    def __init__(self, runtime: ToolCompass) -> None:
        super().__init__(dismiss_on_backdrop=False)
        self.runtime = runtime
        self.finished = False

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                yield Static(text(_RUNNING), id="compass-maintenance-result", markup=False)
                yield Static("", id="compass-maintenance-brief", markup=False)
            yield Button(text(CLOSE_BINDING), id="compass-maintenance-close", disabled=True)

    def on_mount(self) -> None:
        self.run_worker(self.perform, exclusive=True, exit_on_error=False)

    async def perform(self) -> None:
        try:
            if self.runtime.closed:
                message = text(_CHANGED)
            else:
                result = await maintain(self.runtime)
                message = format_message(
                    _DONE.bind(
                        due=str(result.get("maintenance_due", False)),
                        outcome=result.get("outcome", ""),
                        revision=result.get("revision", 0),
                        reason=result.get("reason") or "—",
                        remaining=result.get("remaining_evidence_steps", 0),
                        processed=result.get("processed_evidence_seq", 0),
                        latest=result.get("latest_evidence_seq", 0),
                    )
                )
                if result.get("maintenance_error"):
                    message = text(_MAINTENANCE_ERROR)
                self.query_one("#compass-maintenance-brief", Static).update(brief_text(result.get("brief", "")))
        except Exception as exc:
            message = format_message(_FAILED.bind(kind=type(exc).__name__))
        self.query_one("#compass-maintenance-result", Static).update(literal(message))
        self.finished = True
        self.query_one(Button).disabled = False

    def action_close(self) -> None:
        if self.finished:
            self.dismiss()

    @on(Button.Pressed, "#compass-maintenance-close")
    def close_result(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()
