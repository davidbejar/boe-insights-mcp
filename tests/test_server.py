"""Tests of the MCP layer through FastMCP's in-memory client; HTTP is mocked with respx."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import Tool

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import LawOverview, ResolveReferenceResult, SearchLegislationResult
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
LEY_40 = "BOE-A-2015-10566"
TOOL_NAMES = {"search_legislation", "resolve_reference", "get_law_overview"}
MAX_DESCRIPTION_WORDS = 120

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
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
    async with Client(create_server(client=boe_client)) as client:
        yield client


@pytest.fixture
async def tools(mcp: Client[Any]) -> dict[str, Tool]:
    return {tool.name: tool for tool in await mcp.list_tools()}


@pytest.fixture
def ley_40_routes(
    router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    prefix = f"{SEARCH_URL}/id/{LEY_40}"
    router.get(f"{prefix}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{prefix}/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )


# --- Discovery: what the model sees ---------------------------------------------------------


async def test_server_exposes_exactly_the_three_tools(tools: dict[str, Tool]) -> None:
    assert set(tools) == TOOL_NAMES


async def test_server_name_and_instructions_guide_the_model(boe_client: BoeClient) -> None:
    server = create_server(client=boe_client)
    async with Client(server) as client:
        info = client.server_info
        assert info is not None
        name = info.name
        instructions = client.instructions or ""

    assert name == "boe-insights"
    assert "resolve_reference" in instructions
    assert "Basado en datos de la Agencia Estatal Boletín Oficial del Estado" in instructions
    assert "no official legal value" in instructions


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
async def test_tool_description_is_concise(tools: dict[str, Tool], name: str) -> None:
    description = tools[name].description or ""

    assert 20 <= len(description.split()) <= MAX_DESCRIPTION_WORDS


async def test_descriptions_point_to_the_right_neighbouring_tool(tools: dict[str, Tool]) -> None:
    assert "resolve_reference" in (tools["search_legislation"].description or "")
    assert "search_legislation" in (tools["resolve_reference"].description or "")
    assert "resolve_reference" in (tools["get_law_overview"].description or "")


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
async def test_every_parameter_is_described(tools: dict[str, Tool], name: str) -> None:
    properties: dict[str, dict[str, Any]] = tools[name].input_schema["properties"]

    assert properties
    assert "ctx" not in properties
    assert [key for key, schema in properties.items() if not schema.get("description")] == []


async def test_required_parameters(tools: dict[str, Tool]) -> None:
    assert tools["search_legislation"].input_schema.get("required", []) == []
    assert tools["resolve_reference"].input_schema["required"] == ["reference"]
    assert tools["get_law_overview"].input_schema["required"] == ["law_id"]


async def test_search_limit_is_bounded_in_the_schema(tools: dict[str, Tool]) -> None:
    limit = tools["search_legislation"].input_schema["properties"]["limit"]

    assert (limit["minimum"], limit["maximum"]) == (1, 50)


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
async def test_tools_are_annotated_read_only_idempotent_and_open_world(
    tools: dict[str, Tool], name: str
) -> None:
    annotations = tools[name].annotations

    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is True
    assert tools[name].title


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
async def test_tools_declare_an_output_schema_with_provenance(
    tools: dict[str, Tool], name: str
) -> None:
    output_schema = tools[name].output_schema

    assert output_schema is not None
    assert "provenance" in output_schema["properties"]


# --- Calls: structured results --------------------------------------------------------------


async def test_search_legislation_returns_structured_content(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=json_response(load_fixture("search_page_limit10.json"))
    )

    result = await mcp.call_tool("search_legislation", {"rank": "real_decreto", "limit": 5})

    page = SearchLegislationResult.model_validate(result.structured_content)
    assert page.returned == 5
    assert page.has_more is True
    assert page.next_offset == 6  # the fixture's second hit is an Orden, dropped by the filter
    assert page.provenance.attribution.startswith("Basado en datos")


async def test_resolve_reference_returns_structured_content(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=json_response(load_fixture("search_numero_oficial_rango_single.json"))
    )

    result = await mcp.call_tool("resolve_reference", {"reference": "LO 3/2018"})

    resolved = ResolveReferenceResult.model_validate(result.structured_content)
    assert resolved.status == "resolved"
    assert resolved.matches[0].identifier == "BOE-A-2018-16673"


@pytest.mark.usefixtures("ley_40_routes")
async def test_get_law_overview_returns_structured_content(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": LEY_40})

    overview = LawOverview.model_validate(result.structured_content)
    assert overview.identifier == LEY_40
    assert overview.amendments.affected_by_count == 24


@pytest.mark.usefixtures("ley_40_routes")
async def test_structured_content_serialises_dates_as_iso_strings(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": LEY_40})

    assert result.structured_content is not None
    assert result.structured_content["enacted_on"] == "2015-10-01"


async def test_date_arguments_are_parsed_from_iso_strings(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    route = router.get(SEARCH_URL).mock(
        return_value=json_response(load_fixture("search_empty.json"))
    )

    await mcp.call_tool(
        "search_legislation", {"published_from": "2020-01-01", "published_to": "2020-12-31"}
    )

    assert '"gte":"20200101","lte":"20201231"' in route.calls[0].request.url.params["query"]


# --- Calls: errors the model can act on -----------------------------------------------------


async def test_unknown_law_error_says_what_to_do(
    mcp: Client[Any], router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(url__regex=rf"{SEARCH_URL}/id/BOE-A-1999-1/.*").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("get_law_overview", {"law_id": "BOE-A-1999-1"})

    message = str(caught.value)
    assert "not_found" in message
    assert "not retryable" in message
    assert "resolve_reference" in message


async def test_unavailable_api_error_is_marked_retryable(
    mcp: Client[Any], router: respx.MockRouter
) -> None:
    router.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("search_legislation", {})

    assert "BOE API unavailable (unavailable, retryable)" in str(caught.value)


async def test_rejected_query_error_suggests_simplifying(
    mcp: Client[Any], router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=xml_response(load_fixture("error_500_syntax_code101.xml"), 500)
    )

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("search_legislation", {"title": "agua"})

    assert "invalid_query" in str(caught.value)
    assert "Simplify" in str(caught.value)


async def test_malformed_identifier_is_reported_as_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*invalid law identifier"):
        await mcp.call_tool("get_law_overview", {"law_id": "Ley 40/2015"})


async def test_inverted_date_range_is_reported_as_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*published_from"):
        await mcp.call_tool(
            "search_legislation", {"published_from": "2021-01-01", "published_to": "2020-01-01"}
        )


async def test_blank_reference_is_reported_as_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*empty"):
        await mcp.call_tool("resolve_reference", {"reference": "  "})


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"limit": 100}, "limit"),
        ({"rank": "decreto"}, "rank"),
        ({"published_from": "yesterday"}, "published_from"),
    ],
)
async def test_arguments_violating_the_schema_name_the_offending_parameter(
    mcp: Client[Any], arguments: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ToolError, match=expected):
        await mcp.call_tool("search_legislation", arguments)


async def test_unexpected_failures_do_not_leak_internals(
    mcp: Client[Any], boe_client: BoeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret internal detail /srv/app/secret.py")

    monkeypatch.setattr(boe_client, "search_laws", explode)

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("search_legislation", {})

    assert "secret" not in str(caught.value)
    assert "RuntimeError" not in str(caught.value)


# --- Lifecycle ------------------------------------------------------------------------------


async def test_injected_client_is_left_open_when_the_server_stops(
    boe_client: BoeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    async def record_close() -> None:
        closed.append(True)

    monkeypatch.setattr(boe_client, "aclose", record_close)

    async with Client(create_server(client=boe_client)) as client:
        await client.list_tools()

    assert closed == []


def test_building_the_server_touches_neither_disk_nor_network(
    router: respx.MockRouter, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "cache"

    server = create_server(settings=Settings(cache_dir=cache_dir))

    assert isinstance(server, FastMCP)
    assert not cache_dir.exists()
    assert router.calls.call_count == 0


async def test_server_without_injected_client_builds_its_own_from_settings(
    router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader, tmp_path: Path
) -> None:
    route = router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    cache_dir = tmp_path / "cache"
    server = create_server(settings=Settings(cache_dir=cache_dir, min_interval=0))

    async with Client(server) as client:
        result = await client.call_tool("resolve_reference", {"reference": LEY_40})

    assert route.call_count == 1
    assert ResolveReferenceResult.model_validate(result.structured_content).status == "resolved"
    assert cache_dir.exists()


async def test_memory_cache_backend_leaves_the_disk_alone(
    router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader, tmp_path: Path
) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    cache_dir = tmp_path / "cache"
    settings = Settings(cache_backend="memory", cache_dir=cache_dir, min_interval=0)

    async with Client(create_server(settings=settings)) as client:
        await client.call_tool("resolve_reference", {"reference": LEY_40})

    assert not cache_dir.exists()
