"""Shared rate limiter that keeps outgoing requests polite to boe.es.

Algorithm: every request first takes one of ``max_concurrency`` slots (an
``asyncio.Semaphore``, which wakes waiters in FIFO order). It then takes a
spacing lock, also FIFO, and sleeps for whatever remains of ``min_interval``
since the previous grant began; the grant time is read from the clock after
that sleep. If the task fails or is cancelled anywhere between taking the slot
and finishing the sleep, the slot is released, so nothing leaks.

``RateLimiter`` is created once per server (see ``server._lifespan``) and handed to
``client.BoeClient``, which wraps each HTTP attempt in ``async with limiter:``. The clock and
sleep functions are injectable so tests can run on fake time.
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
        # Order matters. The slot comes first so that at most ``max_concurrency`` tasks are ever
        # past this line, whether they are sleeping for spacing or running their request. The
        # spacing lock is then held *through* the sleep, so the next task computes its delay from
        # this task's grant time, not from a stale one.
        # The slot acquire sits outside the ``try``: a task cancelled while still queueing for a
        # slot holds none, and releasing one it never took would inflate the semaphore.
        await self._slots.acquire()
        try:
            async with self._spacing:
                # Space request *starts* ``min_interval`` apart. ``_last_grant`` is read from the
                # clock after the sleep (below), so an oversleep delays the next grant too
                # instead of being forgotten.
                if self._last_grant is not None:
                    delay = self._last_grant + self._min_interval - self._clock()
                    if delay > 0:
                        await self._sleep(delay)
                        # Counted only once the sleep has completed, so a cancelled sleep adds
                        # nothing to the stats.
                        self._total_wait += delay
                self._last_grant = self._clock()
                self._acquired += 1
        except BaseException:
            # BaseException on purpose: CancelledError is not an ``Exception``. Any failure or
            # cancellation after the slot was taken (while waiting for the spacing lock or
            # sleeping) must give the slot back, because ``__aexit__`` never runs for an
            # ``__aenter__`` that did not complete.
            self._slots.release()
            raise
        # No await between here and the caller's body, so cancellation cannot land after the
        # grant without ``__aexit__`` running.
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Release the slot, whether or not the body raised."""
        self._slots.release()

    @property
    def stats(self) -> RateLimiterStats:
        """Current acquisition count and total spacing wait."""
        return RateLimiterStats(acquired=self._acquired, total_wait=self._total_wait)
