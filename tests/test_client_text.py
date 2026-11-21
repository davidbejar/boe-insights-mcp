"""Contract tests for the text endpoints of ``boe_insights_mcp.client``.

Covers ``get_text_index``, ``get_text_block`` and ``get_initial_note`` (spec increment 2,
sections 3 and 7). Everything runs against recorded fixtures through respx; no real sleeping.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.client import BASE_URL, BoeClient, ClientStats
from boe_insights_mcp.errors import (
    BoeNotFoundError,
    BoeResponseError,
    BoeUnavailableError,
)
from boe_insights_mcp.models import Block, IndexEntry
from boe_insights_mcp.ratelimit import RateLimiter
from tests.conftest import RecordingCache, SleepRecorder

LAW_ID = "BOE-A-2015-10566"
LAW_URL = f"{BASE_URL}legislacion-consolidada/id/{LAW_ID}"
INDEX_URL = f"{LAW_URL}/texto/indice"
BLOCK_URL = f"{LAW_URL}/texto/bloque/a48"
NOTE_URL = f"{LAW_URL}/texto/bloque/no"

HOUR = 3600.0
TEXT_TTL = 6 * HOUR
VERSIONED_TTL = 30 * 24 * HOUR

A48 = "texto_bloque_a48_two_versions.xml"
A4_2 = "texto_bloque_a4-2_articulo46bis.xml"
NOTE_REPEALED = "texto_bloque_no_lopd1999_repealed.xml"
NOT_FOUND_NOTE = "error_404_bloque_no_in_force_law.xml"

GENERIC_500 = (
    b'<?xml version="1.0" encoding="utf-8"?><response><status><code>500</code>'
    b"<text>Internal Server Error</text></status><data/></response>"
)

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]
MakeClient = Callable[..., BoeClient]


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


def server_error() -> httpx.Response:
    return httpx.Response(500, content=GENERIC_500, headers={"Content-Type": "application/xml"})


def index_body(blocks: object, *, wrapper: str = "list") -> bytes:
    """A hand-built index response; ``wrapper`` picks the shape of ``data``."""
    data: object = [{"bloque": blocks}] if wrapper == "list" else {"bloque": blocks}
    return json.dumps({"status": {"code": "200", "text": "ok"}, "data": data}).encode()


def raw_entry(block_id: str, title: str, updated: str = "20200101") -> dict[str, str]:
    return {
        "id": block_id,
        "titulo": title,
        "fecha_actualizacion": updated,
        "url": f"{LAW_URL}/texto/bloque/{block_id}",
    }


# --- get_text_index: request and parsing ----------------------------------------------------------


async def test_index_requests_the_index_url_asking_for_json(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    route = router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    await client.get_text_index(LAW_ID)
    assert route.call_count == 1
    assert route.calls.last.request.headers["accept"] == "application/json"


# (fixture, number of blocks, first block id, blocks with an empty title)
INDEX_FIXTURES = [
    ("texto_indice_ley40_2015_trimmed.json", 45, "co", 2),
    ("texto_indice_constitucion_1978.json", 210, "preambulo", 2),
    ("texto_indice_ley39_2015.json", 195, "preambulo", 2),
    ("texto_indice_codigo_civil_trimmed.json", 135, "preambulo", 2),
    ("texto_indice_iva_1992_trimmed.json", 114, "preambulo", 3),
    ("texto_indice_ley30_1992_repealed_trimmed.json", 45, "no", 3),
    ("texto_indice_ley49_1960_ordinals.json", 36, "preambulo", 2),
    ("texto_indice_lopd1999_repealed_with_no.json", 75, "no", 3),
    ("texto_indice_rd1006_2026_full.json", 13, "pr", 0),
    ("texto_indice_rd1624_1992_dup_articulo1.json", 187, "no", 3),
    ("texto_indice_rd424_2005_nota_no_repeal.json", 181, "no", 3),
    ("texto_indice_rd784_2021_bracket_titles.json", 39, "pr", 0),
]


@pytest.mark.parametrize(("fixture", "count", "first_id", "empty_titles"), INDEX_FIXTURES)
async def test_every_recorded_index_parses_into_index_entries(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    json_response: JsonResponse,
    fixture: str,
    count: int,
    first_id: str,
    empty_titles: int,
) -> None:
    router.get(INDEX_URL).mock(return_value=json_response(load_fixture(fixture)))
    entries = await client.get_text_index(LAW_ID)
    assert len(entries) == count
    assert all(isinstance(entry, IndexEntry) for entry in entries)
    assert entries[0].block_id == first_id
    assert sum(entry.title == "" for entry in entries) == empty_titles


async def test_index_entry_for_an_article_with_a_collision_id_and_nbsp_title(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    entries = {entry.block_id: entry for entry in await client.get_text_index(LAW_ID)}
    assert entries["a4-2"].title == "Artículo 46 bis"
    assert entries["a4-2"].updated_on == date(2019, 11, 5)
    assert entries["a48"].title == "Artículo 48"
    assert entries["a48"].updated_on == date(2020, 12, 31)


async def test_index_keeps_entries_with_empty_titles_and_their_dates(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    first = (await client.get_text_index(LAW_ID))[0]
    assert (first.block_id, first.title, first.updated_on) == ("co", "", date(2015, 10, 2))


async def test_index_keeps_document_order_and_duplicate_titles(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))
    )
    entries = await client.get_text_index(LAW_ID)
    ids = [entry.block_id for entry in entries]
    assert ids.index("a1") < ids.index("a1-2")
    by_id = {entry.block_id: entry for entry in entries}
    assert by_id["a1"].title == by_id["a1-2"].title == "Artículo 1"
    assert by_id["a1"].updated_on == date(1992, 12, 31)
    assert by_id["a1-2"].updated_on == date(2021, 6, 16)


async def test_index_reads_bracketed_titles_as_they_are(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_rd1006_2026_full.json"))
    )
    entries = await client.get_text_index(LAW_ID)
    assert [entry.title for entry in entries[:2]] == ["[preambulo]", "Artículo 1"]
    assert entries[-1].title == "[firma]"


@pytest.mark.parametrize("wrapper", ["list", "object"])
async def test_index_with_a_single_block_as_a_bare_object_yields_one_entry(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse, wrapper: str
) -> None:
    body = index_body(raw_entry("a1", "Artículo 1", "20240102"), wrapper=wrapper)
    router.get(INDEX_URL).mock(return_value=json_response(body))
    entries = await client.get_text_index(LAW_ID)
    assert [(e.block_id, e.title, e.updated_on) for e in entries] == [
        ("a1", "Artículo 1", date(2024, 1, 2))
    ]


async def test_index_data_as_an_object_holding_a_list_is_read_like_a_one_item_list(
    router: respx.MockRouter, client: BoeClient, json_response: JsonResponse
) -> None:
    body = index_body([raw_entry("a1", "Artículo 1"), raw_entry("a2", "Artículo 2")], wrapper="obj")
    router.get(INDEX_URL).mock(return_value=json_response(body))
    assert [e.block_id for e in await client.get_text_index(LAW_ID)] == ["a1", "a2"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"data": [{"bloque": []}]}', id="no-status"),
        pytest.param(b"not json at all", id="not-json"),
        pytest.param(
            b'{"status": {"code": "200"}, "data": [{"bloque": [{"titulo": "x"}]}]}',
            id="entry-without-id",
        ),
    ],
)
async def test_index_with_an_unusable_body_is_a_response_error_and_is_not_cached(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    json_response: JsonResponse,
    body: bytes,
) -> None:
    router.get(INDEX_URL).mock(return_value=json_response(body))
    with pytest.raises(BoeResponseError):
        await client.get_text_index(LAW_ID)
    assert cache.sets == []


# --- get_text_index: caching, errors, validation --------------------------------------------------


async def test_index_is_cached_for_six_hours(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
) -> None:
    router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    await client.get_text_index(LAW_ID)
    assert [ttl for _, ttl in cache.sets] == [TEXT_TTL]


async def test_second_index_call_is_served_from_the_cache(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, json_response: JsonResponse
) -> None:
    route = router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    first = await client.get_text_index(LAW_ID)
    second = await client.get_text_index(LAW_ID)
    assert second == first
    assert route.call_count == 1
    assert client.stats == ClientStats(requests_sent=1, cache_hits=1)


async def test_index_of_an_unknown_law_is_not_found(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(INDEX_URL).mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    with pytest.raises(BoeNotFoundError):
        await client.get_text_index(LAW_ID)
    assert route.call_count == 1


@pytest.mark.parametrize("law_id", ["", "../x", "BOE-A-2015-10566/texto", "boe-a-2015-10566", "x"])
async def test_index_with_an_invalid_law_id_raises_before_any_request(
    router: respx.MockRouter, client: BoeClient, law_id: str
) -> None:
    with pytest.raises(ValueError):
        await client.get_text_index(law_id)
    assert router.calls.call_count == 0


# --- get_text_block: request and result -----------------------------------------------------------


async def test_block_request_asks_for_xml(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48")
    request = route.calls.last.request
    assert request.headers["accept"] == "application/xml"
    assert request.url.path.endswith(f"/id/{LAW_ID}/texto/bloque/a48")


async def test_block_returns_a_parsed_block_with_its_versions(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    block = await client.get_text_block(LAW_ID, "a48")
    assert isinstance(block, Block)
    assert (block.block_id, block.kind, block.title) == ("a48", "precepto", "Artículo 48")
    assert [v.source_law_id for v in block.versions] == ["BOE-A-2015-10566", "BOE-A-2020-17340"]


async def test_block_with_an_opaque_id_is_requested_by_that_id(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(f"{LAW_URL}/texto/bloque/a4-2").mock(
        return_value=xml_response(load_fixture(A4_2), 200)
    )
    block = await client.get_text_block(LAW_ID, "a4-2")
    assert route.call_count == 1
    assert (block.block_id, block.title) == ("a4-2", "Artículo 46 bis")


async def test_block_with_a_url_unsafe_law_id_raises_before_any_request(
    router: respx.MockRouter, client: BoeClient
) -> None:
    with pytest.raises(ValueError):
        await client.get_text_block("../etc", "a48")
    assert router.calls.call_count == 0


# --- get_text_block: block_id validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "block_id",
    [
        pytest.param("", id="empty"),
        pytest.param("a/b", id="slash"),
        pytest.param("../a1", id="parent-directory"),
        pytest.param("..", id="dots-only"),
        pytest.param(".", id="single-dot"),
        pytest.param("a b", id="space"),
        pytest.param(" a1", id="leading-space"),
        pytest.param("a1\n", id="trailing-newline"),
        pytest.param("a?x=1", id="query"),
        pytest.param("a#frag", id="fragment"),
        pytest.param("a%2Fb", id="percent-encoding"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("artículo", id="non-ascii"),
        pytest.param("a" * 61, id="over-60-characters"),
        pytest.param("a\tb", id="tab"),
        pytest.param("a\x00b", id="control-character"),
    ],
)
async def test_invalid_block_id_raises_before_any_request(
    router: respx.MockRouter, client: BoeClient, block_id: str
) -> None:
    with pytest.raises(ValueError):
        await client.get_text_block(LAW_ID, block_id)
    assert router.calls.call_count == 0
    assert client.stats.requests_sent == 0


@pytest.mark.parametrize(
    "block_id", ["a1", "a4-2", "S1", "dt_2", "preambulo", "x" * 60, "7", "a.b", "..a"]
)
async def test_valid_block_ids_are_accepted(
    router: respx.MockRouter,
    client: BoeClient,
    load_fixture: Loader,
    xml_response: XmlResponse,
    block_id: str,
) -> None:
    route = router.get(f"{LAW_URL}/texto/bloque/{block_id}").mock(
        return_value=xml_response(load_fixture(A48), 200)
    )
    await client.get_text_block(LAW_ID, block_id)
    assert route.call_count == 1


async def test_block_with_a_dotted_real_id_is_requested_with_the_dot_kept(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(f"{LAW_URL}/texto/bloque/a51ter.").mock(
        return_value=xml_response(load_fixture(A48), 200)
    )
    await client.get_text_block(LAW_ID, "a51ter.")
    assert (
        route.calls.last.request.url.path
        == f"/datosabiertos/api/legislacion-consolidada/id/{LAW_ID}/texto/bloque/a51ter."
    )


# --- get_text_block: errors -----------------------------------------------------------------------


async def test_unknown_block_is_not_found(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(f"{LAW_URL}/texto/bloque/zzz").mock(
        return_value=xml_response(load_fixture("error_404_unknown_block_zzz.xml"), 404)
    )
    with pytest.raises(BoeNotFoundError) as caught:
        await client.get_text_block(LAW_ID, "zzz")
    assert caught.value.status == 404
    assert caught.value.api_message == "La información solicitada no existe"
    assert route.call_count == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<response><data><bloque id=", id="truncated-xml"),
        pytest.param(b"", id="empty-body"),
        pytest.param(b"<response><data/></response>", id="no-bloque"),
        pytest.param(b'<response><data><bloque tipo="precepto"/></data></response>', id="no-id"),
    ],
)
async def test_unusable_block_body_is_a_response_error_and_nothing_is_cached(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    xml_response: XmlResponse,
    body: bytes,
) -> None:
    router.get(BLOCK_URL).mock(return_value=xml_response(body, 200))
    with pytest.raises(BoeResponseError):
        await client.get_text_block(LAW_ID, "a48")
    assert cache.sets == []


# --- get_text_block: retries on the XML endpoint --------------------------------------------------


async def test_block_request_retries_after_a_server_error_and_returns_the_block(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(BLOCK_URL).mock(
        side_effect=[server_error(), xml_response(load_fixture(A48), 200)]
    )
    block = await client.get_text_block(LAW_ID, "a48")
    assert block.block_id == "a48"
    assert route.call_count == 2
    assert len(sleeps.delays) == 1


async def test_block_request_gives_up_with_unavailable_after_the_retries(
    router: respx.MockRouter, client: BoeClient, sleeps: SleepRecorder, cache: RecordingCache
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await client.get_text_block(LAW_ID, "a48")
    assert route.call_count == 4  # the first attempt plus the default three retries
    assert len(sleeps.delays) == 3
    assert cache.sets == []


# --- get_text_block: caching ----------------------------------------------------------------------


async def test_block_without_updated_on_is_cached_for_six_hours(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48")
    assert [ttl for _, ttl in cache.sets] == [TEXT_TTL]


async def test_block_with_updated_on_is_cached_for_thirty_days(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48", updated_on=date(2020, 12, 31))
    assert [ttl for _, ttl in cache.sets] == [VERSIONED_TTL]


async def test_second_block_call_without_updated_on_is_a_cache_hit(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    first = await client.get_text_block(LAW_ID, "a48")
    second = await client.get_text_block(LAW_ID, "a48")
    assert second == first
    assert route.call_count == 1
    assert client.stats == ClientStats(requests_sent=1, cache_hits=1)


async def test_same_updated_on_is_a_cache_hit(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48", updated_on=date(2020, 12, 31))
    await client.get_text_block(LAW_ID, "a48", updated_on=date(2020, 12, 31))
    assert route.call_count == 1
    assert client.stats == ClientStats(requests_sent=1, cache_hits=1)


async def test_different_updated_on_is_a_cache_miss(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48", updated_on=date(2020, 12, 31))
    await client.get_text_block(LAW_ID, "a48", updated_on=date(2021, 6, 1))
    assert route.call_count == 2
    assert len({key for key, _ in cache.sets}) == 2


async def test_updated_on_is_never_sent_to_the_api(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48", updated_on=date(2020, 12, 31))
    request = route.calls.last.request
    assert request.url.query == b""
    assert "2020" not in str(request.url)
    assert "updated_on" not in str(request.url)


async def test_different_blocks_of_one_law_are_cached_separately(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    router.get(f"{LAW_URL}/texto/bloque/a4-2").mock(
        return_value=xml_response(load_fixture(A4_2), 200)
    )
    await client.get_text_block(LAW_ID, "a48")
    await client.get_text_block(LAW_ID, "a4-2")
    assert (await client.get_text_block(LAW_ID, "a48")).block_id == "a48"
    assert (await client.get_text_block(LAW_ID, "a4-2")).block_id == "a4-2"
    assert client.stats == ClientStats(requests_sent=2, cache_hits=2)


async def test_json_index_and_xml_block_of_one_law_do_not_collide_in_the_cache(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    json_response: JsonResponse,
    xml_response: XmlResponse,
) -> None:
    router.get(INDEX_URL).mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    index = await client.get_text_index(LAW_ID)
    block = await client.get_text_block(LAW_ID, "a48")
    assert len({key for key, _ in cache.sets}) == 2
    assert await client.get_text_index(LAW_ID) == index
    assert await client.get_text_block(LAW_ID, "a48") == block
    assert client.stats == ClientStats(requests_sent=2, cache_hits=2)


async def test_unparseable_cached_block_is_dropped_and_fetched_once_from_the_network(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48")
    (key, _) = cache.sets[0]
    await cache.set(key, b"<response><data><bloque")  # a body that no longer parses

    block = await client.get_text_block(LAW_ID, "a48")

    assert block.block_id == "a48"
    assert route.call_count == 2
    assert await cache.get(key) == load_fixture(A48)


async def test_unparseable_cached_block_and_unparseable_network_body_fail_after_one_fetch(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(BLOCK_URL).mock(return_value=xml_response(load_fixture(A48), 200))
    await client.get_text_block(LAW_ID, "a48")
    (key, _) = cache.sets[0]
    await cache.set(key, b"garbage")
    route.mock(return_value=xml_response(b"<response><data/></response>", 200))

    with pytest.raises(BoeResponseError):
        await client.get_text_block(LAW_ID, "a48")

    assert route.call_count == 2
    assert await cache.get(key) is None


# --- get_text_block: single flight ----------------------------------------------------------------


async def test_concurrent_identical_block_requests_send_one_request(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return xml_response(load_fixture(A48), 200)

    route = router.get(BLOCK_URL).mock(side_effect=slow)

    blocks = await asyncio.gather(*(client.get_text_block(LAW_ID, "a48") for _ in range(5)))

    assert route.call_count == 1
    assert client.stats.requests_sent == 1
    assert all(block == blocks[0] for block in blocks)


async def test_concurrent_block_requests_for_different_blocks_are_not_merged(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return xml_response(load_fixture(A48), 200)

    router.get(BLOCK_URL).mock(side_effect=slow)
    other = router.get(f"{LAW_URL}/texto/bloque/a4-2").mock(side_effect=slow)

    await asyncio.gather(
        client.get_text_block(LAW_ID, "a48"), client.get_text_block(LAW_ID, "a4-2")
    )

    assert client.stats.requests_sent == 2
    assert other.call_count == 1


# --- get_initial_note -----------------------------------------------------------------------------


async def test_initial_note_of_a_repealed_law_is_a_nota_inicial_block(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(NOTE_URL).mock(return_value=xml_response(load_fixture(NOTE_REPEALED), 200))
    note = await client.get_initial_note(LAW_ID)
    assert note is not None
    assert (note.block_id, note.kind) == ("no", "nota_inicial")
    assert note.versions[0].paragraphs[0].text.startswith("Norma derogada")
    assert route.calls.last.request.headers["accept"] == "application/xml"


async def test_initial_note_is_none_when_the_law_has_no_note(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(NOTE_URL).mock(return_value=xml_response(load_fixture(NOT_FOUND_NOTE), 404))
    assert await client.get_initial_note(LAW_ID) is None
    assert route.call_count == 1  # a 404 is not retried
    assert sleeps.delays == []


async def test_absence_of_the_note_is_cached_so_a_second_call_sends_nothing(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    route = router.get(NOTE_URL).mock(return_value=xml_response(load_fixture(NOT_FOUND_NOTE), 404))
    assert await client.get_initial_note(LAW_ID) is None
    assert await client.get_initial_note(LAW_ID) is None
    assert route.call_count == 1
    assert client.stats.requests_sent == 1


async def test_absence_of_the_note_is_cached_for_six_hours(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    router.get(NOTE_URL).mock(return_value=xml_response(load_fixture(NOT_FOUND_NOTE), 404))
    await client.get_initial_note(LAW_ID)
    assert [ttl for _, ttl in cache.sets] == [TEXT_TTL]


async def test_absence_is_remembered_per_law(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    other_law = "BOE-A-1999-23750"
    router.get(NOTE_URL).mock(return_value=xml_response(load_fixture(NOT_FOUND_NOTE), 404))
    other = router.get(f"{BASE_URL}legislacion-consolidada/id/{other_law}/texto/bloque/no").mock(
        return_value=xml_response(load_fixture(NOTE_REPEALED), 200)
    )
    assert await client.get_initial_note(LAW_ID) is None
    note = await client.get_initial_note(other_law)
    assert note is not None
    assert other.call_count == 1


async def test_a_present_note_is_cached_for_six_hours_and_served_from_the_cache(
    router: respx.MockRouter,
    client: BoeClient,
    cache: RecordingCache,
    load_fixture: Loader,
    xml_response: XmlResponse,
) -> None:
    route = router.get(NOTE_URL).mock(return_value=xml_response(load_fixture(NOTE_REPEALED), 200))
    first = await client.get_initial_note(LAW_ID)
    second = await client.get_initial_note(LAW_ID)
    assert first is not None
    assert second == first
    assert route.call_count == 1
    assert [ttl for _, ttl in cache.sets] == [TEXT_TTL]


async def test_a_server_error_is_not_read_as_absence(
    router: respx.MockRouter,
    client: BoeClient,
    sleeps: SleepRecorder,
    cache: RecordingCache,
) -> None:
    route = router.get(NOTE_URL).mock(return_value=server_error())
    with pytest.raises(BoeUnavailableError):
        await client.get_initial_note(LAW_ID)
    assert route.call_count == 4
    assert len(sleeps.delays) == 3
    assert cache.sets == []


async def test_a_server_error_does_not_poison_a_later_call(
    router: respx.MockRouter, client: BoeClient, load_fixture: Loader, xml_response: XmlResponse
) -> None:
    router.get(NOTE_URL).mock(
        side_effect=[server_error()] * 4 + [xml_response(load_fixture(NOTE_REPEALED), 200)]
    )
    with pytest.raises(BoeUnavailableError):
        await client.get_initial_note(LAW_ID)
    note = await client.get_initial_note(LAW_ID)
    assert note is not None


@pytest.mark.parametrize("law_id", ["", "../x", "BOE-A-2015-10566/texto", "lowercase"])
async def test_initial_note_with_an_invalid_law_id_raises_before_any_request(
    router: respx.MockRouter, client: BoeClient, law_id: str
) -> None:
    with pytest.raises(ValueError):
        await client.get_initial_note(law_id)
    assert router.calls.call_count == 0
