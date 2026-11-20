"""Original Compass phase order, independent of iCode assembly and transport."""

from __future__ import annotations

import asyncio

from compass.runtime.budget import MAX_CALLS
from compass.runtime.records import InvalidCompassRun, RunBackend, TurnResult

from .prompts import CHECK_FINAL, CHECK_HELD
from .run_state import RunState


class CompassRun:
    def __init__(self, owner: RunState, backend: RunBackend) -> None:
        self.owner = owner
        self.backend = backend

    async def run(self) -> TurnResult:
        run_state = self.owner
        try:
            async with asyncio.timeout(max(0, run_state.remaining())):
                await self.backend.start()
                result = await self.backend.run(run_state.task)
                require_handoff(run_state)
                check = prepare_submission_check(run_state)
                if check is not None:
                    held, prompt = check
                    if held:
                        result = await self.backend.run(prompt)
                if run_state.calls < MAX_CALLS:
                    await run_state.core.maintain(phase="icode", stage_end=True)
                return result
        finally:
            try:
                await run_state.core.before_fork()
            finally:
                run_state.save()


def require_handoff(owner: RunState) -> None:
    if not owner.flags.started or not owner.flags.handoff_seen or not owner.flags.rules_seen:
        raise InvalidCompassRun("Compass handoff/Brief was not consumed; run is not a valid Compass treatment")


def prepare_submission_check(owner: RunState) -> tuple[bool, str] | None:
    """Record the one-time check; each adapter owns how to continue its host."""
    if owner.flags.check_done:
        return None
    owner.flags.check_done = True
    held = MAX_CALLS - owner.calls >= 15 and owner.remaining() > 0
    prompt = owner.constants["SUBMISSION_CHECK"] + (CHECK_HELD if held else CHECK_FINAL)
    owner.audit("submission_check", held=held, text=prompt)
    owner.save()
    return held, prompt
