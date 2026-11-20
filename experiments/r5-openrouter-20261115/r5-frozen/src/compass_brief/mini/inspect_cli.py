# Copyright (c) 2026 Chrys. All rights reserved.

"""Read-only Compass detail query for a host whose only tool is a shell.

The Brief points a model at the full text behind an R/K/C id. Chrys answers that
with an in-process tool; a shell-only host has none, so this reads the trial's
last saved state instead. It is strictly read-only: no model is available, so no
Maintenance can run, and nothing observed here becomes new Evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from functools import partial
from pathlib import Path

from compass_brief.briefing.render import CANONICAL_DETAIL_ENTRY
from compass_brief.mini.presentation import shell_detail_entry
from compass_brief.ports import MissingArtifactStore
from compass_brief.session import CompassSession

STATE_FILE = "session-state.json"

#: The adapter writes this next to the state file. Answers rendered here point at
#: the same entry, so a reply never names a tool this host cannot execute.
DETAIL_SHIM = "inspect"


class _DroppedEvents:
    async def publish(self, event: object) -> None:
        return None


async def _inspect(state_path: Path, refs: list[str]) -> str:
    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    entry = state_path.parent / DETAIL_SHIM
    session = CompassSession(
        # Persistence is never touched: this process only reads the snapshot it
        # was handed, so a store that refuses every write is the honest port.
        artifacts=MissingArtifactStore(),
        events=_DroppedEvents(),
        model=lambda: None,
        detail_entry=str(entry) if entry.exists() else CANONICAL_DETAIL_ENTRY,
        detail_renderer=partial(shell_detail_entry, entry=str(entry)) if entry.exists() else None,
    )
    await session.restore_session_state(envelope)
    return await session.inspect(refs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compass-brief inspect",
        description="Show the full Compass record behind an R, K or C id.",
    )
    parser.add_argument("refs", nargs="*", metavar="REF", help="R/K/C ids; omit for every item")
    parser.add_argument(
        "--state",
        type=Path,
        required=True,
        help=f"The trial's Compass directory, or the {STATE_FILE} inside it",
    )
    arguments = parser.parse_args(argv)

    state_path = arguments.state
    if state_path.is_dir():
        state_path = state_path / STATE_FILE
    if not state_path.is_file():
        # Say which path was tried: a silent empty answer would read as "no such
        # claim" when the run simply has no saved state yet.
        sys.stderr.write(f"No saved Compass state at {state_path}\n")
        return 2

    try:
        sys.stdout.write(asyncio.run(_inspect(state_path, arguments.refs)) + "\n")
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"Could not read Compass state at {state_path}: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
