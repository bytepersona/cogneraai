"""Job-Payload für die asynchrone Moderations-Warteschlange."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModerationJob:
    """Minimaldaten — Message wird im Worker per fetch nachgeladen."""

    guild_id: int
    channel_id: int
    message_id: int
    event_ref: Optional[str] = None
