"""Pure request/result data and the small backend boundary used by the Compass workflow.

Saved results never contain clients or live state.
Completion means the agent workflow completed, not that an external verifier passed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast


@dataclass(frozen=True)
class RunRequest:
    task: str
    workspace: Path
    directory: Path

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("task must be nonempty")
        if not self.workspace.is_absolute() or not self.workspace.is_dir():
            raise ValueError("workspace must be an existing absolute directory")
        if not self.directory.is_absolute():
            raise ValueError("directory must be absolute")


@dataclass(frozen=True)
class TurnResult:
    text: str
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("The agent did not produce a final message")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("The host did not return a session ID")


@dataclass(frozen=True)
class RunResult:
    status: Literal["completed", "failed", "timeout"]
    text: str = ""
    session_id: str | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "timeout"}:
            raise ValueError("Unknown run status")
        if self.status == "completed":
            if self.error_type is not None:
                raise ValueError("Completed results cannot carry an error")
            TurnResult(self.text, cast(str, self.session_id))
        elif not self.error_type:
            raise ValueError("Failed results must name an error type")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunBackend(Protocol):
    async def start(self) -> None: ...
    async def run(self, message: str) -> TurnResult: ...


class BudgetExhausted(RuntimeError):
    """The fixed Compass call or wall budget has been reached."""


class InvalidCompassRun(RuntimeError):
    """The required handoff and Brief were not consumed."""
