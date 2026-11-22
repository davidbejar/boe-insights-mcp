"""Sequences of saves and checks over several days: what a gazette search remembers it delivered.

A gazette search keeps ``checked_through`` (every day up to it is finished) and ``seen_items``
(what was already delivered for later days). These tests drive whole stories (save, the issue
grows, check, check) through ``save_search`` and ``check_saved_searches`` with a fake BOE and an
injected clock, and assert the promise "a check never loses news silently": over any sequence of
calls, every matching item is delivered exactly once.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from boe_insights_mcp import saved_searches
from boe_insights_mcp.errors import BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import DailySummary, GazetteIssue
from boe_insights_mcp.searchstore import SavedSearch, SavedSearchStore
from tests.test_saved_searches import FakeBoe, ids, item, seed_gazette

OWNER = "local"
FRI = date(2026, 11, 20)  # a Friday
THU = FRI - timedelta(days=1)
SAT = FRI + timedelta(days=1)
SUN = FRI + timedelta(days=2)
MON = FRI + timedelta(days=3)


def at(day: date, hour: int = 12) -> datetime:
    """``hour`` o'clock in Madrid (UTC+1 in November) on ``day``, as a UTC instant."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC) - timedelta(hours=1)


@pytest.fixture
async def store() -> AsyncIterator[SavedSearchStore]:
    """A real in-memory store."""
    opened = SavedSearchStore(None)
    yield opened
    await opened.close()


async def save_on(
    store: SavedSearchStore, fake: FakeBoe, day: date, hour: int = 8, **kwargs: Any
) -> Any:
    """``save_search`` at ``hour`` Madrid time on ``day``."""
    return await saved_searches.save_search(
        store, fake, OWNER, now=at(day, hour), today=day, **kwargs
    )


async def check_on(
    store: SavedSearchStore, fake: FakeBoe, day: date, hour: int = 12, **kwargs: Any
) -> Any:
    """``check_saved_searches`` at ``hour`` Madrid time on ``day``."""
    return await saved_searches.check_saved_searches(
        store, fake, OWNER, now=at(day, hour), today=day, clock=fake.clock, **kwargs
    )


async def stored(store: SavedSearchStore, name: str = "vivienda") -> SavedSearch:
    """The stored row of the local owner's search ``name``."""
    return await store.get(OWNER, name)


def issue(day: date, *items: Any, issues: int = 1) -> DailySummary:
    """A summary of ``day`` with ``issues`` issues."""
    return DailySummary(
        day=day,
        issues=tuple(GazetteIssue(number=str(n), identifier=f"BOE-S-{n}") for n in range(issues)),
        items=tuple(items),
    )


# --- save: the baseline ------------------------------------------------------------------------


async def test_saving_remembers_what_todays_issue_already_has(store: SavedSearchStore) -> None:
    fake = FakeBoe(days={FRI: issue(FRI, item("A", "Ley de vivienda"), item("Z", "Otra cosa"))})

    result = await save_on(store, fake, FRI, words="vivienda")

    assert fake.requested_days == [FRI]
    row = await stored(store)
    assert row.checked_through == THU
    # All the items of the issue, not only those that match: a later edit of the filter must
    # not bring them back.
    assert row.seen_items == {FRI: frozenset({"A", "Z"})}
    assert result.note is not None
    assert "The first check reports what is published from now on." in result.note


