# Copyright (c) 2026 Chrys. All rights reserved.

"""The seams between the Claim Layer core and one Coding Agent.

Every host-specific capability is declared here as a Protocol. A new target
implements these in their host adapter; nothing in the core changes.

What is deliberately *not* here: anything the core can decide on its own. A
Protocol that only forwards a value the core already holds is a cost with no
buyer, and it makes the real seams harder to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import BinaryIO


class SessionArtifactStore(Protocol):
    """Crash-safe, owner-only persistence for one session's Claim artifacts.

    Claims, Briefs and Evidence are audit records: a torn write loses the reason
    a decision was made, and world-readable ones leak repository contents. Hosts
    already solve this — atomic replace, no-follow opens, owner verification,
    Windows DACLs — so the core asks rather than reimplements.
    """

    def write_text(self, path: Path, payload: str) -> None:
        """Atomically replace *path* with owner-only encoded text."""
        ...

    def open_verified_binary(self, path: Path) -> BinaryIO:
        """Open an existing owner-verified file for binary reading."""
        ...


class EventSink(Protocol):
    """Where the Claim Layer announces what it committed.

    The layer publishes its own events plus one host-shaped warning; a target
    without an event bus can drop them and lose only the UI's live view, never
    a committed Claim.
    """

    async def publish(self, event: object) -> None:
        """Deliver one event to whoever is listening."""
        ...


class MaintenanceModel(Protocol):
    """One structured, tool-free side call that proposes a Claim delta.

    The host owns provider wiring and chat options; the core owns the schema.
    """

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelReply:
        """Return the model's raw text plus whatever usage it reported."""
        ...


@dataclass(frozen=True, slots=True)
class ModelReply:
    """One Maintenance response, before the Claim Layer parses it."""

    text: str
    usage_details: Mapping[str, Any] | None = None


class MissingArtifactStore:
    """Require a host's secure file operations whenever persistence is requested."""

    def write_text(self, path: Path, payload: str) -> None:
        raise RuntimeError("persistent Claim state requires a secure artifact store")

    def open_verified_binary(self, path: Path) -> BinaryIO:
        raise RuntimeError("persistent Claim state requires a secure artifact store")
