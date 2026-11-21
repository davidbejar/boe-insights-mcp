"""Shared rate limiter that keeps outgoing requests polite to boe.es.

Algorithm: every request first takes one of ``max_concurrency`` slots (an
``asyncio.Semaphore``, which wakes waiters in FIFO order). It then takes a
spacing lock, also FIFO, and sleeps for whatever remains of ``min_interval``
since the previous grant began; the grant time is read from the clock after
that sleep. If the task fails or is cancelled anywhere between taking the slot
and finishing the sleep, the slot is released, so nothing leaks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["RateLimiter", "RateLimiterStats"]


@dataclass(frozen=True)
class RateLimiterStats:
    """Snapshot of limiter activity."""

    acquired: int
    """Number of completed acquisitions."""
    total_wait: float
    """Seconds passed to ``sleep`` for spacing, summed."""


class RateLimiter:
    """Async context manager bounding request spacing and concurrency.

    Use ``async with limiter:`` around each request.
    """

    def __init__(
        self,
        min_interval: float = 0.5,
        max_concurrency: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Configure the minimum seconds between request starts and the concurrency cap."""
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        # asyncio primitives bind to the running loop on first use (Python 3.10+).
        self._slots = asyncio.Semaphore(max_concurrency)
        self._spacing = asyncio.Lock()
        self._last_grant: float | None = None
        self._acquired = 0
        self._total_wait = 0.0

    async def __aenter__(self) -> RateLimiter:
        """Wait for a free slot and for the spacing interval to elapse."""
        await self._slots.acquire()
        try:
            async with self._spacing:
                if self._last_grant is not None:
                    delay = self._last_grant + self._min_interval - self._clock()
                    if delay > 0:
                        await self._sleep(delay)
                        self._total_wait += delay
                self._last_grant = self._clock()
                self._acquired += 1
        except BaseException:
            self._slots.release()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Release the slot, whether or not the body raised."""
        self._slots.release()

    @property
    def stats(self) -> RateLimiterStats:
        """Current acquisition count and total spacing wait."""
        return RateLimiterStats(acquired=self._acquired, total_wait=self._total_wait)