async def test_saving_before_todays_issue_is_out_remembers_nothing(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe()

    await save_on(store, fake, FRI, hour=0, words="vivienda")

    row = await stored(store)
    assert row.checked_through == THU
    assert row.seen_items == {}


async def test_a_failure_reading_todays_issue_still_saves_and_says_so(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe(days={FRI: BoeUnavailableError("down", status=503)})

    result = await save_on(store, fake, FRI, words="vivienda")

    assert (await stored(store)).seen_items == {}
    assert result.note is not None
    assert (
        "Today's issue could not be read; the first check may include items published earlier "
        "today."
    ) in result.note


async def test_an_issue_published_after_saving_is_reported_by_the_first_check(
    store: SavedSearchStore,
) -> None:
    # R2: saved at 00:30 Madrid, the issue comes out at 07:30.
    await save_on(store, FakeBoe(), FRI, hour=0, words="vivienda")
    fake = FakeBoe(days={FRI: issue(FRI, item("A", "Orden de vivienda"))})

    result = await check_on(store, fake, FRI)

    assert ids(result.results[0]) == ["A"]


# --- the issue of the day grows -----------------------------------------------------------------


async def test_an_item_added_to_todays_issue_after_a_check_is_delivered_by_the_next_one(
    store: SavedSearchStore,
) -> None:
    # R1: read at 08:00, an extraordinary issue appears at 20:00.
    await seed_gazette(store, checked_through=THU)
    morning = issue(FRI, item("A", "Ley de vivienda"))
    fake = FakeBoe(days={FRI: morning})
    first = await check_on(store, fake, FRI, hour=8)
    assert ids(first.results[0]) == ["A"]
    assert (await stored(store)).checked_through == THU  # today is never finished

    fake.days[FRI] = issue(
        FRI, item("B", "Real Decreto-ley de vivienda urgente"), *morning.items, issues=2
    )
    second = await check_on(store, fake, FRI, hour=21)
    assert ids(second.results[0]) == ["B"]

    third = await check_on(store, fake, FRI, hour=22)
    assert ids(third.results[0]) == []

    fake.days[SAT] = issue(SAT)
    fourth = await check_on(store, fake, SAT, hour=10)
    assert ids(fourth.results[0]) == []
    row = await stored(store)
    assert row.checked_through == FRI  # yesterday is settled now and everything was delivered
    assert row.seen_items == {}


async def test_save_then_the_issue_grows_then_checks(store: SavedSearchStore) -> None:
    fake = FakeBoe(days={FRI: issue(FRI, item("A", "Ley de vivienda"))})
    await save_on(store, fake, FRI, words="vivienda")

    nothing = await check_on(store, fake, FRI, hour=10)
    assert ids(nothing.results[0]) == []

    fake.days[FRI] = issue(FRI, item("A", "Ley de vivienda"), item("B", "Orden de vivienda"))
    grown = await check_on(store, fake, FRI, hour=11)
    assert ids(grown.results[0]) == ["B"]

    again = await check_on(store, fake, FRI, hour=12)
    assert ids(again.results[0]) == []
    assert again.complete is True


# --- a limit cuts inside a day ---------------------------------------------------------------


async def test_a_limit_cut_leaves_the_rest_new_and_calls_deliver_everything_once(
    store: SavedSearchStore,
) -> None:
    # R6: 30 matches, limit 5.
    await seed_gazette(store, checked_through=THU)
    everything = [f"A{n:02}" for n in range(30)]
    fake = FakeBoe(days={FRI: issue(FRI, *(item(i, "Vivienda") for i in everything))})
    delivered: list[str] = []

    first = await check_on(store, fake, FRI, limit=5)
    gazette = first.results[0]
    assert gazette.status == "partial"
    assert gazette.has_more is True
    assert gazette.matched == 30
    assert first.complete is False
    assert first.note is not None and "Call again to continue." in first.note
    assert gazette.note is not None
    assert "Showing 5 of 30 new items; call again for the rest." in gazette.note
    delivered += ids(gazette)

    calls = 1
    while True:
        result = await check_on(store, fake, FRI, limit=5)
        calls += 1
        delivered += ids(result.results[0])
        if result.complete:
            break
        assert calls < 20
    assert calls == 6
    assert delivered == everything  # all, once, in document order
    assert result.results[0].has_more is False
    assert result.results[0].status == "checked"


async def test_a_cut_delivers_the_oldest_day_first_and_the_bookmark_waits(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=date(2026, 11, 16))  # Monday
    tue, wed, thu = date(2026, 11, 17), date(2026, 11, 18), THU
    fake = FakeBoe(
        days={
            tue: issue(tue, item("T1"), item("T2")),
            wed: issue(wed, item("W1")),
            thu: issue(thu, item("H1")),
            FRI: issue(FRI),
        }
    )

    first = await check_on(store, fake, FRI, limit=3)

    assert ids(first.results[0]) == ["T1", "T2", "W1"]
    assert [entry.date for entry in first.results[0].items] == [tue, tue, wed]
    row = await stored(store)
    assert row.checked_through == wed  # Tuesday and Wednesday are finished, Thursday is not
    assert row.seen_items == {}

    second = await check_on(store, fake, FRI, limit=3)

    assert ids(second.results[0]) == ["H1"]
    assert second.complete is True
    assert (await stored(store)).checked_through == thu


async def test_a_cut_inside_a_day_remembers_what_was_delivered_for_later_days(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=THU - timedelta(days=1))
    fake = FakeBoe(days={THU: issue(THU, item("H1"), item("H2"), item("H3")), FRI: issue(FRI)})

    await check_on(store, fake, FRI, limit=2)

    row = await stored(store)
    assert row.checked_through == THU - timedelta(days=1)
    assert row.seen_items == {THU: frozenset({"H1", "H2"})}


# --- the total cap over all searches -----------------------------------------------------------


async def test_the_total_cap_serves_searches_in_order_and_calls_deliver_all_once(
    store: SavedSearchStore,
) -> None:
    for number in range(20):
        await seed_gazette(store, f"s{number:02}", checked_through=THU, minutes=number)
    todays = [f"A{n:02}" for n in range(10)]
    fake = FakeBoe(days={FRI: issue(FRI, *(item(i, "Vivienda") for i in todays))})
    delivered: dict[str, list[str]] = {f"s{n:02}": [] for n in range(20)}
    completes: list[bool] = []

    for _ in range(10):
        result = await check_on(store, fake, FRI, limit=50)
        completes.append(result.complete)
        assert sum(len(check.items or []) for check in result.results) <= 60
        for check in result.results:
            delivered[check.name] += ids(check)
        if result.complete:
            break
        waiting = [check for check in result.results if check.status == "partial"]
        assert waiting
        assert all(
            check.note == "Not reached in this call; call again." and check.items == []
            for check in waiting
            if check.name > "s05" and check.matched
        )

    assert completes == [False, False, False, True]
    assert all(got == todays for got in delivered.values())


async def test_the_first_search_a_call_cannot_reach_says_so(store: SavedSearchStore) -> None:
    for number in range(7):
        await seed_gazette(store, f"s{number}", checked_through=THU, minutes=number)
    fake = FakeBoe(days={FRI: issue(FRI, *(item(f"A{n:02}", "Vivienda") for n in range(10)))})

    result = await check_on(store, fake, FRI, limit=50)

    last = result.results[6]
    assert last.status == "partial"
    assert last.items == []
    assert last.matched == 10
    assert last.has_more is True
    assert last.note == "Not reached in this call; call again."
    assert [len(check.items or []) for check in result.results[:6]] == [10] * 6
    # Nothing was marked for the search that was not reached.
    assert (await stored(store, "s6")).seen_items == {}
    assert (await stored(store, "s6")).last_checked_at is None


# --- days that are not settled -----------------------------------------------------------------


async def test_a_recent_weekday_without_an_issue_is_read_again_until_it_appears(
    store: SavedSearchStore,
) -> None:
    # R4: a Wednesday answered 404 (transient or late).
    tue, wed = date(2026, 11, 17), date(2026, 11, 18)
    await seed_gazette(store, checked_through=tue)
    fake = FakeBoe(days={wed: None, THU: issue(THU, item("H1")), FRI: issue(FRI)})

    first = await check_on(store, fake, FRI)

    assert ids(first.results[0]) == ["H1"]
    assert first.results[0].status == "checked"  # unsettled days do not stop "checked"
    row = await stored(store)
    assert row.checked_through == tue
    assert row.seen_items == {THU: frozenset({"H1"})}

    fake.days[wed] = issue(wed, item("W1"))  # the late issue
    second = await check_on(store, fake, FRI)

    assert ids(second.results[0]) == ["W1"]  # H1 is not delivered twice
    row = await stored(store)
    assert row.checked_through == THU
    assert row.seen_items == {}


async def test_a_sunday_without_an_issue_is_settled_at_once(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=THU)
    fake = FakeBoe(days={FRI: issue(FRI), SAT: issue(SAT, item("S1")), SUN: None})

    await check_on(store, fake, MON)

    row = await stored(store)
    # Monday is today (never finished): the run of finished days ends on Sunday.
    assert row.checked_through == SUN


@pytest.mark.parametrize(("back", "settled"), [(7, False), (8, True)])
async def test_an_old_weekday_without_an_issue_is_settled_after_seven_days(
    store: SavedSearchStore, back: int, settled: bool
) -> None:
    gap = FRI - timedelta(days=back)
    assert gap.weekday() < 5
    await seed_gazette(store, checked_through=gap - timedelta(days=1))
    days: dict[date, Any] = {
        gap + timedelta(days=n): issue(gap + timedelta(days=n)) for n in range(1, back + 1)
    }
    days[gap] = None
    fake = FakeBoe(days=days)

    await check_on(store, fake, FRI)

    expected = THU if settled else gap - timedelta(days=1)
    assert (await stored(store)).checked_through == expected


@pytest.mark.parametrize(
    "error",
    [BoeResponseError("unreadable"), BoeUnavailableError("down", status=503)],
    ids=["not retryable", "retryable"],
)
async def test_an_unreadable_recent_day_is_retried_and_reading_goes_on(
    store: SavedSearchStore, error: Exception
) -> None:
    # Any BoeError makes the day unreadable: a 403 or an HTML maintenance page during a short
    # outage is not "failed for good". Later days are read and delivered; the bookmark waits.
    await seed_gazette(store, checked_through=date(2026, 11, 17))
    wed = date(2026, 11, 18)
    fake = FakeBoe(days={wed: error, THU: issue(THU, item("H1")), FRI: issue(FRI)})

    result = await check_on(store, fake, FRI)

    gazette = result.results[0]
    assert gazette.status == "checked"
    assert result.complete is True
    assert gazette.days_failed == 0
    assert gazette.days_retrying == 1
    assert gazette.note == "1 day could not be read and will be tried again at the next check."
    assert ids(gazette) == ["H1"]
    row = await stored(store)
    assert row.checked_through == date(2026, 11, 17)
    assert row.seen_items == {THU: frozenset({"H1"})}


async def test_an_unreadable_day_that_recovers_is_delivered_once_with_nothing_repeated(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=date(2026, 11, 17))
    wed = date(2026, 11, 18)
    fake = FakeBoe(
        days={
            wed: BoeUnavailableError("down", status=503),
            THU: issue(THU, item("H1")),
            FRI: issue(FRI),
        }
    )
    first = await check_on(store, fake, FRI)

    fake.days[wed] = issue(wed, item("W1"))
    second = await check_on(store, fake, FRI)
    third = await check_on(store, fake, FRI)

    assert ids(first.results[0]) == ["H1"]
    assert ids(second.results[0]) == ["W1"]
    assert second.results[0].days_retrying in (None, 0)
    assert second.results[0].note is None
    assert ids(third.results[0]) == []
    assert (await stored(store)).checked_through == THU


@pytest.mark.parametrize(("ago_days", "given_up"), [(7, False), (8, True)])
async def test_a_day_unreadable_for_more_than_a_week_is_given_up_and_reported(
    store: SavedSearchStore, ago_days: int, given_up: bool
) -> None:
    gap = FRI - timedelta(days=ago_days)
    await seed_gazette(store, checked_through=gap - timedelta(days=1))
    days: dict[date, Any] = {gap: BoeUnavailableError("down", status=503)}
    days |= {
        gap + timedelta(days=n): issue(gap + timedelta(days=n)) for n in range(1, ago_days + 1)
    }
    fake = FakeBoe(days=days)

    result = await check_on(store, fake, FRI)

    gazette = result.results[0]
    assert gazette.status == "checked"
    assert result.complete is True
    if given_up:
        assert gazette.days_failed == 1
        assert not gazette.days_retrying
        assert gazette.note == (
            f"1 day could not be read after a week of trying and was skipped: {gap.isoformat()}."
        )
        assert (await stored(store)).checked_through == THU
    else:
        assert not gazette.days_failed
        assert gazette.days_retrying == 1
        assert (await stored(store)).checked_through == gap - timedelta(days=1)


async def test_several_days_given_up_are_listed_in_the_plural(store: SavedSearchStore) -> None:
    first, second = FRI - timedelta(days=10), FRI - timedelta(days=9)
    await seed_gazette(store, checked_through=first - timedelta(days=1))
    days: dict[date, Any] = {
        first: BoeResponseError("unreadable"),
        second: BoeUnavailableError("down", status=503),
    }
    days |= {first + timedelta(days=n): issue(first + timedelta(days=n)) for n in range(2, 11)}
    fake = FakeBoe(days=days)

    result = await check_on(store, fake, FRI)

    assert result.results[0].days_failed == 2
    assert result.results[0].note == (
        "2 days could not be read after a week of trying and were skipped: "
        f"{first.isoformat()}, {second.isoformat()}."
    )


async def test_today_unreadable_is_retried_and_never_given_up(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=THU)
    fake = FakeBoe(days={FRI: BoeUnavailableError("down", status=503)})

    result = await check_on(store, fake, FRI)

    gazette = result.results[0]
    assert gazette.status == "checked"
    assert gazette.days_retrying == 1
    assert not gazette.days_failed
    assert (await stored(store)).checked_through == THU


async def test_a_day_that_keeps_failing_does_not_block_delivery_over_many_checks(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=date(2026, 11, 17))
    wed = date(2026, 11, 18)
    fake = FakeBoe(days={wed: BoeUnavailableError("down", status=503), THU: issue(THU, item("H1"))})
    delivered: list[str] = []

    for day, item_id in [(FRI, "F1"), (SAT, "S1"), (MON, "M1")]:
        fake.days[day] = issue(day, item(item_id))
        result = await check_on(store, fake, day)
        assert result.complete is True
        delivered += ids(result.results[0])

    assert delivered == ["H1", "F1", "S1", "M1"]


async def test_the_time_budget_stops_the_reading_and_nothing_is_lost(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=date(2026, 11, 17))
    wed = date(2026, 11, 18)
    fake = FakeBoe(
        days={
            wed: issue(wed, item("W1")),
            THU: issue(THU, item("H1")),
            FRI: issue(FRI, item("F1")),
        },
        seconds_each=6.0,
    )

    first = await check_on(store, fake, FRI, time_limit=10.0)

    assert fake.requested_days == [wed]
    assert first.results[0].status == "partial"
    delivered = ids(first.results[0])
    for _ in range(5):
        result = await check_on(store, fake, FRI, time_limit=10.0)
        delivered += ids(result.results[0])
        if result.complete:
            break
    assert delivered == ["W1", "H1", "F1"]


# --- the 31-day cap ------------------------------------------------------------------------------


async def test_the_cap_starts_thirty_days_back_and_the_bookmark_follows_the_run(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=FRI - timedelta(days=40))
    days: dict[date, Any] = {
        FRI - timedelta(days=n): issue(FRI - timedelta(days=n)) for n in range(31)
    }
    fake = FakeBoe(days=days)

    result = await check_on(store, fake, FRI)

    assert fake.requested_days[0] == FRI - timedelta(days=30)
    assert len(fake.requested_days) == 31
    note = result.results[0].note
    assert note is not None
    assert "Only the last 31 days were checked; 9 earlier days were skipped." in note
    assert (await stored(store)).checked_through == THU


# --- the filter is edited between checks ----------------------------------------------------------


async def test_editing_the_filter_does_not_bring_back_what_was_delivered(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=THU)
    fake = FakeBoe(
        days={
            FRI: issue(
                FRI,
                item("A", "Ley de vivienda"),
                item("B", "Ley de becas"),
                item("C", "Becas y vivienda"),
            )
        }
    )
    first = await check_on(store, fake, FRI, hour=9)
    assert ids(first.results[0]) == ["A", "C"]

    await saved_searches.edit_saved_search(store, OWNER, "vivienda", words="becas")
    second = await check_on(store, fake, FRI, hour=10)

    # C was delivered already; B matches the new words and was never delivered.
    assert ids(second.results[0]) == ["B"]


async def test_a_saved_baseline_survives_an_edit_of_the_filter(store: SavedSearchStore) -> None:
    fake = FakeBoe(days={FRI: issue(FRI, item("A", "Ley de vivienda"), item("B", "Ley de becas"))})
    await save_on(store, fake, FRI, words="vivienda")

    await saved_searches.edit_saved_search(store, OWNER, "vivienda", words="becas")
    result = await check_on(store, fake, FRI)

    assert ids(result.results[0]) == []


# --- since ----------------------------------------------------------------------------


async def test_since_delivers_oldest_first_stores_nothing_and_points_to_scan_gazette(
    store: SavedSearchStore,
) -> None:
    seed = await seed_gazette(store, checked_through=FRI)
    days: dict[date, Any] = {
        FRI - timedelta(days=n): issue(FRI - timedelta(days=n), item(f"N{n}", "Vivienda"))
        for n in range(6)
    }
    fake = FakeBoe(days=days)

    result = await check_on(store, fake, FRI, since=FRI - timedelta(days=5), limit=3)

    gazette = result.results[0]
    assert ids(gazette) == ["N5", "N4", "N3"]
    assert gazette.has_more is True
    assert gazette.status == "checked"  # calling again cannot progress, so it is not partial
    assert result.complete is True
    assert gazette.note == (
        "Showing 3 of 6; scan_gazette with the same filters lists them all "
        "(at most 31 days per call)."
    )
    assert await stored(store) == seed


async def test_since_does_not_skip_what_was_delivered_before(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=THU)
    fake = FakeBoe(days={FRI: issue(FRI, item("A", "Vivienda"))})
    await check_on(store, fake, FRI)

    result = await check_on(store, fake, FRI, since=THU)

    assert ids(result.results[0]) == ["A"]


async def test_since_exactly_ninety_days_back_is_valid_and_ninety_one_is_not(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=FRI)
    fake = FakeBoe()

    ok = await check_on(store, fake, FRI, since=FRI - timedelta(days=90))
    assert ok.results[0].status in {"checked", "partial"}
    with pytest.raises(ValueError, match="since may be at most 90 days back"):
        await check_on(store, fake, FRI, since=FRI - timedelta(days=91))


async def test_the_total_cap_also_bounds_a_since_call_without_a_false_call_again(
    store: SavedSearchStore,
) -> None:
    for number in range(7):
        await seed_gazette(store, f"s{number}", checked_through=FRI, minutes=number)
    fake = FakeBoe(days={FRI: issue(FRI, *(item(f"A{n:02}", "Vivienda") for n in range(10)))})

    result = await check_on(store, fake, FRI, since=FRI, limit=50)

    assert sum(len(check.items or []) for check in result.results) == 60
    last = result.results[6]
    assert last.items == []
    assert last.has_more is True
    assert last.status == "checked"
    assert result.complete is True
    assert last.note is not None and "scan_gazette" in last.note


# --- every item exactly once ---------------------------------------------------------


async def test_every_item_is_delivered_exactly_once_whatever_the_calls(
    store: SavedSearchStore,
) -> None:
    """Items appear over four days, checks happen at odd moments and with small limits."""
    await save_on(store, FakeBoe(), THU, hour=1, words="vivienda")
    fake = FakeBoe()
    delivered: Counter[str] = Counter()
    published: set[str] = set()
    plan = [
        (THU, 9, {"a1", "a2", "a3"}),
        (THU, 20, {"a4"}),
        (FRI, 8, {"b1", "b2"}),
        (FRI, 9, set()),
        (FRI, 22, {"b3", "b4", "b5"}),
        (SAT, 10, {"c1"}),
        (SUN, 10, set()),
        (MON, 9, {"d1", "d2"}),
    ]
    todays: dict[date, list[str]] = {}
    for day, hour, new in plan:
        todays.setdefault(day, [])
        todays[day] += sorted(new)
        published |= new
        fake.days[day] = issue(day, *(item(i, "Vivienda") for i in todays[day]))
        if day == SUN:
            fake.days[day] = None
        for _ in range(8):
            result = await check_on(store, fake, day, hour=hour, limit=2)
            for check in result.results:
                delivered.update(ids(check))
            if result.complete:
                break

    assert set(delivered) == published
    assert all(count == 1 for count in delivered.values()), delivered
