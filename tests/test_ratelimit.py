"""Contract tests for ``boe_insights_mcp.ratelimit``."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from boe_insights_mcp.ratelimit import RateLimiter, RateLimiterStats


class FakeTime:
    """Fake clock whose sleep advances time and yields to the event loop once."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


def make_limiter(fake: FakeTime, interval: float = 0.5, concurrency: int = 2) -> RateLimiter:
    return RateLimiter(
        min_interval=interval,
        max_concurrency=concurrency,
        clock=fake.clock,
        sleep=fake.sleep,
    )


async def test_first_entry_is_immediate_without_sleep(fake: FakeTime) -> None:
    limiter = make_limiter(fake)
    async with limiter:
        pass
    assert fake.sleeps == []
    assert fake.now == 100.0


async def test_aenter_returns_the_limiter(fake: FakeTime) -> None:
    limiter = make_limiter(fake)
    async with limiter as entered:
        assert entered is limiter


async def test_sequential_entries_are_spaced_by_min_interval(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5)
    grants: list[float] = []
    for _ in range(3):
        async with limiter:
            grants.append(fake.now)
    assert grants[1] - grants[0] >= 0.5
    assert grants[2] - grants[1] >= 0.5


async def test_no_sleep_when_enough_time_already_passed(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5)
    async with limiter:
        pass
    fake.now += 5
    async with limiter:
        pass
    assert fake.sleeps == []


async def test_only_remaining_interval_is_slept(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5)
    async with limiter:
        pass
    fake.now += 0.2
    async with limiter:
        pass
    assert fake.sleeps == [pytest.approx(0.3)]


async def test_concurrent_grants_are_spaced(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5, concurrency=10)
    grants: list[float] = []

    async def worker() -> None:
        async with limiter:
            grants.append(fake.now)

    await asyncio.gather(*(worker() for _ in range(5)))
    assert len(grants) == 5
    for earlier, later in itertools.pairwise(grants):
        assert later - earlier >= 0.5 - 1e-9
    assert grants[-1] - grants[0] >= 4 * 0.5 - 1e-9


async def test_grants_are_fifo(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5, concurrency=1)
    release = asyncio.Event()
    holding = asyncio.Event()
    order: list[int] = []

    async def holder() -> None:
        async with limiter:
            holding.set()
            await release.wait()

    async def worker(i: int) -> None:
        async with limiter:
            order.append(i)

    async with asyncio.timeout(1):
        hold = asyncio.create_task(holder())
        await holding.wait()
        tasks = []
        for i in range(6):
            tasks.append(asyncio.create_task(worker(i)))
            for _ in range(5):  # ensure this worker is queued before the next starts
                await asyncio.sleep(0)
        assert order == []  # all six are genuinely waiting on the held slot
        release.set()
        await asyncio.gather(hold, *tasks)
    assert order == list(range(6))


async def test_never_exceeds_max_concurrency(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0, concurrency=2)
    running = 0
    peak = 0

    async def worker() -> None:
        nonlocal running, peak
        async with limiter:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            running -= 1

    await asyncio.gather(*(worker() for _ in range(8)))
    assert peak == 2


async def test_max_concurrency_one_serialises_bodies(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0, concurrency=1)
    running = 0
    peak = 0

    async def worker() -> None:
        nonlocal running, peak
        async with limiter:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1

    await asyncio.gather(*(worker() for _ in range(4)))
    assert peak == 1


async def test_zero_interval_disables_spacing(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0, concurrency=5)

    async def worker() -> None:
        async with limiter:
            pass

    await asyncio.gather(*(worker() for _ in range(5)))
    assert fake.sleeps == []
    assert limiter.stats.total_wait == 0


async def test_exception_in_body_propagates_and_releases_slot(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0, concurrency=1)
    with pytest.raises(RuntimeError, match="boom"):
        async with limiter:
            raise RuntimeError("boom")
    async with asyncio.timeout(1):
        async with limiter:
            pass
    assert limiter.stats.acquired == 2


async def test_cancelled_waiter_does_not_leak_slot(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0, concurrency=1)
    release = asyncio.Event()

    async def holder() -> None:
        async with limiter:
            await release.wait()

    async def waiter() -> None:
        async with limiter:
            pass

    hold = asyncio.create_task(holder())
    await asyncio.sleep(0)
    wait = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait
    release.set()
    await hold
    async with asyncio.timeout(1):
        async with limiter:
            pass


async def test_cancelled_waiter_does_not_block_later_tasks(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5, concurrency=1)
    release = asyncio.Event()
    done: list[str] = []

    async def holder() -> None:
        async with limiter:
            await release.wait()

    async def waiter(name: str) -> None:
        async with limiter:
            done.append(name)

    hold = asyncio.create_task(holder())
    await asyncio.sleep(0)
    doomed = asyncio.create_task(waiter("doomed"))
    await asyncio.sleep(0)
    later = asyncio.create_task(waiter("later"))
    await asyncio.sleep(0)
    doomed.cancel()
    release.set()
    await asyncio.wait_for(asyncio.gather(hold, later), timeout=1)
    assert done == ["later"]
    assert doomed.cancelled()


async def test_stats_start_at_zero(fake: FakeTime) -> None:
    limiter = make_limiter(fake)
    assert limiter.stats == RateLimiterStats(acquired=0, total_wait=0.0)


async def test_stats_count_acquisitions(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0)
    for _ in range(3):
        async with limiter:
            pass
    assert limiter.stats.acquired == 3


async def test_stats_total_wait_sums_sleeps(fake: FakeTime) -> None:
    limiter = make_limiter(fake, interval=0.5)
    for _ in range(3):
        async with limiter:
            pass
    assert limiter.stats.acquired == 3
    assert limiter.stats.total_wait == pytest.approx(sum(fake.sleeps))
    assert limiter.stats.total_wait == pytest.approx(1.0)


def test_stats_is_frozen() -> None:
    stats = RateLimiterStats(acquired=1, total_wait=0.5)
    with pytest.raises(AttributeError):
        stats.acquired = 2  # type: ignore[misc]


def test_negative_interval_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(min_interval=-0.1)


@pytest.mark.parametrize("bad", [0, -1])
def test_max_concurrency_below_one_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        RateLimiter(max_concurrency=bad)


async def test_task_cancelled_during_spacing_sleep_does_not_leak_slot() -> None:
    now = 100.0
    gate = asyncio.Event()
    sleeping = asyncio.Event()

    def clock() -> float:
        return now

    async def blocking_sleep(delay: float) -> None:
        nonlocal now
        sleeping.set()
        await gate.wait()
        now += delay

    limiter = RateLimiter(min_interval=0.5, max_concurrency=1, clock=clock, sleep=blocking_sleep)
    async with limiter:
        pass

    async def waiter() -> None:
        async with limiter:
            pass

    doomed = asyncio.create_task(waiter())
    await sleeping.wait()
    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    gate.set()
    async with asyncio.timeout(1):
        async with limiter:
            pass
