"""Tests of ``catalogue``: the in-process snapshot of the whole consolidated collection.

The store is exercised against a fake search client that serves slices of a list, so paging,
de-duplication, single flight and cancellation are checked without any network. One test at the
end uses the real client with a recorded fixture. Clocks are injected; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from boe_insights_mcp import catalogue
from boe_insights_mcp.catalogue import CATALOGUE_TTL, CatalogueStore
from boe_insights_mcp.client import BASE_URL, SEARCH_TTL, BoeClient
from boe_insights_mcp.errors import BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import LawSummary
from boe_insights_mcp.ratelimit import RateLimiter
from tests.conftest import RecordingCache
from tests.laws import make_law

START = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


class Clock:
    """A settable UTC clock."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class FakeSearch:
    """Stands in for ``BoeClient.search_laws``: serves slices of ``laws`` and records calls."""

    def __init__(self, laws: list[LawSummary]) -> None:
        self.laws = laws
        self.calls: list[dict[str, object]] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.cancelled = 0
        self.fail_with: Exception | None = None

    async def search_laws(
        self,
        query: str | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        ttl: float | None = None,
    ) -> list[LawSummary]:
        self.calls.append({"query": query, "offset": offset, "limit": limit, "ttl": ttl})
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.fail_with is not None:
            raise self.fail_with
        return self.laws[offset : offset + limit]


def laws_numbered(count: int) -> list[LawSummary]:
    """``count`` laws, newest first like the API, with distinct identifiers."""
    return [
        make_law(f"BOE-A-2020-{n:04d}", published=date(2020, 1, 1) - timedelta(days=n))
        for n in range(count)
    ]


