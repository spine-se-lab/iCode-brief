# Copyright (c) 2026 Chrys. All rights reserved.

"""Publish core announcements onto the Chrys event bus.

The core says what it committed in its own vocabulary, because it has to work
on a target whose event base class it has never heard of. The Chrys bus
dispatches by exact type, so a core payload published straight onto it would
reach nobody — silently, with the sidebar simply never updating. This is the
one place that translation happens.

Anything the core publishes that has no Chrys counterpart is forwarded
unchanged rather than dropped: a payload nobody listens to is harmless, and
silently swallowing one would hide the next capability's new event.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

from chrys.foundation.events.types import Warning

from compass_brief import events as core_events
from compass_brief.chrys import events as host_events

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus

__all__ = ["HostEventSink"]

_TRANSLATIONS: Final[dict[type, type]] = {
    core_events.BriefUpdated: host_events.BriefUpdated,
    core_events.GoalUpdated: host_events.GoalUpdated,
    core_events.EvidenceUpdated: host_events.EvidenceUpdated,
    core_events.ClaimsUpdated: host_events.ClaimsUpdated,
    core_events.ClaimsBriefOverflow: host_events.ClaimsBriefOverflow,
    core_events.MaintenanceFailed: Warning,
    core_events.MaintenanceStatusUpdated: host_events.MaintenanceStatusUpdated,
}


class HostEventSink:
    """Adapt the core's :class:`EventSink` port to one Chrys event bus."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def publish(self, event: object) -> None:
        """Translate a core payload if we know it, then publish."""
        await self._bus.publish(_as_host_event(event))


def _as_host_event(event: object) -> Any:
    host_type = _TRANSLATIONS.get(type(event))
    if host_type is None:
        return event
    payload = {field.name: getattr(event, field.name) for field in dataclasses.fields(event)}
    accepted = {field.name for field in dataclasses.fields(host_type)}
    # ``detail`` and any other core-only field is dropped on purpose: the host
    # event is the public shape, and passing an unknown key would raise here
    # rather than at the one place that could act on it.
    return host_type(**{key: value for key, value in payload.items() if key in accepted})
