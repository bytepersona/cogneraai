"""Async Circuit Breaker für externe API-Aufrufe (Anthropic)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Circuit ist geöffnet — Aufruf wird nicht durchgeführt."""


class AsyncCircuitBreaker:
    """
    Nach `failure_threshold` aufeinanderfolgenden Fehlern wird der Circuit geöffnet.
    Nach `reset_timeout_s` Halboffen: ein erfolgreicher Aufruf schließt wieder.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 60.0,
        name: str = "default",
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.reset_timeout_s = reset_timeout_s
        self.name = name
        self._failures = 0
        self._state = "closed"
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def call(self, func: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            await self._maybe_transition_half_open()
            if self._state == "open":
                logger.warning(
                    "CircuitBreaker[%s] geöffnet — Aufruf verworfen.",
                    self.name,
                )
                raise CircuitOpenError(f"CircuitBreaker {self.name} ist offen.")

        try:
            result = await func()
        except Exception:
            async with self._lock:
                self._failures += 1
                logger.warning(
                    "CircuitBreaker[%s] Fehler (%s/%s)",
                    self.name,
                    self._failures,
                    self.failure_threshold,
                )
                if self._failures >= self.failure_threshold:
                    self._state = "open"
                    self._opened_at = time.monotonic()
            raise

        async with self._lock:
            self._failures = 0
            self._state = "closed"
            self._opened_at = None
        return result

    async def _maybe_transition_half_open(self) -> None:
        if self._state != "open":
            return
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at >= self.reset_timeout_s:
            self._state = "half-open"
            logger.info("CircuitBreaker[%s] halb-offen (Testaufruf).", self.name)
