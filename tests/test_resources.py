"""Tests of the MCP resources through FastMCP's in-memory client; HTTP is mocked with respx.

Each resource must return exactly what the matching use case returns, so the tests compute the
expected value by calling the use case over the same mocked client.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from mcp import MCPError

from boe_insights_mcp import articles, gazette, legislation, resources
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
SUMMARY_URL = f"{BASE_URL}boe/sumario/"
LEY_40 = "BOE-A-2015-10566"
TEMPLATES = {
    "boe://law/{law_id}": "law",
    "boe://law/{law_id}/article/{block_id}": "article",
    "boe://gazette/{day}": "gazette_day",
    "boe://codes/{kind}": "codes",
}

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    """A client over the mocked transport, with no waiting."""

    async def no_sleep(_: float) -> None:
        return None

    return BoeClient(
        cache=MemoryCache(),
        limiter=RateLimiter(min_interval=0),
        http=http,
        max_retries=0,
        sleep=no_sleep,
    )


@pytest.fixture
async def mcp(boe_client: BoeClient) -> AsyncIterator[Client[Any]]:
    """An in-memory client of a bare server that only has the resources registered."""

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
        yield {"client": boe_client}

    server = FastMCP("test", lifespan=lifespan, mask_error_details=True)
    resources.register(server)
    async with Client(server) as client:
        yield client


@pytest.fixture
def ley_40_routes(
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> None:
    """Metadata, analysis and index routes for Ley 40/2015, plus two article blocks."""
    prefix = f"{SEARCH_URL}/id/{LEY_40}"
    router.get(f"{prefix}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{prefix}/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )
    router.get(f"{prefix}/texto/bloque/no").mock(
        return_value=xml_response(load_fixture("error_404_bloque_no_in_force_law.xml"), 404)
    )
    router.get(f"{prefix}/texto/indice").mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    router.get(f"{prefix}/texto/bloque/a4-2").mock(
        return_value=httpx.Response(
            200,
            content=load_fixture("texto_bloque_a4-2_articulo46bis.xml"),
            headers={"Content-Type": "application/xml"},
        )
    )


def without_clock(value: Any) -> Any:
    """Drop every ``generated_at`` (the only field that depends on the moment of the call)."""
    if isinstance(value, dict):
        return {k: without_clock(v) for k, v in value.items() if k != "generated_at"}
    if isinstance(value, list):
        return [without_clock(v) for v in value]
    return value


async def read_json(mcp: Client[Any], uri: str) -> Any:
    """Read a resource and parse its single JSON text."""
    contents = await mcp.read_resource(uri)
    assert len(contents) == 1
    assert contents[0].mime_type == "application/json"
    return json.loads(contents[0].text)  # type: ignore[union-attr]


# --- discovery -------------------------------------------------------------------------------


async def test_the_four_templates_are_listed_with_texts_and_mime_type(mcp: Client[Any]) -> None:
    listed = await mcp.list_resource_templates()

    assert {t.uri_template: t.name for t in listed} == TEMPLATES
    for template in listed:
        assert template.title
        assert template.mime_type == "application/json"
        assert template.description
        assert len(template.description.split()) <= 40


async def test_the_codes_description_names_the_two_kinds_and_points_to_find_codes(
    mcp: Client[Any],
) -> None:
    listed = {t.name: t for t in await mcp.list_resource_templates()}

    description = listed["codes"].description or ""
    assert all(kind in description for kind in ("department", "rank"))
    # The subject table (about 6,800 entries, 0.5 MB) is too large to attach to a conversation.
    assert "find_codes" in description
    assert "subject" in description


# --- reading ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("ley_40_routes")
async def test_a_law_returns_what_get_law_overview_returns(
    mcp: Client[Any], boe_client: BoeClient
) -> None:
    expected = await legislation.get_law_overview(boe_client, LEY_40)

    actual = await read_json(mcp, f"boe://law/{LEY_40}")

    assert without_clock(actual) == without_clock(json.loads(expected.model_dump_json()))
    assert actual["provenance"]["attribution"].startswith("Basado en datos")
    assert actual["provenance"]["source_url"] == "https://www.boe.es"


@pytest.mark.usefixtures("ley_40_routes")
async def test_an_article_returns_what_read_article_returns(
    mcp: Client[Any], boe_client: BoeClient
) -> None:
    expected = await articles.read_article(boe_client, LEY_40, block_id="a4-2")

    actual = await read_json(mcp, f"boe://law/{LEY_40}/article/a4-2")

    assert without_clock(actual) == without_clock(json.loads(expected.model_dump_json()))
    assert actual["block_id"] == "a4-2"
    assert actual["text"].startswith("Artículo 46 bis.")


async def test_a_day_returns_what_daily_summary_returns(
    mcp: Client[Any],
    boe_client: BoeClient,
    router: respx.MockRouter,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> None:
    router.get(f"{SUMMARY_URL}20261120").mock(
        return_value=xml_response(load_fixture("sumario_boe_20261120_trimmed.xml"), 200)
    )
    expected = await gazette.daily_summary(
        boe_client, day=date(2026, 11, 20), limit=gazette.MAX_LIMIT
    )

    actual = await read_json(mcp, "boe://gazette/2026-11-20")

    assert without_clock(actual) == without_clock(json.loads(expected.model_dump_json()))
    assert actual["total_items"] == 31


@pytest.mark.parametrize(
    ("kind", "fixture", "route"),
    [
        ("department", "aux_departamentos_trimmed.json", "departamentos"),
        ("rank", "aux_rangos.json", "rangos"),
    ],
)
async def test_a_code_table_is_returned_whole_sorted_by_text(
    mcp: Client[Any],
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_fixture: Loader,
    load_json: Callable[[str], Any],
    kind: str,
    fixture: str,
    route: str,
) -> None:
    router.get(f"{BASE_URL}datos-auxiliares/{route}").mock(
        return_value=json_response(load_fixture(fixture))
    )

    actual = await read_json(mcp, f"boe://codes/{kind}")

    table = load_json(fixture)["data"]
    assert (actual["kind"], actual["total"]) == (kind, len(table))
    assert {c["code"]: c["text"] for c in actual["codes"]} == table
    texts = [c["text"].casefold() for c in actual["codes"]]
    assert texts == sorted(texts)
    assert actual["provenance"]["attribution"].startswith("Basado en datos")
    assert (
        "informational" in actual["provenance"]["notice"]
        or "information" in (actual["provenance"]["notice"])
    )


# --- errors ----------------------------------------------------------------------------------


async def test_an_unknown_law_gives_the_curated_not_found_message(
    mcp: Client[Any],
    router: respx.MockRouter,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> None:
    router.get(f"{SEARCH_URL}/id/BOE-A-1900-1/metadatos").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    router.get(f"{SEARCH_URL}/id/BOE-A-1900-1/analisis").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    router.get(f"{SEARCH_URL}/id/BOE-A-1900-1/texto/bloque/no").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    with pytest.raises(MCPError) as caught:
        await mcp.read_resource("boe://law/BOE-A-1900-1")

    message = str(caught.value)
    assert "not_found" in message
    assert "not retryable" in message
    assert "Traceback" not in message


@pytest.mark.parametrize("day", ["notadate", "20261120", "2026-13-40"])
async def test_a_malformed_day_gives_a_clear_error_without_a_request(
    mcp: Client[Any], router: respx.MockRouter, day: str
) -> None:
    with pytest.raises(MCPError) as caught:
        await mcp.read_resource(f"boe://gazette/{day}")

    assert "Invalid argument (invalid_argument, not retryable)" in str(caught.value)
    assert "ISO" in str(caught.value)
    assert router.calls.call_count == 0


async def test_an_unknown_kind_names_the_two_valid_kinds(
    mcp: Client[Any], router: respx.MockRouter
) -> None:
    with pytest.raises(MCPError) as caught:
        await mcp.read_resource("boe://codes/gazette")

    message = str(caught.value)
    assert "Invalid argument (invalid_argument, not retryable)" in message
    assert "department" in message and "rank" in message
    assert "subject" not in message
    assert router.calls.call_count == 0


async def test_the_subject_table_is_not_offered_and_the_error_points_to_find_codes(
    mcp: Client[Any], router: respx.MockRouter
) -> None:
    with pytest.raises(MCPError) as caught:
        await mcp.read_resource("boe://codes/subject")

    message = str(caught.value)
    assert "Invalid argument (invalid_argument, not retryable)" in message
    assert "use the find_codes tool for subjects" in message
    assert router.calls.call_count == 0


@pytest.mark.parametrize(
    "uri",
    [
        "boe://law/..",
        "boe://law/..%2F..%2Fetc",
        f"boe://law/{LEY_40}/article/..",
        "boe://gazette/..",
        "boe://codes/..",
        "boe://law/BOE-A-2015-10566%00",
    ],
)
async def test_path_tricks_never_reach_the_client(
    mcp: Client[Any], router: respx.MockRouter, uri: str
) -> None:
    with pytest.raises(MCPError):
        await mcp.read_resource(uri)

    assert router.calls.call_count == 0


async def test_a_malformed_law_id_is_an_invalid_argument_without_a_request(
    mcp: Client[Any], router: respx.MockRouter
) -> None:
    with pytest.raises(MCPError) as caught:
        await mcp.read_resource("boe://law/not-an-id")

    assert "Invalid argument (invalid_argument, not retryable)" in str(caught.value)
    assert router.calls.call_count == 0


async def test_the_gazette_day_description_says_it_gives_the_first_hundred_items(
    mcp: Client[Any],
) -> None:
    templates = {t.name: t for t in await mcp.list_resource_templates()}

    description = templates["gazette_day"].description or ""

    assert "first 100 items" in description
