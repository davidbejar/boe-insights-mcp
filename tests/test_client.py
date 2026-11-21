"""Contract tests for ``boe_insights_mcp.errors`` and ``boe_insights_mcp.client``."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.cache import make_key
from boe_insights_mcp.client import BASE_URL, DEFAULT_USER_AGENT, BoeClient, ClientStats
from boe_insights_mcp.errors import (
    BoeError,
    BoeNotFoundError,
    BoeQueryError,
    BoeResponseError,
    BoeUnavailableError,
)
from boe_insights_mcp.models import LawAnalysis, LawMetadata, LawSummary
from boe_insights_mcp.ratelimit import RateLimiter
from tests.conftest import RecordingCache, SleepRecorder

LAW_ID = "BOE-A-2015-10566"
SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
METADATA_URL = f"{SEARCH_URL}/id/{LAW_ID}/metadatos"
ANALYSIS_URL = f"{SEARCH_URL}/id/{LAW_ID}/analisis"
AMBITOS_URL = f"{BASE_URL}datos-auxiliares/ambitos"

DAY = 24 * 3600.0
SEARCH_TTL = 30 * 60.0
LAW_TTL = 6 * 3600.0
AUXILIARY_TTL = 7 * DAY

GENERIC_500 = (
    b'<?xml version="1.0" encoding="utf-8"?><response><status><code>500</code>'
    b"<text>Internal Server Error</text></status><data/></response>"
)

Call = Callable[[BoeClient], Awaitable[object]]
MakeClient = Callable[..., BoeClient]
Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]

ENDPOINTS = [
    pytest.param(
        lambda c: c.search_laws(),
        SEARCH_URL,
        "search_page_limit10.json",
        SEARCH_TTL,
        id="search",
    ),
    pytest.param(
        lambda c: c.get_law_metadata(LAW_ID),
        METADATA_URL,
        "metadatos_in_force_ley40_2015.json",
        LAW_TTL,
        id="metadata",
    ),
    pytest.param(
        lambda c: c.get_law_analysis(LAW_ID),
        ANALYSIS_URL,
        "analisis_ley40_2015.json",
        LAW_TTL,
        id="analysis",
    ),
    pytest.param(
        lambda c: c.get_auxiliary_table("ambitos"),
        AMBITOS_URL,
        "aux_ambitos.json",
        AUXILIARY_TTL,
        id="auxiliary",
    ),
]

AUXILIARY_FIXTURES = {
    "materias": "aux_materias_trimmed.json",
    "ambitos": "aux_ambitos.json",
    "estados-consolidacion": "aux_estados_consolidacion.json",
    "departamentos": "aux_departamentos_trimmed.json",
    "rangos": "aux_rangos.json",
    "relaciones-anteriores": "aux_relaciones_anteriores.json",
    "relaciones-posteriores": "aux_relaciones_posteriores.json",
}


@pytest.fixture
def make_client(
    cache: RecordingCache, limiter: RateLimiter, sleeps: SleepRecorder, http: httpx.AsyncClient
) -> MakeClient:
    """Build a client wired to test doubles; keyword overrides replace any default."""

    def _make(**overrides: Any) -> BoeClient:
        options: dict[str, Any] = {
            "cache": cache,
            "limiter": limiter,
            "http": http,
            "sleep": sleeps,
            "jitter": lambda: 0.0,
            "backoff_base": 1.0,
        }
        options.update(overrides)
        return BoeClient(**options)

    return _make


@pytest.fixture
def client(make_client: MakeClient) -> BoeClient:
    return make_client()


def mock_ok(
    router: respx.MockRouter, url: str, body: bytes, json_response: JsonResponse
) -> respx.Route:
    return router.get(url).mock(return_value=json_response(body))


def server_error(status: int = 500, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, content=GENERIC_500, headers={"Content-Type": "application/xml", **(headers or {})}
    )


def metadata_body(law_id: str, load_fixture: Loader) -> bytes:
    """The Ley 40/2015 metadata fixture re-labelled with another identifier."""
    document = json.loads(load_fixture("metadatos_in_force_ley40_2015.json"))
    document["data"][0]["identificador"] = law_id
    return json.dumps(document).encode()


# --- error classes -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_class", "code", "retryable"),
    [
        (BoeNotFoundError, "not_found", False),
        (BoeQueryError, "invalid_query", False),
        (BoeUnavailableError, "unavailable", True),
        (BoeResponseError, "bad_response", False),
    ],
)
def test_error_classes_have_stable_code_and_retryable(
    error_class: type[BoeError], code: str, retryable: bool
) -> None:
    assert error_class.code == code
    assert error_class.retryable is retryable


@pytest.mark.parametrize(
    "error_class", [BoeNotFoundError, BoeQueryError, BoeUnavailableError, BoeResponseError]
)
def test_every_error_is_a_boe_error_and_an_exception(error_class: type[BoeError]) -> None:
    assert issubclass(error_class, BoeError)
    assert issubclass(BoeError, Exception)


# --- request shape -----------------------------------------------------------------------------


@pytest.mark.parametrize(("call", "url", "fixture", "ttl"), ENDPOINTS)
async def test_every_request_sends_accept_and_default_user_agent(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
    call: Call,
    url: str,
    fixture: str,
    ttl: float,
) -> None:
    route = mock_ok(router, url, load_fixture(fixture), json_response)
    await call(client)
    request = route.calls.last.request
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == DEFAULT_USER_AGENT


def test_default_user_agent_identifies_the_project() -> None:
    assert DEFAULT_USER_AGENT.startswith("boe-insights-mcp/")
    assert "(+https://github.com/davidbejar/boe-insights-mcp)" in DEFAULT_USER_AGENT


async def test_custom_user_agent_is_sent(
    router: respx.MockRouter,
    make_client: MakeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    route = mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    await make_client(user_agent="my-agent/1.0").get_auxiliary_table("ambitos")
    assert route.calls.last.request.headers["user-agent"] == "my-agent/1.0"


async def test_search_always_sends_explicit_limit_and_offset(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    route = mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    await client.search_laws()
    params = route.calls.last.request.url.params
    assert params["limit"] == "50"
    assert params["offset"] == "0"
    assert "query" not in params


async def test_search_sends_the_given_limit_and_offset(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    route = mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    await client.search_laws(offset=120, limit=10)
    params = route.calls.last.request.url.params
    assert (params["offset"], params["limit"]) == ("120", "10")


async def test_search_passes_the_query_string_through_unchanged(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    query = '{"query":{"query_string":{"query":"titulo:energía AND rango@codigo:1300"}}}'
    route = mock_ok(router, SEARCH_URL, load_fixture("search_titulo_phrase.json"), json_response)
    await client.search_laws(query)
    assert route.calls.last.request.url.params["query"] == query


async def test_requests_go_to_the_configured_base_url(
    router: respx.MockRouter,
    make_client: MakeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    route = mock_ok(
        router,
        "https://example.test/api/datos-auxiliares/ambitos",
        load_fixture("aux_ambitos.json"),
        json_response,
    )
    await make_client(base_url="https://example.test/api/").get_auxiliary_table("ambitos")
    assert route.call_count == 1


# --- argument validation -----------------------------------------------------------------------


def assert_nothing_sent(router: respx.MockRouter, client: BoeClient) -> None:
    assert router.calls.call_count == 0
    assert client.stats == ClientStats(requests_sent=0, cache_hits=0)


@pytest.mark.parametrize(
    "law_id",
    [
        "",
        "boe-A-2015-10566",
        "BOE-1-2015-10566",
        "BOE-A-2015-",
        "BOE-A-2015-1234567",
        "BOE-A-15-10566",
        "BOE-AA-2015-1",
        "B-A-2015-1",
        "BOEBOEB-A-2015-1",
        " BOE-A-2015-10566",
        "BOE-A-2015-10566\n",
        "BOE-A-2015-10566/analisis",
        "BOE-A-2015-10566?limit=1",
        "BOE-A-2015-10566#x",
        "../BOE-A-2015-10566",
        "BOE-A-2015-10566/../../datos-auxiliares/materias",
        "BOE-A-2015-10566%2F..%2F",
        "BOE-A-2015-10566\\..",
    ],
)
@pytest.mark.parametrize("method", ["get_law_metadata", "get_law_analysis"])
async def test_bad_law_id_raises_value_error_without_a_request(
    router: respx.MockRouter, client: BoeClient, method: str, law_id: str
) -> None:
    with pytest.raises(ValueError):
        await getattr(client, method)(law_id)
    assert_nothing_sent(router, client)


@pytest.mark.parametrize("law_id", ["BOE-A-2015-10566", "DOGC-f-2010-90008", "BOE-B-1958-1"])
async def test_valid_law_ids_are_requested(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
    law_id: str,
) -> None:
    route = mock_ok(
        router,
        f"{SEARCH_URL}/id/{law_id}/metadatos",
        metadata_body(law_id, load_fixture),
        json_response,
    )
    result = await client.get_law_metadata(law_id)
    assert route.call_count == 1
    assert result.identifier == law_id


@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_limit_out_of_range_raises_value_error(
    router: respx.MockRouter, client: BoeClient, limit: int
) -> None:
    with pytest.raises(ValueError):
        await client.search_laws(limit=limit)
    assert_nothing_sent(router, client)


async def test_negative_offset_raises_value_error(
    router: respx.MockRouter, client: BoeClient
) -> None:
    with pytest.raises(ValueError):
        await client.search_laws(offset=-1)
    assert_nothing_sent(router, client)


@pytest.mark.parametrize("limit", [1, 1000])
async def test_limit_bounds_are_inclusive(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
    limit: int,
) -> None:
    route = mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    await client.search_laws(limit=limit)
    assert route.calls.last.request.url.params["limit"] == str(limit)


@pytest.mark.parametrize("name", ["unknown", "", "Materias", "materias/../x", "materias?a=b"])
async def test_unknown_auxiliary_table_raises_value_error(
    router: respx.MockRouter, client: BoeClient, name: str
) -> None:
    with pytest.raises(ValueError):
        await client.get_auxiliary_table(name)  # type: ignore[arg-type]
    assert_nothing_sent(router, client)


# --- results -----------------------------------------------------------------------------------


async def test_search_returns_law_summaries_in_response_order(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    results = await client.search_laws(limit=10)
    assert len(results) == 10
    assert all(isinstance(item, LawSummary) for item in results)
    assert results[0].identifier == "BOE-A-2022-10675"
    assert results[3].identifier == "BOE-A-2026-24316"


async def test_search_with_no_matches_returns_empty_list(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(router, SEARCH_URL, load_fixture("search_empty.json"), json_response)
    assert await client.search_laws() == []


async def test_search_with_a_single_hit_returns_one_summary(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(
        router, SEARCH_URL, load_fixture("search_numero_oficial_rango_single.json"), json_response
    )
    results = await client.search_laws()
    assert [r.identifier for r in results] == ["BOE-A-2018-16673"]


async def test_search_with_a_bare_object_instead_of_a_list_returns_one_summary(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    document = json.loads(load_fixture("search_numero_oficial_rango_single.json"))
    document["data"] = document["data"][0]
    mock_ok(router, SEARCH_URL, json.dumps(document).encode(), json_response)
    assert [r.identifier for r in await client.search_laws()] == ["BOE-A-2018-16673"]


async def test_metadata_of_a_law_in_force(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(router, METADATA_URL, load_fixture("metadatos_in_force_ley40_2015.json"), json_response)
    metadata = await client.get_law_metadata(LAW_ID)
    assert isinstance(metadata, LawMetadata)
    assert metadata.identifier == LAW_ID
    assert metadata.repealed is False


async def test_metadata_of_a_repealed_law(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    url = f"{SEARCH_URL}/id/BOE-A-1992-26318/metadatos"
    mock_ok(router, url, load_fixture("metadatos_repealed_ley30_1992.json"), json_response)
    metadata = await client.get_law_metadata("BOE-A-1992-26318")
    assert metadata.repealed is True
    assert str(metadata.repealed_on) == "2021-04-02"


async def test_analysis_of_ley_40_2015(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(router, ANALYSIS_URL, load_fixture("analisis_ley40_2015.json"), json_response)
    analysis = await client.get_law_analysis(LAW_ID)
    assert isinstance(analysis, LawAnalysis)
    assert (len(analysis.earlier), len(analysis.later)) == (19, 24)


@pytest.mark.parametrize(("name", "fixture"), AUXILIARY_FIXTURES.items())
async def test_auxiliary_table_returns_code_to_stripped_text_mapping(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
    name: str,
    fixture: str,
) -> None:
    body = load_fixture(fixture)
    mock_ok(router, f"{BASE_URL}datos-auxiliares/{name}", body, json_response)
    expected = {code: text.strip() for code, text in json.loads(body)["data"].items()}
    result = await client.get_auxiliary_table(name)  # type: ignore[arg-type]
    assert result == expected
    assert result  # non-empty


async def test_auxiliary_table_known_values(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    assert await client.get_auxiliary_table("ambitos") == {"1": "Estatal", "2": "Autonómico"}


async def test_auxiliary_table_texts_lose_trailing_spaces(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    url = f"{BASE_URL}datos-auxiliares/relaciones-posteriores"
    mock_ok(router, url, load_fixture("aux_relaciones_posteriores.json"), json_response)
    table = await client.get_auxiliary_table("relaciones-posteriores")
    assert table["217"] == "SE DEROGA tácitamente"


async def test_client_without_injected_http_creates_its_own(
    router: respx.MockRouter,
    cache: RecordingCache,
    limiter: RateLimiter,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    route = mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    owned = BoeClient(cache=cache, limiter=limiter, sleep=sleeps)
    try:
        assert await owned.get_auxiliary_table("ambitos") == {"1": "Estatal", "2": "Autonómico"}
    finally:
        await owned.aclose()
    assert route.calls.last.request.headers["user-agent"] == DEFAULT_USER_AGENT


# --- caching -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("call", "url", "fixture", "ttl"), ENDPOINTS)
async def test_second_identical_call_is_served_from_cache(
    router: respx.MockRouter,
    client: BoeClient,
    limiter: RateLimiter,
    load_fixture: Loader,
    json_response: JsonResponse,
    call: Call,
    url: str,
    fixture: str,
    ttl: float,
) -> None:
    route = mock_ok(router, url, load_fixture(fixture), json_response)
    first = await call(client)
    second = await call(client)
    assert second == first
    assert route.call_count == 1
    assert client.stats == ClientStats(requests_sent=1, cache_hits=1)
    assert limiter.stats.acquired == 1


@pytest.mark.parametrize(("call", "url", "fixture", "ttl"), ENDPOINTS)
async def test_successful_body_is_cached_with_the_endpoint_lifetime(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
    call: Call,
    url: str,
    fixture: str,
    ttl: float,
) -> None:
    mock_ok(router, url, load_fixture(fixture), json_response)
    await call(client)
    assert len(cache.sets) == 1
    assert cache.sets[0][1] == ttl


async def test_cached_bodies_are_stored_under_the_documented_keys(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    search_body = load_fixture("search_page_limit10.json")
    metadata = load_fixture("metadatos_in_force_ley40_2015.json")
    aux = load_fixture("aux_ambitos.json")
    mock_ok(router, SEARCH_URL, search_body, json_response)
    mock_ok(router, METADATA_URL, metadata, json_response)
    mock_ok(router, AMBITOS_URL, aux, json_response)
    query = '{"sort":[{"fecha_publicacion":"desc"}]}'

    await client.search_laws(query, offset=5, limit=10)
    await client.get_law_metadata(LAW_ID)
    await client.get_auxiliary_table("ambitos")

    search_key = make_key(
        SEARCH_URL, {"limit": 10, "offset": 5, "query": query}, "application/json"
    )
    assert await cache.get(search_key) == search_body
    assert await cache.get(make_key(METADATA_URL, None, "application/json")) == metadata
    assert await cache.get(make_key(AMBITOS_URL, None, "application/json")) == aux


async def test_prefilled_cache_answers_without_any_request(
    router: respx.MockRouter, client: BoeClient, cache: RecordingCache, load_fixture: Loader
) -> None:
    key = make_key(AMBITOS_URL, None, "application/json")
    await cache.set(key, load_fixture("aux_ambitos.json"))
    assert await client.get_auxiliary_table("ambitos") == {"1": "Estatal", "2": "Autonómico"}
    assert router.calls.call_count == 0
    assert client.stats == ClientStats(requests_sent=0, cache_hits=1)


@pytest.mark.parametrize(
    "variation",
    [
        {"offset": 1},
        {"limit": 49},
        {"query": '{"sort":[{"fecha_publicacion":"asc"}]}'},
    ],
    ids=["offset", "limit", "query"],
)
async def test_searches_with_different_params_are_cached_separately(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
    variation: dict[str, Any],
) -> None:
    route = mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    await client.search_laws()
    await client.search_laws(**variation)
    assert route.call_count == 2
    assert client.stats.cache_hits == 0


async def test_same_search_params_share_one_cache_entry(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    route = mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    await client.search_laws('{"sort":[]}', offset=20, limit=5)
    await client.search_laws('{"sort":[]}', offset=20, limit=5)
    assert route.call_count == 1


async def test_different_law_ids_are_cached_separately(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    other = "BOE-A-2015-10565"
    mock_ok(router, METADATA_URL, metadata_body(LAW_ID, load_fixture), json_response)
    mock_ok(
        router,
        f"{SEARCH_URL}/id/{other}/metadatos",
        metadata_body(other, load_fixture),
        json_response,
    )
    first = await client.get_law_metadata(LAW_ID)
    second = await client.get_law_metadata(other)
    assert (first.identifier, second.identifier) == (LAW_ID, other)
    assert client.stats.requests_sent == 2


async def test_metadata_and_analysis_of_one_law_are_cached_separately(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    mock_ok(router, METADATA_URL, load_fixture("metadatos_in_force_ley40_2015.json"), json_response)
    mock_ok(router, ANALYSIS_URL, load_fixture("analisis_ley40_2015.json"), json_response)
    await client.get_law_metadata(LAW_ID)
    analysis = await client.get_law_analysis(LAW_ID)
    assert len(analysis.earlier) == 19


async def test_empty_search_result_is_cached_too(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    route = mock_ok(router, SEARCH_URL, load_fixture("search_empty.json"), json_response)
    assert await client.search_laws() == []
    assert await client.search_laws() == []
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "fixture"),
    [
        (404, "error_404_unknown_id.xml"),
        (400, "error_400_unsupported_query_format.xml"),
        (500, "error_500_syntax_code101.xml"),
    ],
)
async def test_error_responses_are_never_cached(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    status: int,
    fixture: str,
) -> None:
    route = router.get(SEARCH_URL).mock(
        side_effect=[
            xml_response(load_fixture(fixture), status),
            json_response(load_fixture("search_page_limit10.json")),
        ]
    )
    with pytest.raises(BoeError):
        await client.search_laws()
    assert cache.sets == []
    assert len(await client.search_laws()) == 10
    assert route.call_count == 2


async def test_exhausted_retries_leave_the_cache_empty(
    router: respx.MockRouter, client: BoeClient, cache: RecordingCache
) -> None:
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await client.search_laws()
    assert cache.sets == []


# --- error mapping -----------------------------------------------------------------------------


@pytest.mark.parametrize(("call", "url", "fixture", "ttl"), ENDPOINTS)
async def test_404_raises_not_found_on_every_endpoint(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    xml_response: XmlResponse,
    call: Call,
    url: str,
    fixture: str,
    ttl: float,
) -> None:
    route = router.get(url).mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    with pytest.raises(BoeNotFoundError) as caught:
        await call(client)
    assert caught.value.status == 404
    assert caught.value.code == "not_found"
    assert caught.value.retryable is False
    assert route.call_count == 1
    assert sleeps.delays == []


async def test_404_carries_the_api_message(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    router.get(METADATA_URL).mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    with pytest.raises(BoeNotFoundError) as caught:
        await client.get_law_metadata(LAW_ID)
    assert caught.value.api_message == "La información solicitada no existe"
    assert str(caught.value)


@pytest.mark.parametrize(
    "fixture",
    [
        "error_400_unsupported_query_format.xml",
        "error_400_sort_field_not_found.xml",
        "error_400_missing_accept.xml",
    ],
)
async def test_400_raises_query_error_without_retry(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    xml_response: XmlResponse,
    fixture: str,
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=xml_response(load_fixture(fixture), 400))
    with pytest.raises(BoeQueryError) as caught:
        await client.search_laws("whatever")
    assert caught.value.status == 400
    assert caught.value.retryable is False
    assert caught.value.api_message
    assert route.call_count == 1
    assert sleeps.delays == []


async def test_400_api_message_is_the_xml_text(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=xml_response(load_fixture("error_400_unsupported_query_format.xml"), 400)
    )
    with pytest.raises(BoeQueryError) as caught:
        await client.search_laws("x")
    assert caught.value.api_message == "El parámetro query tiene un formato no soportado."


async def test_400_without_an_xml_envelope_has_no_api_message(
    router: respx.MockRouter, client: BoeClient
) -> None:
    router.get(SEARCH_URL).mock(return_value=httpx.Response(400, content=b"Bad Request"))
    with pytest.raises(BoeQueryError) as caught:
        await client.search_laws()
    assert caught.value.api_message is None
    assert caught.value.status == 400


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ("error_500_syntax_code101.xml", "Server error - Code: 101"),
        ("error_500_unknown_field_code101.xml", "Server error - Code: 101"),
        ("error_500_nonjson_query_code109.xml", "Server error - Code: 109"),
    ],
)
async def test_500_with_query_error_code_is_not_retried(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    xml_response: XmlResponse,
    fixture: str,
    message: str,
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=xml_response(load_fixture(fixture), 500))
    with pytest.raises(BoeQueryError) as caught:
        await client.search_laws("titulo:energía AND (")
    assert caught.value.status == 500
    assert caught.value.api_message == message
    assert caught.value.retryable is False
    assert route.call_count == 1
    assert client.stats.requests_sent == 1
    assert sleeps.delays == []


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(server_error(500), id="500-xml"),
        pytest.param(server_error(502), id="502"),
        pytest.param(server_error(503), id="503"),
        pytest.param(server_error(504), id="504"),
        pytest.param(httpx.Response(500, content=b"<html>oops</html>"), id="500-html"),
        pytest.param(httpx.Response(500, content=b""), id="500-empty"),
        pytest.param(
            httpx.Response(
                500,
                content=b"<response><status><code>500</code>"
                b"<text>Server error - Code: 102</text></status></response>",
            ),
            id="500-other-code",
        ),
    ],
)
async def test_other_server_errors_are_retried_then_unavailable(
    router: respx.MockRouter,
    make_client: MakeClient,
    sleeps: SleepRecorder,
    response: httpx.Response,
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=response)
    client = make_client(max_retries=2)
    with pytest.raises(BoeUnavailableError) as caught:
        await client.search_laws()
    assert caught.value.code == "unavailable"
    assert caught.value.retryable is True
    assert caught.value.status == response.status_code
    assert route.call_count == 3
    assert len(sleeps.delays) == 2


async def test_unavailable_error_after_500_carries_the_api_message(
    router: respx.MockRouter, make_client: MakeClient
) -> None:
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError) as caught:
        await make_client(max_retries=1).search_laws()
    assert caught.value.api_message == "Internal Server Error"


async def test_default_policy_makes_one_attempt_plus_three_retries(
    router: respx.MockRouter, client: BoeClient, sleeps: SleepRecorder
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await client.search_laws()
    assert route.call_count == 4
    assert client.stats.requests_sent == 4
    assert len(sleeps.delays) == 3


async def test_429_is_retried_then_unavailable(
    router: respx.MockRouter, make_client: MakeClient
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=server_error(429))
    with pytest.raises(BoeUnavailableError) as caught:
        await make_client(max_retries=2).search_laws()
    assert caught.value.status == 429
    assert route.call_count == 3


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
    ],
    ids=["read-timeout", "connect-timeout", "connect-error", "read-error"],
)
async def test_timeouts_and_transport_errors_are_retried_then_unavailable(
    router: respx.MockRouter, make_client: MakeClient, failure: httpx.HTTPError
) -> None:
    route = router.get(SEARCH_URL).mock(side_effect=failure)
    with pytest.raises(BoeUnavailableError) as caught:
        await make_client(max_retries=2).search_laws()
    assert caught.value.status is None
    assert caught.value.api_message is None
    assert caught.value.retryable is True
    assert route.call_count == 3


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(server_error(500), id="500"),
        pytest.param(server_error(503), id="503"),
        pytest.param(server_error(429), id="429"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
        pytest.param(httpx.ConnectError("refused"), id="connect-error"),
    ],
)
async def test_a_retry_that_succeeds_returns_the_result(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
    failure: httpx.Response | Exception,
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[failure, json_response(load_fixture("search_page_limit10.json"))]
    )
    results = await client.search_laws()
    assert len(results) == 10
    assert client.stats.requests_sent == 2
    assert len(sleeps.delays) == 1


async def test_recovered_response_is_cached(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    route = router.get(SEARCH_URL).mock(
        side_effect=[server_error(), json_response(load_fixture("search_page_limit10.json"))]
    )
    await client.search_laws()
    await client.search_laws()
    assert route.call_count == 2
    assert len(cache.sets) == 1
    assert client.stats == ClientStats(requests_sent=2, cache_hits=1)


async def test_a_non_retryable_error_after_a_retry_stops_immediately(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(METADATA_URL).mock(
        side_effect=[server_error(), xml_response(load_fixture("error_404_unknown_id.xml"), 404)]
    )
    with pytest.raises(BoeNotFoundError):
        await client.get_law_metadata(LAW_ID)
    assert route.call_count == 2
    assert len(sleeps.delays) == 1


async def test_zero_retries_means_a_single_attempt(
    router: respx.MockRouter, make_client: MakeClient, sleeps: SleepRecorder
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await make_client(max_retries=0).search_laws()
    assert route.call_count == 1
    assert sleeps.delays == []


# --- backoff -----------------------------------------------------------------------------------


async def test_backoff_doubles_each_retry(
    router: respx.MockRouter, client: BoeClient, sleeps: SleepRecorder
) -> None:
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await client.search_laws()
    assert sleeps.delays == pytest.approx([1.0, 2.0, 4.0])


async def test_backoff_scales_with_the_base(
    router: respx.MockRouter, make_client: MakeClient, sleeps: SleepRecorder
) -> None:
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await make_client(backoff_base=0.5, max_retries=3).search_laws()
    assert sleeps.delays == pytest.approx([0.5, 1.0, 2.0])


async def test_jitter_adds_a_quarter_of_the_base_times_the_random_draw(
    router: respx.MockRouter, make_client: MakeClient, sleeps: SleepRecorder
) -> None:
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await make_client(jitter=lambda: 0.5, backoff_base=2.0).search_laws()
    assert sleeps.delays == pytest.approx([2.25, 4.25, 8.25])


async def test_jitter_is_drawn_for_each_retry(
    router: respx.MockRouter, make_client: MakeClient, sleeps: SleepRecorder
) -> None:
    draws = iter([0.0, 0.4, 0.8])
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await make_client(jitter=lambda: next(draws)).search_laws()
    assert sleeps.delays == pytest.approx([1.0, 2.1, 4.2])


async def test_retry_after_seconds_replace_the_backoff(
    router: respx.MockRouter,
    make_client: MakeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[
            server_error(429, {"Retry-After": "7"}),
            json_response(load_fixture("search_page_limit10.json")),
        ]
    )
    await make_client(jitter=lambda: 0.9).search_laws()
    assert sleeps.delays == pytest.approx([7.0])


async def test_retry_after_is_honoured_on_server_errors_too(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[
            server_error(503, {"Retry-After": "3"}),
            json_response(load_fixture("search_page_limit10.json")),
        ]
    )
    await client.search_laws()
    assert sleeps.delays == pytest.approx([3.0])


async def test_retry_after_applies_to_its_own_retry_only(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[
            server_error(429, {"Retry-After": "5"}),
            server_error(500),
            json_response(load_fixture("search_page_limit10.json")),
        ]
    )
    await client.search_laws()
    assert sleeps.delays == pytest.approx([5.0, 2.0])


@pytest.mark.parametrize("value", ["60", "61", "3600", "99999"])
async def test_retry_after_is_capped_at_sixty_seconds(
    router: respx.MockRouter,
    make_client: MakeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
    value: str,
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[
            server_error(429, {"Retry-After": value}),
            json_response(load_fixture("search_page_limit10.json")),
        ]
    )
    # A budget larger than the default, which would refuse to wait that long (see below).
    await make_client(total_timeout=120.0).search_laws()
    assert sleeps.delays == pytest.approx([60.0])


async def test_unusable_retry_after_falls_back_to_backoff(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[
            server_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            json_response(load_fixture("search_page_limit10.json")),
        ]
    )
    await client.search_laws()
    assert sleeps.delays == pytest.approx([1.0])


async def test_every_attempt_enters_the_limiter_and_is_counted(
    router: respx.MockRouter, client: BoeClient, limiter: RateLimiter
) -> None:
    router.get(SEARCH_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await client.search_laws()
    assert limiter.stats.acquired == 4
    assert client.stats == ClientStats(requests_sent=4, cache_hits=0)


async def test_cache_hit_does_not_enter_the_limiter(
    router: respx.MockRouter,
    client: BoeClient,
    limiter: RateLimiter,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    await client.get_auxiliary_table("ambitos")
    await client.get_auxiliary_table("ambitos")
    assert limiter.stats.acquired == 1


# --- bad responses -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"this is not json",
        b"",
        b"<html><body>maintenance</body></html>",
        b"[]",
        b'{"status": {"code": "200", "text": "ok"}}',
        b'{"data": []}',
    ],
    ids=["text", "empty", "html", "array", "no-data", "no-status"],
)
async def test_unparseable_or_incomplete_200_raises_bad_response(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    json_response: JsonResponse,
    body: bytes,
) -> None:
    mock_ok(router, SEARCH_URL, body, json_response)
    with pytest.raises(BoeResponseError) as caught:
        await client.search_laws()
    assert caught.value.code == "bad_response"
    assert caught.value.retryable is False
    assert caught.value.status == 200
    assert cache.sets == []


async def test_bad_search_data_type_raises_bad_response(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse
) -> None:
    mock_ok(router, SEARCH_URL, b'{"status":{"code":"200"},"data":"surprise"}', json_response)
    with pytest.raises(BoeResponseError):
        await client.search_laws()


async def test_search_hit_failing_validation_raises_bad_response(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse
) -> None:
    body = b'{"status":{"code":"200"},"data":[{"identificador":"BOE-A-2000-1"}]}'
    mock_ok(router, SEARCH_URL, body, json_response)
    with pytest.raises(BoeResponseError):
        await client.search_laws()


@pytest.mark.parametrize("data", ["[]", '""', '[{"titulo":"sin identificador"}]'])
async def test_metadata_without_a_valid_record_raises_bad_response(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse, data: str
) -> None:
    mock_ok(
        router, METADATA_URL, f'{{"status":{{"code":"200"}},"data":{data}}}'.encode(), json_response
    )
    with pytest.raises(BoeResponseError):
        await client.get_law_metadata(LAW_ID)


async def test_analysis_with_wrong_shape_raises_bad_response(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse
) -> None:
    mock_ok(router, ANALYSIS_URL, b'{"status":{"code":"200"},"data":[]}', json_response)
    with pytest.raises(BoeResponseError):
        await client.get_law_analysis(LAW_ID)


@pytest.mark.parametrize("data", ["[]", '"text"', "null"])
async def test_auxiliary_table_that_is_not_a_mapping_raises_bad_response(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse, data: str
) -> None:
    mock_ok(
        router, AMBITOS_URL, f'{{"status":{{"code":"200"}},"data":{data}}}'.encode(), json_response
    )
    with pytest.raises(BoeResponseError):
        await client.get_auxiliary_table("ambitos")


async def test_bad_body_is_not_cached_and_the_next_call_asks_again(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    route = router.get(AMBITOS_URL).mock(
        side_effect=[json_response(b"not json"), json_response(load_fixture("aux_ambitos.json"))]
    )
    with pytest.raises(BoeResponseError):
        await client.get_auxiliary_table("ambitos")
    assert await client.get_auxiliary_table("ambitos") == {"1": "Estatal", "2": "Autonómico"}
    assert route.call_count == 2


# --- lifecycle and concurrency -----------------------------------------------------------------


@pytest.fixture
def closed_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Record every ``httpx.AsyncClient`` whose ``aclose`` is called."""
    closed: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.aclose

    async def spy(self: httpx.AsyncClient) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", spy)
    return closed


