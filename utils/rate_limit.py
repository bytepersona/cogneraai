"""Einfaches zeitfensterbasiertes Rate-Limit pro Nutzer (API-Kosten)."""

from __future__ import annotations

import time
from typing import Optional
from collections import defaultdict
class SlidingWindowRateLimiter:
    """Erlaubt höchstens `max_calls` Aufrufe pro Nutzer innerhalb von `window_seconds`."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._hits: dict[int, list[float]] = defaultdict(list)

    def allow(self, user_id: int, now: Optional[float] = None) -> bool:
        """True wenn der Aufruf erlaubt ist; bei False sollte keine API angefragt werden."""
        t = now if now is not None else time.monotonic()
        cutoff = t - self._window
        bucket = self._hits[user_id]
        bucket[:] = [x for x in bucket if x >= cutoff]
        if len(bucket) >= self._max_calls:
            return False
        bucket.append(t)
        return True


def monotonic_now() -> float:
    """Test-Hook."""
    return time.monotonic()
