"""Contract tests for ``BoeClient.get_full_text`` (spec A1): the whole-law ``/texto`` request.

Runs against the recorded ``texto_full_*`` fixtures through respx; nothing sleeps for real.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.client import BASE_URL, FULL_TEXT_TTL, BoeClient
from boe_insights_mcp.errors import BoeNotFoundError, BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import Block
from boe_insights_mcp.ratelimit import RateLimiter
from tests.conftest import RecordingCache, SleepRecorder

LAW_ID = "BOE-A-2026-23272"
TEXT_URL = f"{BASE_URL}legislacion-consolidada/id/{LAW_ID}/texto"
ASTURIAS = "texto_full_asturias_ley8_2026_not_yet_in_force_trimmed.xml"

Loader = Callable[[str], bytes]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def client(
    cache: RecordingCache, limiter: RateLimiter, sleeps: SleepRecorder, http: httpx.AsyncClient
) -> BoeClient:
    """A client wired to test doubles."""
    options: dict[str, Any] = {
        "cache": cache,
        "limiter": limiter,
        "http": http,
        "sleep": sleeps,
        "jitter": lambda: 0.0,
        "backoff_base": 1.0,
    }
    return BoeClient(**options)


async def test_requests_the_whole_text_asking_for_xml_and_returns_the_blocks(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(TEXT_URL).mock(return_value=xml_response(load_fixture(ASTURIAS), 200))

    blocks = await client.get_full_text(LAW_ID)

    request = route.calls.last.request
    assert request.headers["accept"] == "application/xml"
    assert request.url.path.endswith(f"/id/{LAW_ID}/texto")
    assert all(isinstance(block, Block) for block in blocks)
    assert [b.block_id for b in blocks][:4] == ["pr", "ti", "ci", "a1"]
    assert len(blocks) == 26


async def test_second_call_is_served_from_the_cache_with_the_six_hour_lifetime(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(TEXT_URL).mock(return_value=xml_response(load_fixture(ASTURIAS), 200))

    first = await client.get_full_text(LAW_ID)
    second = await client.get_full_text(LAW_ID)

    assert route.call_count == 1
    assert first == second
    assert [ttl for _, ttl in cache.sets] == [FULL_TEXT_TTL] == [6 * 3600.0]
    assert client.stats.cache_hits == 1


async def test_unknown_law_is_not_found(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    router.get(TEXT_URL).mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    with pytest.raises(BoeNotFoundError):
        await client.get_full_text(LAW_ID)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"<response><data><texto><bloque id=", id="truncated"),
        pytest.param(
            b"<response><data><texto><bloque tipo='precepto'/></texto></data></response>",
            id="bloque-without-id",
        ),
    ],
)
async def test_unusable_body_is_a_response_error_and_is_not_cached(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: XmlResponse,
    body: bytes,
) -> None:
    router.get(TEXT_URL).mock(return_value=xml_response(body, 200))
    with pytest.raises(BoeResponseError):
        await client.get_full_text(LAW_ID)
    assert cache.sets == []


async def test_server_errors_are_retried_then_reported_as_unavailable(
    router: respx.MockRouter, client: BoeClient, sleeps: SleepRecorder, xml_response: XmlResponse
) -> None:
    route = router.get(TEXT_URL).mock(return_value=xml_response(b"<response/>", 500))
    with pytest.raises(BoeUnavailableError):
        await client.get_full_text(LAW_ID)
    assert route.call_count == 4
    assert len(sleeps.delays) == 3


async def test_a_body_over_the_size_cap_is_refused(
    router: respx.MockRouter,
    cache: RecordingCache,
    limiter: RateLimiter,
    sleeps: SleepRecorder,
    http: httpx.AsyncClient,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    body = load_fixture(ASTURIAS)
    small = BoeClient(
        cache=cache, limiter=limiter, http=http, sleep=sleeps, max_response_bytes=len(body) - 1
    )
    router.get(TEXT_URL).mock(return_value=xml_response(body, 200))
    with pytest.raises(BoeResponseError, match="too large"):
        await small.get_full_text(LAW_ID)


async def test_invalid_law_id_raises_before_any_request(
    router: respx.MockRouter, client: BoeClient
) -> None:
    with pytest.raises(ValueError):
        await client.get_full_text("../etc/passwd")
    assert router.calls.call_count == 0
