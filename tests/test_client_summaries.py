"""Tests of ``BoeClient.get_daily_summary`` and ``has_cached_summary`` with respx.

The cases that matter most are the cache lifetimes: a past day is final (30 days, and so is a
remembered 404), today is not (one hour, and a 404 for today is never remembered).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import httpx
import pytest
import respx

from boe_insights_mcp.client import (
    BASE_URL,
    SUMMARY_PAST_TTL,
    SUMMARY_RECENT_ABSENCE_TTL,
    SUMMARY_TODAY_TTL,
    BoeClient,
)
from boe_insights_mcp.errors import BoeQueryError, BoeResponseError, BoeUnavailableError
from boe_insights_mcp.ratelimit import RateLimiter
from tests.conftest import RecordingCache

TODAY = date(2026, 11, 21)
DAY = date(2026, 11, 20)
URL = f"{BASE_URL}boe/sumario/20261120"
TODAY_URL = f"{BASE_URL}boe/sumario/20261121"
DAYS = 24 * 3600.0
Loader = Callable[[str], bytes]
Xml = Callable[..., httpx.Response]


@pytest.fixture
def client(cache: RecordingCache, limiter: RateLimiter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(cache=cache, limiter=limiter, http=http, max_retries=0)


@pytest.fixture
def body(load_fixture: Loader) -> bytes:
    return load_fixture("sumario_boe_20261120_trimmed.xml")


def test_the_lifetimes_are_thirty_days_and_one_hour() -> None:
    assert SUMMARY_PAST_TTL == 30 * DAYS
    assert SUMMARY_TODAY_TTL == 3600.0


async def test_a_summary_is_requested_as_xml_and_parsed(
    router: respx.MockRouter, client: BoeClient, xml_response: Xml, body: bytes
) -> None:
    route = router.get(URL).mock(return_value=xml_response(body, 200))

    summary = await client.get_daily_summary(DAY, today=TODAY)

    assert summary is not None
    assert summary.day == DAY
    assert summary.issues[0].identifier == "BOE-S-2026-289"
    assert route.calls.last.request.headers["Accept"] == "application/xml"


async def test_a_past_day_is_cached_for_thirty_days_and_served_from_the_cache(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    body: bytes,
) -> None:
    route = router.get(URL).mock(return_value=xml_response(body, 200))

    first = await client.get_daily_summary(DAY, today=TODAY)
    second = await client.get_daily_summary(DAY, today=TODAY)

    assert first == second
    assert route.call_count == 1
    assert [ttl for _, ttl in cache.sets] == [SUMMARY_PAST_TTL]


async def test_today_is_cached_for_one_hour(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    body: bytes,
) -> None:
    router.get(TODAY_URL).mock(return_value=xml_response(body, 200))

    await client.get_daily_summary(TODAY, today=TODAY)

    assert [ttl for _, ttl in cache.sets] == [SUMMARY_TODAY_TTL]


async def test_a_404_on_a_past_day_is_none_and_remembered_for_thirty_days(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    load_fixture: Loader,
) -> None:
    sunday = date(2026, 11, 15)
    route = router.get(f"{BASE_URL}boe/sumario/20261115").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )

    first = await client.get_daily_summary(sunday, today=TODAY)
    second = await client.get_daily_summary(sunday, today=TODAY)

    assert (first, second) == (None, None)
    assert route.call_count == 1
    # Six days ago is recent: a 404 may be a transient failure, so it is remembered for a day only.
    assert [ttl for _, ttl in cache.sets] == [SUMMARY_RECENT_ABSENCE_TTL]


async def test_a_404_for_a_day_more_than_a_week_ago_is_remembered_for_a_month(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    load_fixture: Loader,
) -> None:
    old_sunday = date(2026, 11, 8)  # 13 days before TODAY
    router.get(f"{BASE_URL}boe/sumario/20261108").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )

    assert await client.get_daily_summary(old_sunday, today=TODAY) is None
    assert [ttl for _, ttl in cache.sets] == [SUMMARY_PAST_TTL]


@pytest.mark.parametrize(("ago", "ttl"), [(7, SUMMARY_RECENT_ABSENCE_TTL), (8, SUMMARY_PAST_TTL)])
async def test_the_week_boundary_of_a_remembered_absence(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    load_fixture: Loader,
    ago: int,
    ttl: float,
) -> None:
    day = TODAY - timedelta(days=ago)
    router.get(f"{BASE_URL}boe/sumario/{day:%Y%m%d}").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )

    await client.get_daily_summary(day, today=TODAY)

    assert [t for _, t in cache.sets] == [ttl]


async def test_a_404_for_today_is_none_and_not_remembered(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    load_fixture: Loader,
    body: bytes,
) -> None:
    route = router.get(TODAY_URL).mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )

    assert await client.get_daily_summary(TODAY, today=TODAY) is None
    assert cache.sets == []
    # The issue appears later in the day: the next call must ask again and find it.
    route.mock(return_value=xml_response(body, 200))
    assert await client.get_daily_summary(TODAY, today=TODAY) is not None
    assert route.call_count == 2


async def test_a_day_that_is_today_for_the_caller_is_judged_by_the_callers_today(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: Xml,
    body: bytes,
) -> None:
    router.get(URL).mock(return_value=xml_response(body, 200))

    await client.get_daily_summary(DAY, today=DAY)

    assert [ttl for _, ttl in cache.sets] == [SUMMARY_TODAY_TTL]


@pytest.mark.parametrize("day", [date(1960, 8, 31), date(2026, 11, 22)])
async def test_a_day_outside_the_range_raises_before_any_request(
    router: respx.MockRouter, client: BoeClient, day: date
) -> None:
    with pytest.raises(ValueError, match="1960-09-01"):
        await client.get_daily_summary(day, today=TODAY)

    assert router.calls.call_count == 0


async def test_the_first_day_is_accepted(
    router: respx.MockRouter, client: BoeClient, xml_response: Xml, load_fixture: Loader
) -> None:
    router.get(f"{BASE_URL}boe/sumario/19600901").mock(
        return_value=xml_response(load_fixture("sumario_boe_19600901_trimmed.xml"), 200)
    )

    summary = await client.get_daily_summary(date(1960, 9, 1), today=TODAY)

    assert summary is not None and summary.issues[0].identifier == "BOE-S-1960-210"


async def test_a_server_error_is_unavailable_and_not_cached(
    router: respx.MockRouter, client: BoeClient, cache: RecordingCache, xml_response: Xml
) -> None:
    router.get(URL).mock(return_value=xml_response(b"<response/>", 503))

    with pytest.raises(BoeUnavailableError):
        await client.get_daily_summary(DAY, today=TODAY)

    assert cache.sets == []


async def test_a_400_is_an_invalid_query(
    router: respx.MockRouter, client: BoeClient, xml_response: Xml
) -> None:
    router.get(URL).mock(return_value=xml_response(b"<response/>", 400))

    with pytest.raises(BoeQueryError):
        await client.get_daily_summary(DAY, today=TODAY)


async def test_an_unreadable_body_is_a_response_error_and_not_cached(
    router: respx.MockRouter, client: BoeClient, cache: RecordingCache, xml_response: Xml
) -> None:
    router.get(URL).mock(return_value=xml_response(b"<response><data/></response>", 200))

    with pytest.raises(BoeResponseError):
        await client.get_daily_summary(DAY, today=TODAY)

    assert cache.sets == []


async def test_has_cached_summary_sees_bodies_and_remembered_absences_without_a_request(
    router: respx.MockRouter,
    client: BoeClient,
    xml_response: Xml,
    load_fixture: Loader,
    body: bytes,
) -> None:
    sunday = date(2026, 11, 15)
    router.get(URL).mock(return_value=xml_response(body, 200))
    router.get(f"{BASE_URL}boe/sumario/20261115").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )
    assert not await client.has_cached_summary(DAY)

    await client.get_daily_summary(DAY, today=TODAY)
    await client.get_daily_summary(sunday, today=TODAY)

    assert await client.has_cached_summary(DAY)
    assert await client.has_cached_summary(sunday)
    assert router.calls.call_count == 2