async def test_aclose_leaves_an_injected_http_client_open(
    router: respx.MockRouter,
    client: BoeClient,
    http: httpx.AsyncClient,
    closed_clients: list[httpx.AsyncClient],
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    await client.get_auxiliary_table("ambitos")
    await client.aclose()
    assert http.is_closed is False
    assert http not in closed_clients


async def test_aclose_closes_an_http_client_it_created(
    router: respx.MockRouter,
    cache: RecordingCache,
    limiter: RateLimiter,
    http: httpx.AsyncClient,
    closed_clients: list[httpx.AsyncClient],
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    owned = BoeClient(cache=cache, limiter=limiter)
    await owned.get_auxiliary_table("ambitos")
    await owned.aclose()
    assert len(closed_clients) == 1
    assert closed_clients[0] is not http


async def test_aclose_is_idempotent(
    router: respx.MockRouter,
    cache: RecordingCache,
    limiter: RateLimiter,
    closed_clients: list[httpx.AsyncClient],
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    owned = BoeClient(cache=cache, limiter=limiter)
    await owned.get_auxiliary_table("ambitos")
    await owned.aclose()
    await owned.aclose()
    assert len(closed_clients) == 1


async def test_aclose_twice_with_injected_http_does_not_raise(client: BoeClient) -> None:
    await client.aclose()
    await client.aclose()


async def test_async_context_manager_returns_the_client_and_closes_owned_http(
    router: respx.MockRouter,
    cache: RecordingCache,
    limiter: RateLimiter,
    closed_clients: list[httpx.AsyncClient],
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    async with BoeClient(cache=cache, limiter=limiter) as entered:
        assert isinstance(entered, BoeClient)
        await entered.get_auxiliary_table("ambitos")
        assert closed_clients == []
    assert len(closed_clients) == 1


async def test_async_context_manager_keeps_injected_http_open(
    make_client: MakeClient, http: httpx.AsyncClient
) -> None:
    async with make_client() as entered:
        assert isinstance(entered, BoeClient)
    assert http.is_closed is False


async def test_context_manager_closes_even_when_the_body_raises(
    cache: RecordingCache,
    limiter: RateLimiter,
    router: respx.MockRouter,
    closed_clients: list[httpx.AsyncClient],
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)
    with pytest.raises(RuntimeError):
        async with BoeClient(cache=cache, limiter=limiter) as entered:
            await entered.get_auxiliary_table("ambitos")
            raise RuntimeError("boom")
    assert len(closed_clients) == 1


async def test_stats_start_at_zero_and_are_immutable(client: BoeClient) -> None:
    stats = client.stats
    assert stats == ClientStats(requests_sent=0, cache_hits=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.requests_sent = 1  # type: ignore[misc]


async def test_concurrent_calls_for_different_resources_all_succeed(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    law_ids = [f"BOE-A-2015-{number}" for number in range(10001, 10006)]

    def respond(request: httpx.Request) -> httpx.Response:
        law_id = request.url.path.split("/")[-2]
        return json_response(metadata_body(law_id, load_fixture))

    router.get(url__regex=r".*/legislacion-consolidada/id/[^/]+/metadatos").mock(
        side_effect=respond
    )
    mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)
    mock_ok(router, AMBITOS_URL, load_fixture("aux_ambitos.json"), json_response)

    metadata, search, table = await asyncio.gather(
        asyncio.gather(*(client.get_law_metadata(law_id) for law_id in law_ids)),
        client.search_laws(limit=10),
        client.get_auxiliary_table("ambitos"),
    )

    assert [m.identifier for m in metadata] == law_ids
    assert len(search) == 10
    assert table == {"1": "Estatal", "2": "Autonómico"}
    assert client.stats.requests_sent == 7


# --- single-flight: concurrent identical calls share one request --------------------------------


async def test_concurrent_identical_calls_send_one_request(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return json_response(load_fixture("search_page_limit10.json"))

    route = router.get(SEARCH_URL).mock(side_effect=slow)

    results = await asyncio.gather(*(client.search_laws(limit=10) for _ in range(5)))

    assert route.call_count == 1
    assert client.stats.requests_sent == 1
    assert all(len(result) == 10 for result in results)
    assert all(result == results[0] for result in results)


async def test_concurrent_identical_calls_do_not_share_mutable_results(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return json_response(load_fixture("aux_ambitos.json"))

    router.get(AMBITOS_URL).mock(side_effect=slow)

    first, second = await asyncio.gather(
        client.get_auxiliary_table("ambitos"), client.get_auxiliary_table("ambitos")
    )
    first["1"] = "changed"

    assert second["1"] == "Estatal"


async def test_concurrent_calls_with_different_params_are_not_merged(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return json_response(load_fixture("search_page_limit10.json"))

    router.get(SEARCH_URL).mock(side_effect=slow)

    await asyncio.gather(client.search_laws(limit=10), client.search_laws(limit=11))

    assert client.stats.requests_sent == 2


async def test_concurrent_identical_calls_all_get_the_error_and_the_next_call_retries(
    router: respx.MockRouter,
    client: BoeClient,
    xml_response: XmlResponse,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    async def slow_404(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return xml_response(load_fixture("error_404_unknown_id.xml"), 404)

    route = router.get(METADATA_URL).mock(side_effect=slow_404)

    outcomes = await asyncio.gather(
        *(client.get_law_metadata(LAW_ID) for _ in range(4)), return_exceptions=True
    )

    assert route.call_count == 1
    assert all(isinstance(outcome, BoeNotFoundError) for outcome in outcomes)

    route.mock(
        side_effect=None,
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json")),
    )
    assert (await client.get_law_metadata(LAW_ID)).identifier == LAW_ID
    assert route.call_count == 2


async def test_cancelling_the_first_caller_does_not_break_the_others(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(0.05)
        return json_response(load_fixture("search_page_limit10.json"))

    route = router.get(SEARCH_URL).mock(side_effect=slow)
    first = asyncio.create_task(client.search_laws(limit=10))
    await started.wait()
    second = asyncio.create_task(client.search_laws(limit=10))
    await asyncio.sleep(0)
    first.cancel()

    assert len(await second) == 10
    assert first.cancelled()
    assert client.stats.requests_sent == 1
    assert route.call_count == 1


async def test_cancelling_every_caller_cancels_the_request_and_leaves_nothing_behind(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    started = asyncio.Event()
    request_cancelled = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            request_cancelled.set()
            raise
        return json_response(b"")

    route = router.get(SEARCH_URL).mock(side_effect=hang)
    callers = [asyncio.create_task(client.search_laws(limit=10)) for _ in range(2)]
    await started.wait()
    for caller in callers:
        caller.cancel()
    await asyncio.gather(*callers, return_exceptions=True)
    await asyncio.sleep(0)

    assert request_cancelled.is_set()
    route.mock(
        side_effect=None, return_value=json_response(load_fixture("search_page_limit10.json"))
    )
    assert len(await client.search_laws(limit=10)) == 10
    assert client.stats.requests_sent == 2  # the cancelled one and the new one


# --- a cached body that no longer parses ----------------------------------------------------------

SEARCH_KEY_PARAMS = {"limit": 50, "offset": 0}
STALE_SEARCH_BODY = b'{"status":{"code":"200","text":"ok"},"data":[{"unexpected":"shape"}]}'


async def test_unparseable_cached_body_is_dropped_and_fetched_once_from_the_network(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    key = make_key(SEARCH_URL, SEARCH_KEY_PARAMS, "application/json")
    await cache.set(key, STALE_SEARCH_BODY)
    route = mock_ok(router, SEARCH_URL, load_fixture("search_page_limit10.json"), json_response)

    assert len(await client.search_laws()) == 10

    assert route.call_count == 1
    assert await cache.get(key) == load_fixture("search_page_limit10.json")
    assert client.stats == ClientStats(requests_sent=1, cache_hits=0)


async def test_unparseable_cached_body_and_unparseable_network_body_fail_after_one_fetch(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    json_response: JsonResponse,
) -> None:
    key = make_key(SEARCH_URL, SEARCH_KEY_PARAMS, "application/json")
    await cache.set(key, STALE_SEARCH_BODY)
    route = mock_ok(router, SEARCH_URL, STALE_SEARCH_BODY, json_response)

    with pytest.raises(BoeResponseError):
        await client.search_laws()

    assert route.call_count == 1
    assert await cache.get(key) is None


# --- total time budget -------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only moves when a test (or a fake sleep) advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def timing_out(clock: FakeClock, timeouts_seen: list[float]) -> Callable[[httpx.Request], Any]:
    """A mocked endpoint on which every attempt runs into its own read timeout."""

    def respond(request: httpx.Request) -> httpx.Response:
        seconds: float = request.extensions["timeout"]["read"]
        timeouts_seen.append(seconds)
        clock.now += seconds
        raise httpx.ReadTimeout("slow")

    return respond


def test_defaults_are_a_20_second_attempt_inside_a_45_second_budget(
    router: respx.MockRouter, limiter: RateLimiter
) -> None:
    signature = inspect.signature(BoeClient.__init__)

    assert signature.parameters["timeout"].default == 20.0
    assert signature.parameters["total_timeout"].default == 45.0


async def test_attempts_and_backoffs_never_run_past_the_total_budget(
    router: respx.MockRouter, make_client: MakeClient, clock: FakeClock
) -> None:
    timeouts_seen: list[float] = []
    router.get(SEARCH_URL).mock(side_effect=timing_out(clock, timeouts_seen))
    client = make_client(
        timeout=20.0, total_timeout=45.0, sleep=clock.sleep, clock=clock, max_retries=10
    )

    with pytest.raises(BoeUnavailableError, match="within 45"):
        await client.search_laws()

    assert clock.now <= 45.0
    assert timeouts_seen[0] == 20.0
    assert all(0 < seconds <= 20.0 for seconds in timeouts_seen)
    assert len(timeouts_seen) < 10  # the budget, not max_retries, ended the call


async def test_a_backoff_that_does_not_fit_in_the_budget_is_not_slept(
    router: respx.MockRouter, make_client: MakeClient, clock: FakeClock
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=server_error(503, {"Retry-After": "30"}))
    client = make_client(total_timeout=10.0, sleep=clock.sleep, clock=clock)

    with pytest.raises(BoeUnavailableError) as caught:
        await client.search_laws()

    assert route.call_count == 1
    assert clock.sleeps == []
    assert caught.value.status == 503


async def test_a_backoff_that_fits_in_the_budget_is_still_slept_and_retried(
    router: respx.MockRouter, make_client: MakeClient, clock: FakeClock, load_fixture: Loader
) -> None:
    router.get(SEARCH_URL).mock(
        side_effect=[
            server_error(503),
            httpx.Response(200, content=load_fixture("search_page_limit10.json")),
        ]
    )
    client = make_client(total_timeout=10.0, sleep=clock.sleep, clock=clock)

    assert len(await client.search_laws(limit=10)) == 10
    assert clock.sleeps == [1.0]


async def test_a_budget_already_spent_raises_without_a_request(
    router: respx.MockRouter, make_client: MakeClient, clock: FakeClock
) -> None:
    route = router.get(SEARCH_URL).mock(return_value=server_error(503))
    client = make_client(total_timeout=0.0, sleep=clock.sleep, clock=clock)

    with pytest.raises(BoeUnavailableError):
        await client.search_laws()

    assert route.call_count == 0
