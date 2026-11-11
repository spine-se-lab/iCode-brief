# Copyright (c) 2026 Chrys. All rights reserved.

"""GoalTracker — engine-owned, session-scoped goal state."""

from __future__ import annotations

import asyncio

from compass_brief.task_contract.models import GoalState, parse_goal_state, serialize_goal_state


class GoalTracker:
    """Holds the current session goal as one immutable GoalState.

    Concurrency contract mirrors TodoTracker:

    - **Writers** (:meth:`replace`, :meth:`clear`, :meth:`restore`) are async
      and serialize on an ``asyncio.Lock``.
    - **Readers** (:meth:`snapshot`, :meth:`serialize`) are sync and lock-free:
      they read the current GoalState reference, which is only ever swapped,
      never mutated.
    """

    def __init__(self) -> None:
        self._state: GoalState = GoalState()
        self._lock = asyncio.Lock()

    async def replace(self, state: GoalState) -> GoalState:
        """Replace the whole goal, returning the applied snapshot.

        Callers publish ``GoalUpdated`` from the returned snapshot AFTER
        this call returns (never while holding the lock).
        """
        async with self._lock:
            self._state = state
            return self._state

    async def clear(self) -> None:
        """Reset to the empty goal."""
        async with self._lock:
            self._state = GoalState()

    async def restore(self, raw: object) -> GoalState:
        """Hydrate from a persisted ``chrys_goal`` payload; tolerant, never raises."""
        async with self._lock:
            self._state = parse_goal_state(raw)
            return self._state

    def snapshot(self) -> GoalState:
        """Current goal (sync, lock-free)."""
        return self._state

    def serialize(self) -> dict[str, object]:
        """Current goal as the plain-dict ``chrys_goal`` payload (sync, lock-free)."""
        return serialize_goal_state(self._state)
