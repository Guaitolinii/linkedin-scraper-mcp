"""Pace browser tool calls so an autonomous agent does not hammer LinkedIn.

An agent that researches on its own can issue many tool calls back to back.
Each one drives a real browser against LinkedIn, and a burst of page loads is
what gets an account throttled or restricted. This module spaces the calls out:

* a minimum quiet gap, plus a little random jitter, between the end of one call
  and the start of the next, so the traffic does not look machine-regular;
* a ceiling on calls per rolling minute.

It only ever delays a call. It never refuses one and never retries one, so a
paced call behaves exactly like an unpaced call that started a few seconds
later.

Both limits are set through the environment and are on by default:

* ``LINKEDIN_MIN_CALL_GAP_SECONDS`` (default ``3``), the minimum gap;
* ``LINKEDIN_CALL_GAP_JITTER_SECONDS`` (default ``1.5``), the random extra;
* ``LINKEDIN_MAX_CALLS_PER_MINUTE`` (default ``12``), ``0`` turns the ceiling off.

New file for this project (Apache-2.0). Not part of the upstream
linkedin-scraper-mcp.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
import logging
import os
import random
import time

logger = logging.getLogger(__name__)

DEFAULT_MIN_GAP_SECONDS = 3.0
DEFAULT_JITTER_SECONDS = 1.5
DEFAULT_MAX_CALLS_PER_MINUTE = 12
_WINDOW_SECONDS = 60.0


def _read_number(
    env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0
) -> float:
    """Read one non-negative number from *env*, falling back on anything unusable."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if value != value or value < minimum:  # NaN or below the floor
        logger.warning("%s=%r is out of range; using %s", name, raw, default)
        return default
    return value


class CallPacer:
    """Spaces calls out. Not thread-safe: use it under the browser lock."""

    def __init__(
        self,
        *,
        min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS,
        jitter_seconds: float = DEFAULT_JITTER_SECONDS,
        max_calls_per_minute: int = DEFAULT_MAX_CALLS_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._min_gap = max(0.0, min_gap_seconds)
        self._jitter = max(0.0, jitter_seconds)
        self._max_per_minute = max(0, int(max_calls_per_minute))
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._last_end: float | None = None
        self._starts: deque[float] = deque()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CallPacer:
        """Build a pacer from the ``LINKEDIN_*`` pacing variables."""
        source = os.environ if env is None else env
        return cls(
            min_gap_seconds=_read_number(
                source, "LINKEDIN_MIN_CALL_GAP_SECONDS", DEFAULT_MIN_GAP_SECONDS
            ),
            jitter_seconds=_read_number(
                source, "LINKEDIN_CALL_GAP_JITTER_SECONDS", DEFAULT_JITTER_SECONDS
            ),
            max_calls_per_minute=int(
                _read_number(
                    source,
                    "LINKEDIN_MAX_CALLS_PER_MINUTE",
                    DEFAULT_MAX_CALLS_PER_MINUTE,
                )
            ),
        )

    @property
    def enabled(self) -> bool:
        """Whether this pacer can ever delay a call."""
        return self._min_gap > 0 or self._jitter > 0 or self._max_per_minute > 0

    def delay_needed(self) -> float:
        """Seconds the next call has to wait from now, without waiting."""
        now = self._clock()
        delay = 0.0
        if self._last_end is not None and (self._min_gap > 0 or self._jitter > 0):
            gap = self._min_gap + self._rng() * self._jitter
            delay = max(delay, self._last_end + gap - now)
        if self._max_per_minute > 0:
            self._forget_old(now)
            if len(self._starts) >= self._max_per_minute:
                oldest = self._starts[0]
                delay = max(delay, oldest + _WINDOW_SECONDS - now)
        return max(0.0, delay)

    async def wait(
        self, on_delay: Callable[[float], Awaitable[None]] | None = None
    ) -> float:
        """Wait until the next call may start, record it, return seconds waited.

        *on_delay*, when given, is awaited with the delay just before sleeping,
        so a caller can tell its client why the call is not starting yet.
        """
        delay = self.delay_needed()
        if delay > 0:
            logger.debug("Pacing the next LinkedIn call by %.2fs", delay)
            if on_delay is not None:
                await on_delay(delay)
            await self._sleep(delay)
        now = self._clock()
        self._forget_old(now)
        self._starts.append(now)
        return delay

    def finished(self) -> None:
        """Record that a call has ended; the quiet gap counts from here."""
        self._last_end = self._clock()

    def _forget_old(self, now: float) -> None:
        while self._starts and now - self._starts[0] >= _WINDOW_SECONDS:
            self._starts.popleft()
