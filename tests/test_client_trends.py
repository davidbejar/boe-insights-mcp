"""Tests of the two client additions that the trend tools rely on.

``search_laws`` takes a cache lifetime (``ttl``) and pages of up to 5000 records, and
``has_cached_analysis`` answers from the cache without sending a request, and a permanently
failing analysis is remembered for a short time instead of being requested again.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import respx

from boe_insights_mcp.client import (
    ANALYSIS_FAILURE_TTL,
    ANALYSIS_TTL,
    BASE_URL,
    SEARCH_TTL,
    BoeClient,
)
from boe_insights_mcp.errors import (
    BoeError,
    BoeNotFoundError,
    BoeResponseError,
    BoeUnavailableError,
)
from boe_insights_mcp.ratelimit import RateLimiter
from tests.conftest import RecordingCache

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
LAW_ID = "BOE-A-2015-10566"
ANALYSIS_URL = f"{SEARCH_URL}/id/{LAW_ID}/analisis"
DAY = 24 * 3600.0


@pytest.fixture
def client(cache: RecordingCache, limiter: RateLimiter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(cache=cache, limiter=limiter, http=http, max_retries=0)


async def test_search_uses_the_default_lifetime_without_ttl(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=json_response(load_fixture("search_page_limit10.json"))
    )

    await client.search_laws(limit=10)

    assert [ttl for _, ttl in cache.sets] == [SEARCH_TTL]


async def test_search_ttl_overrides_the_cache_lifetime_of_that_call(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=json_response(load_fixture("search_page_limit10.json"))
    )

    await client.search_laws(limit=10, ttl=DAY)

    assert [ttl for _, ttl in cache.sets] == [DAY]


async def test_a_page_of_5000_is_requested_as_such(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
) -> None:
    route = router.get(SEARCH_URL).mock(
        return_value=json_response(load_fixture("search_page_limit10.json"))
    )

    await client.search_laws(limit=5000)

    assert route.calls.last.request.url.params["limit"] == "5000"


async def test_has_cached_analysis_is_false_before_and_true_after_the_fetch(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
) -> None:
    route = router.get(ANALYSIS_URL).mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )

    before = await client.has_cached_analysis(LAW_ID)
    await client.get_law_analysis(LAW_ID)
    after = await client.has_cached_analysis(LAW_ID)

    assert (before, after) == (False, True)
    assert route.call_count == 1  # the two lookups sent nothing and counted no cache hit
    assert client.stats.cache_hits == 0


async def test_has_cached_analysis_never_sends_a_request(
    router: respx.MockRouter, client: BoeClient
) -> None:
    assert await client.has_cached_analysis(LAW_ID) is False
    assert router.calls.call_count == 0
    assert client.stats.requests_sent == 0


async def test_has_cached_analysis_rejects_a_malformed_identifier(client: BoeClient) -> None:
    with pytest.raises(ValueError):
        await client.has_cached_analysis("../etc")


async def test_the_analysis_lifetime_is_unchanged(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
) -> None:
    router.get(ANALYSIS_URL).mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )

    await client.get_law_analysis(LAW_ID)

    assert cache.sets[0][1] == ANALYSIS_TTL


# --- a permanently failing analysis is remembered ----------------------------------------------


@pytest.mark.parametrize(
    ("make_response", "error"),
    [
        (lambda xml, load: xml(load("error_404_unknown_id.xml"), 404), BoeNotFoundError),
        (lambda xml, load: httpx.Response(200, json={"unexpected": True}), BoeResponseError),
    ],
    ids=["not_found", "unreadable"],
)
async def test_a_permanent_failure_is_remembered_and_costs_no_second_request(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Callable[[str], bytes],
    xml_response: Callable[..., httpx.Response],
    make_response: Callable[..., httpx.Response],
    error: type[BoeError],
) -> None:
    route = router.get(ANALYSIS_URL).mock(return_value=make_response(xml_response, load_fixture))

    with pytest.raises(error):
        await client.get_law_analysis(LAW_ID)
    assert await client.has_cached_analysis(LAW_ID) is True  # the answer is known, no request
    with pytest.raises(error):
        await client.get_law_analysis(LAW_ID)

    assert route.call_count == 1
    assert client.stats.cache_hits == 1  # the second call was served from the marker
    assert [ttl for _, ttl in cache.sets] == [ANALYSIS_FAILURE_TTL]
    assert ANALYSIS_FAILURE_TTL == 3600 < ANALYSIS_TTL


async def test_a_retryable_failure_is_never_remembered(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Callable[[str], bytes],
    json_response: Callable[..., httpx.Response],
) -> None:
    route = router.get(ANALYSIS_URL).mock(
        side_effect=[
            httpx.Response(503, content=b"<status/>"),
            json_response(load_fixture("analisis_ley40_2015.json")),
        ]
    )

    with pytest.raises(BoeUnavailableError):
        await client.get_law_analysis(LAW_ID)
    assert cache.sets == []
    assert await client.has_cached_analysis(LAW_ID) is False
    await client.get_law_analysis(LAW_ID)  # tried again, and this time it works

    assert route.call_count == 2