@pytest.fixture(autouse=True)
def small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages of 3 records and at most 4 pages, so paging is testable with a handful of laws."""
    monkeypatch.setattr(catalogue, "CATALOGUE_PAGE_SIZE", 3)
    monkeypatch.setattr(catalogue, "MAX_CATALOGUE_PAGES", 4)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def store_for(fake: FakeSearch, clock: Clock) -> CatalogueStore:
    return CatalogueStore(fake, clock=clock)


# --- get: paging and de-duplication ------------------------------------------------------------


async def test_get_pages_until_a_short_page_and_returns_every_law_in_order(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(7))

    result = await store_for(fake, clock).get()

    assert [law.identifier for law in result.laws] == [law.identifier for law in fake.laws]
    assert [(c["offset"], c["limit"]) for c in fake.calls] == [(0, 3), (3, 3), (6, 3)]


async def test_get_asks_for_publication_order_with_the_day_long_lifetime(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(2))

    await store_for(fake, clock).get()

    assert fake.calls[0]["ttl"] == CATALOGUE_TTL
    # Sorting by publication date is what keeps offsets stable (api-notes, "Bulk download").
    assert json.loads(str(fake.calls[0]["query"])) == {"sort": [{"fecha_publicacion": "desc"}]}


async def test_an_exact_multiple_of_the_page_size_ends_on_an_empty_page(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(6))

    result = await store_for(fake, clock).get()

    assert len(result.laws) == 6
    assert [c["offset"] for c in fake.calls] == [0, 3, 6]


async def test_an_empty_catalogue_is_a_valid_empty_snapshot(clock: Clock) -> None:
    result = await store_for(FakeSearch([]), clock).get()

    assert result.laws == ()
    assert result.newest_update is None


async def test_duplicate_identifiers_are_dropped_keeping_the_first(clock: Clock) -> None:
    # A record added mid-crawl shifts the offsets, so the last record of one page reappears at
    # the start of the next.
    first = make_law("A", title="first copy")
    repeat = make_law("A", title="second copy")
    fake = FakeSearch([first, make_law("B"), make_law("C"), repeat, make_law("D")])

    result = await store_for(fake, clock).get()

    assert [law.identifier for law in result.laws] == ["A", "B", "C", "D"]
    assert result.laws[0].title == "first copy"


async def test_offsets_follow_raw_positions_not_unique_counts(clock: Clock) -> None:
    fake = FakeSearch([make_law("A"), make_law("A"), make_law("A"), make_law("B")])

    await store_for(fake, clock).get()

    assert [c["offset"] for c in fake.calls] == [0, 3]


async def test_too_many_pages_is_a_response_error_and_leaves_no_snapshot(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(100))
    store = store_for(fake, clock)

    with pytest.raises(BoeResponseError):
        await store.get()

    assert len(fake.calls) == 4
    fake.calls.clear()
    with pytest.raises(BoeResponseError):
        await store.get()
    assert len(fake.calls) == 4  # tried again from scratch


async def test_progress_is_awaited_after_each_page(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(7))
    reports: list[tuple[int, int | None, str]] = []

    async def progress(done: int, total: int | None, message: str) -> None:
        reports.append((done, total, message))

    await store_for(fake, clock).get(progress=progress)

    assert [(done, total) for done, total, _ in reports] == [(3, None), (6, None), (7, None)]
    assert all(message for _, _, message in reports)


async def test_a_failing_progress_callback_does_not_break_the_download(clock: Clock) -> None:
    async def broken(done: int, total: int | None, message: str) -> None:
        raise RuntimeError("client went away")

    result = await store_for(FakeSearch(laws_numbered(4)), clock).get(progress=broken)

    assert len(result.laws) == 4


# --- get: snapshot lifetime --------------------------------------------------------------------


async def test_snapshot_records_when_the_download_finished_and_the_newest_update(
    clock: Clock,
) -> None:
    laws = [
        make_law("A", updated_at=datetime(2026, 1, 1, tzinfo=UTC)),
        make_law("B", updated_at=datetime(2026, 5, 1, tzinfo=UTC)),
        make_law("C"),
    ]

    result = await store_for(FakeSearch(laws), clock).get()

    assert result.fetched_at == START
    assert result.newest_update == datetime(2026, 5, 1, tzinfo=UTC)


async def test_a_young_snapshot_is_reused_and_an_old_one_is_refreshed(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(2))
    store = store_for(fake, clock)
    first = await store.get()

    clock.now = START + timedelta(seconds=CATALOGUE_TTL - 1)
    second = await store.get()
    assert second is first
    assert len(fake.calls) == 1

    clock.now = START + timedelta(seconds=CATALOGUE_TTL)
    third = await store.get()
    assert third is not first
    assert third.fetched_at == clock.now
    assert len(fake.calls) == 2


# --- get: single flight, failure and cancellation ----------------------------------------------


async def test_concurrent_callers_share_one_download(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(5))
    fake.gate = asyncio.Event()
    store = store_for(fake, clock)
    callers = [asyncio.create_task(store.get()) for _ in range(3)]
    await fake.started.wait()

    fake.gate.set()
    results = await asyncio.gather(*callers)

    assert results[0] is results[1] is results[2]
    assert len(fake.calls) == 2  # two pages, once


async def test_every_concurrent_caller_gets_progress(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(5))
    fake.gate = asyncio.Event()
    store = store_for(fake, clock)
    seen: dict[str, list[int]] = {"a": [], "b": []}

    def listener(name: str) -> Callable[[int, int | None, str], asyncio.Future[None]]:
        async def report(done: int, total: int | None, message: str) -> None:
            seen[name].append(done)

        return report  # type: ignore[return-value]

    one = asyncio.create_task(store.get(progress=listener("a")))
    two = asyncio.create_task(store.get(progress=listener("b")))
    await fake.started.wait()
    fake.gate.set()
    await asyncio.gather(one, two)

    assert seen["a"] == seen["b"] == [3, 5]


async def test_a_failed_download_reaches_every_waiter_and_leaves_no_snapshot(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(5))
    fake.gate = asyncio.Event()
    fake.fail_with = BoeUnavailableError("down")
    store = store_for(fake, clock)
    callers = [asyncio.create_task(store.get()) for _ in range(2)]
    await fake.started.wait()
    fake.gate.set()

    outcomes = await asyncio.gather(*callers, return_exceptions=True)

    assert all(isinstance(outcome, BoeUnavailableError) for outcome in outcomes)
    fake.fail_with = None
    retried = await store.get()
    assert len(retried.laws) == 5  # the next caller retried instead of seeing a cached failure


async def test_cancelling_the_only_caller_cancels_the_download_and_the_next_call_retries(
    clock: Clock,
) -> None:
    fake = FakeSearch(laws_numbered(5))
    fake.gate = asyncio.Event()
    store = store_for(fake, clock)
    caller = asyncio.create_task(store.get())
    await fake.started.wait()

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)  # let the abandoned download task process its cancellation

    assert fake.cancelled == 1
    fake.gate = None
    result = await store.get()
    assert len(result.laws) == 5


async def test_cancelling_one_of_two_callers_leaves_the_download_to_the_other(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(5))
    fake.gate = asyncio.Event()
    store = store_for(fake, clock)
    leaving = asyncio.create_task(store.get())
    staying = asyncio.create_task(store.get())
    await fake.started.wait()

    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving
    fake.gate.set()
    result = await staying

    assert len(result.laws) == 5
    assert fake.cancelled == 0
    assert len(fake.calls) == 2


async def test_a_caller_joining_a_download_that_lost_all_its_callers_starts_a_fresh_one(
    clock: Clock,
) -> None:
    fake = FakeSearch(laws_numbered(2))
    fake.gate = asyncio.Event()
    store = store_for(fake, clock)
    first = asyncio.create_task(store.get())
    await fake.started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(store.get())
    await asyncio.sleep(0)
    fake.gate.set()
    result = await second

    assert len(result.laws) == 2


async def test_each_caller_gets_progress_from_its_own_context(clock: Clock) -> None:
    # FastMCP's ``Context.report_progress`` finds its request through a context variable. The
    # download runs in the task of whoever started it, so a callback called from there would
    # report to that first caller's request. Each callback must run in its own caller's context.
    caller: contextvars.ContextVar[str] = contextvars.ContextVar("caller")
    fake = FakeSearch(laws_numbered(5))
    fake.gate = asyncio.Event()
    store = store_for(fake, clock)
    seen: list[tuple[str, str, int]] = []

    def listener(name: str) -> Callable[[int, int | None, str], asyncio.Future[None]]:
        async def report(done: int, total: int | None, message: str) -> None:
            seen.append((name, caller.get(), done))

        return report  # type: ignore[return-value]

    async def call(name: str) -> None:
        caller.set(name)
        await store.get(progress=listener(name))

    one = asyncio.create_task(call("a"))
    await fake.started.wait()
    two = asyncio.create_task(call("b"))
    await asyncio.sleep(0)
    fake.gate.set()
    await asyncio.gather(one, two)

    assert sorted(seen) == [("a", "a", 3), ("a", "a", 5), ("b", "b", 3), ("b", "b", 5)]


# --- get: when the pages were really downloaded -------------------------------------------------


class StampingSearch(FakeSearch):
    """A ``FakeSearch`` that also keeps the download stamp, as ``BoeClient`` does in its cache."""

    def __init__(self, laws: list[LawSummary]) -> None:
        super().__init__(laws)
        self.stamp: datetime | None = None
        self.stamp_ttl: float | None = None

    async def read_catalogue_stamp(self) -> datetime | None:
        return self.stamp

    async def write_catalogue_stamp(self, when: datetime, ttl: float) -> None:
        self.stamp, self.stamp_ttl = when, ttl


async def test_a_download_leaves_a_stamp_that_expires_before_its_pages(clock: Clock) -> None:
    fake = StampingSearch(laws_numbered(2))

    result = await store_for(fake, clock).get()

    assert result.fetched_at == START
    assert fake.stamp == START
    assert fake.stamp_ttl is not None
    assert 0 < fake.stamp_ttl < CATALOGUE_TTL  # pages are written first and expire first


async def test_after_a_restart_the_snapshot_keeps_the_time_of_the_real_download(
    clock: Clock,
) -> None:
    fake = StampingSearch(laws_numbered(2))
    await store_for(fake, clock).get()

    clock.now = START + timedelta(hours=20)  # a new process; the pages come from the disk cache
    restarted = store_for(fake, clock)
    result = await restarted.get()

    assert result.fetched_at == START
    # It is only four hours from expiry, not a fresh day.
    clock.now = START + timedelta(seconds=CATALOGUE_TTL)
    again = await restarted.get()
    assert again is not result


async def test_a_client_without_stamps_reports_the_time_the_download_finished(
    clock: Clock,
) -> None:
    clock.now = START + timedelta(hours=1)

    result = await store_for(FakeSearch(laws_numbered(2)), clock).get()

    assert result.fetched_at == clock.now


# --- get: a failed refresh serves the old snapshot -----------------------------------------------


async def test_a_failed_refresh_serves_the_previous_snapshot_with_its_true_age(
    clock: Clock,
) -> None:
    fake = FakeSearch(laws_numbered(2))
    store = store_for(fake, clock)
    first = await store.get()

    clock.now = START + timedelta(seconds=CATALOGUE_TTL + 5)
    fake.fail_with = BoeUnavailableError("down")
    served = await store.get()

    assert served is first
    assert served.fetched_at == START
    # The next call tries again, and a recovered API gives a fresh snapshot.
    fake.fail_with = None
    refreshed = await store.get()
    assert refreshed is not first
    assert refreshed.fetched_at == clock.now


async def test_a_failed_first_download_still_raises(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(2))
    fake.fail_with = BoeUnavailableError("down")

    with pytest.raises(BoeUnavailableError):
        await store_for(fake, clock).get()


async def test_cancelling_a_refresh_is_not_turned_into_the_old_snapshot(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(2))
    store = store_for(fake, clock)
    await store.get()
    clock.now = START + timedelta(seconds=CATALOGUE_TTL + 5)
    fake.gate = asyncio.Event()
    fake.started.clear()
    caller = asyncio.create_task(store.get())
    await fake.started.wait()

    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller


# --- select ------------------------------------------------------------------------------------


async def test_select_with_neither_argument_returns_the_catalogue(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(4))
    store = store_for(fake, clock)

    selected = await store.select()

    assert selected == list((await store.get()).laws)
    assert len(fake.calls) == 2  # one crawl only: the second get() was a snapshot hit


async def test_select_by_text_queries_the_body_and_pages_with_the_normal_lifetime(
    clock: Clock,
) -> None:
    fake = FakeSearch(laws_numbered(5))
    store = store_for(fake, clock)

    selected = await store.select(text="teletrabajo")

    assert len(selected) == 5
    assert [c["offset"] for c in fake.calls] == [0, 3]
    assert all(c["ttl"] is None for c in fake.calls)  # None means the client's SEARCH_TTL
    document = json.loads(str(fake.calls[0]["query"]))
    assert document["query"]["query_string"]["query"] == "texto:teletrabajo"
    assert document["sort"] == [{"fecha_publicacion": "desc"}]


async def test_select_by_subject_code_and_text_combines_them(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(1))

    await store_for(fake, clock).select(text="datos", subject_code="3685")

    document = json.loads(str(fake.calls[0]["query"]))
    assert document["query"]["query_string"]["query"] == "texto:datos AND materia@codigo:3685"


async def test_select_drops_duplicates_and_does_not_replace_the_snapshot(clock: Clock) -> None:
    fake = FakeSearch([make_law("A"), make_law("B"), make_law("A")])
    store = store_for(fake, clock)

    selected = await store.select(text="x")

    assert [law.identifier for law in selected] == ["A", "B"]
    fake.calls.clear()
    await store.get()
    assert fake.calls[0]["ttl"] == CATALOGUE_TTL  # get() downloaded: select had stored nothing


async def test_select_stops_at_the_page_limit(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(100))

    with pytest.raises(BoeResponseError):
        await store_for(fake, clock).select(subject_code="1")


async def test_select_rejects_a_malformed_subject_code_without_a_request(clock: Clock) -> None:
    fake = FakeSearch(laws_numbered(1))

    with pytest.raises(ValueError):
        await store_for(fake, clock).select(subject_code="1 OR 2")

    assert fake.calls == []


async def test_select_reports_progress(clock: Clock) -> None:
    reports: list[int] = []

    async def progress(done: int, total: int | None, message: str) -> None:
        reports.append(done)

    await store_for(FakeSearch(laws_numbered(5)), clock).select(text="x", progress=progress)

    assert reports == [3, 5]


# --- with the real client and a recorded body --------------------------------------------------


async def test_with_the_real_client_the_recorded_page_is_cached_for_a_day(
    router: respx.MockRouter,
    cache: RecordingCache,
    limiter: RateLimiter,
    http: httpx.AsyncClient,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalogue, "CATALOGUE_PAGE_SIZE", 5000)
    route = router.get(f"{BASE_URL}legislacion-consolidada").mock(
        return_value=json_response(load_fixture("search_bulk_sample_selected.json"))
    )
    client = BoeClient(cache=cache, limiter=limiter, http=http, max_retries=0)

    result = await CatalogueStore(client).get()

    # The fixture has 9 hits, one of them twice (BOE-A-2026-23892).
    assert len(result.laws) == 8
    assert len({law.identifier for law in result.laws}) == 8
    assert route.call_count == 1
    assert route.calls.last.request.url.params["limit"] == "5000"
    assert [ttl for _, ttl in cache.sets].count(CATALOGUE_TTL) == 1
    assert CATALOGUE_TTL != SEARCH_TTL
    assert result.newest_update is not None
    # The stamp is a second, small entry: the pages (a day) and the stamp (a little less).
    stamp_ttls = [ttl for key, ttl in cache.sets if ttl != CATALOGUE_TTL]
    assert len(stamp_ttls) == 1 and stamp_ttls[0] is not None
    assert 0 < stamp_ttls[0] < CATALOGUE_TTL
    assert await client.read_catalogue_stamp() == result.fetched_at
