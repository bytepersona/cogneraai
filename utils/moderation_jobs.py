"""Job-Payload für die asynchrone Moderations-Warteschlange."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModerationJob:
    """Minimaldaten — Message wird im Worker per fetch nachgeladen."""

    guild_id: int
    channel_id: int
    message_id: int
