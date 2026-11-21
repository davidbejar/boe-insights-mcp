"""Tests of the MCP layer through FastMCP's in-memory client; HTTP is mocked with respx."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date
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
from boe_insights_mcp.models import LawSummary
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import (
    AmendmentHistoryResult,
    ArticleHistoryResult,
    CompareArticleVersionsResult,
    FindCodesResult,
    LawOverview,
    ReadArticleResult,
    ResolveReferenceResult,
    SearchLegislationResult,
)
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
LEY_40 = "BOE-A-2015-10566"
OLD_TOOL_NAMES = {"search_legislation", "resolve_reference", "get_law_overview", "find_codes"}
NEW_TOOL_NAMES = {
    "get_amendment_history",
    "read_article",
    "compare_article_versions",
    "get_article_history",
}
TREND_TOOL_NAMES = {
    "legislation_over_time",
    "top_departments",
    "compare_topics",
    "explain_spike",
    "most_amended_laws",
}
TOOL_NAMES = OLD_TOOL_NAMES | NEW_TOOL_NAMES | TREND_TOOL_NAMES
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
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> None:
    prefix = f"{SEARCH_URL}/id/{LEY_40}"
    router.get(f"{prefix}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{prefix}/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )
    # Ley 40/2015 has no initial note: the BOE answers 404 for block "no".
    router.get(f"{prefix}/texto/bloque/no").mock(
        return_value=xml_response(load_fixture("error_404_bloque_no_in_force_law.xml"), 404)
    )


# --- Discovery: what the model sees ---------------------------------------------------------


async def test_server_exposes_exactly_the_expected_tools(tools: dict[str, Tool]) -> None:
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
    assert "search_legislation" in (tools["find_codes"].description or "")


async def test_new_descriptions_point_to_their_siblings(tools: dict[str, Tool]) -> None:
    descriptions = {name: (tool.description or "") for name, tool in tools.items()}

    assert "resolve_reference" in descriptions["read_article"]
    assert "compare_article_versions" in descriptions["read_article"]
    assert "get_article_history" in descriptions["compare_article_versions"]
    assert "get_amendment_history" in descriptions["get_article_history"]
    assert "compare_article_versions" in descriptions["get_article_history"]
    assert "get_article_history" in descriptions["get_amendment_history"]
    assert "resolve_reference" in descriptions["get_amendment_history"]
    for name in NEW_TOOL_NAMES:
        assert (
            "informational" in descriptions[name].lower() or "informational" in descriptions[name]
        )


async def test_article_parameters_show_accepted_wordings_and_formats(
    tools: dict[str, Tool],
) -> None:
    properties = tools["read_article"].input_schema["properties"]

    assert "46 bis" in properties["article"]["description"]
    assert "art. 1.902" in properties["article"]["description"]
    assert "block_id" in properties["article"]["description"]
    assert "candidates" in properties["block_id"]["description"]
    assert "ISO" in properties["as_of"]["description"]
    assert "resolve_reference" in properties["law_id"]["description"]
    assert (properties["max_chars"]["minimum"], properties["max_chars"]["maximum"]) == (500, 20000)
    assert properties["max_chars"]["default"] == 6000


async def test_compare_and_amendment_parameter_bounds_and_defaults(
    tools: dict[str, Tool],
) -> None:
    compare = tools["compare_article_versions"].input_schema
    history = tools["get_amendment_history"].input_schema["properties"]

    # article and block_id are both optional in the schema: exactly one is checked by the use case.
    assert compare["required"] == ["law_id", "from_date"]
    assert (
        compare["properties"]["max_changes"]["maximum"],
        compare["properties"]["max_changes"]["default"],
    ) == (200, 40)
    assert (
        compare["properties"]["max_chars"]["minimum"],
        compare["properties"]["max_chars"]["maximum"],
        compare["properties"]["max_chars"]["default"],
    ) == (1000, 30000, 8000)
    assert history["group"]["enum"] == [
        "all",
        "repeals",
        "modifies",
        "adds",
        "corrections",
        "other",
    ]
    assert (history["limit"]["minimum"], history["limit"]["maximum"]) == (1, 100)
    assert tools["get_article_history"].input_schema["required"] == ["law_id"]


async def test_instructions_mention_the_article_tools(boe_client: BoeClient) -> None:
    async with Client(create_server(client=boe_client)) as client:
        instructions = client.instructions or ""

    for name in NEW_TOOL_NAMES:
        assert name in instructions


async def test_search_code_parameters_point_to_find_codes(tools: dict[str, Tool]) -> None:
    properties = tools["search_legislation"].input_schema["properties"]

    assert "find_codes" in properties["department_code"]["description"]
    assert "find_codes" in properties["subject_code"]["description"]


async def test_instructions_mention_find_codes(boe_client: BoeClient) -> None:
    async with Client(create_server(client=boe_client)) as client:
        assert "find_codes" in (client.instructions or "")


async def test_overview_description_does_not_promise_certainty(tools: dict[str, Tool]) -> None:
    description = (tools["get_law_overview"].description or "").lower()

    assert "boe flags" in description
    assert "amendment references" in description
    assert "partially_repealed" in description


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
    assert tools["find_codes"].input_schema["required"] == ["kind", "text"]
    assert tools["read_article"].input_schema["required"] == ["law_id"]


async def test_find_codes_kind_is_an_enum_and_limit_is_bounded(tools: dict[str, Tool]) -> None:
    properties = tools["find_codes"].input_schema["properties"]

    assert properties["kind"]["enum"] == ["department", "subject", "rank"]
    assert (properties["limit"]["minimum"], properties["limit"]["maximum"]) == (1, 50)


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


async def test_find_codes_returns_structured_content(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    router.get(f"{BASE_URL}datos-auxiliares/departamentos").mock(
        return_value=json_response(load_fixture("aux_departamentos_trimmed.json"))
    )

    result = await mcp.call_tool(
        "find_codes", {"kind": "department", "text": "proteccion datos", "limit": 5}
    )

    found = FindCodesResult.model_validate(result.structured_content)
    assert [match.code for match in found.matches] == ["1012", "1011"]
    assert (found.returned, found.total_matches) == (2, 2)
    assert found.provenance.generated_at is not None


async def test_find_codes_rejects_an_unknown_kind_and_a_blank_text(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match="kind"):
        await mcp.call_tool("find_codes", {"kind": "gazette", "text": "x"})
    with pytest.raises(ToolError, match=r"invalid_argument.*text"):
        await mcp.call_tool("find_codes", {"kind": "rank", "text": "  "})


@pytest.mark.usefixtures("ley_40_routes")
async def test_get_law_overview_accepts_a_lowercase_identifier(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": LEY_40.lower()})

    assert LawOverview.model_validate(result.structured_content).identifier == LEY_40


@pytest.mark.usefixtures("ley_40_routes")
async def test_provenance_calls_the_assembly_time_generated_at(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": LEY_40})

    assert result.structured_content is not None
    provenance = result.structured_content["provenance"]
    assert "generated_at" in provenance
    assert "retrieved_at" not in provenance


async def test_provenance_fields_are_described_in_the_output_schema(tools: dict[str, Tool]) -> None:
    schema = tools["get_law_overview"].output_schema
    assert schema is not None
    fields = schema["properties"]["provenance"]["properties"]

    assert "cache" in fields["generated_at"]["description"]
    assert "own last-update" in fields["data_updated_at"]["description"]


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


# --- Calls: the article and amendment-history tools ----------------------------------------


@pytest.fixture
def ley_40_text_routes(
    router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    prefix = f"{SEARCH_URL}/id/{LEY_40}"
    router.get(f"{prefix}/texto/indice").mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    for block_id, fixture in (
        ("a4-2", "texto_bloque_a4-2_articulo46bis.xml"),
        ("a48", "texto_bloque_a48_two_versions.xml"),
    ):
        router.get(f"{prefix}/texto/bloque/{block_id}").mock(
            return_value=httpx.Response(
                200, content=load_fixture(fixture), headers={"Content-Type": "application/xml"}
            )
        )
    # Article tools also check whether the whole law is repealed.
    router.get(f"{prefix}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )


@pytest.mark.usefixtures("ley_40_text_routes")
async def test_read_article_returns_structured_content(mcp: Client[Any]) -> None:
    result = await mcp.call_tool(
        "read_article", {"law_id": LEY_40, "article": "46 bis", "as_of": "2020-01-01"}
    )

    article = ReadArticleResult.model_validate(result.structured_content)
    assert (article.outcome, article.block_id, article.state) == ("found", "a4-2", "in_force")
    assert article.version is not None
    assert article.version.introduced_by == "BOE-A-2019-15790"
    assert article.text.startswith("Artículo 46 bis.")
    assert article.provenance.attribution.startswith("Basado en datos")
    assert result.structured_content is not None
    assert result.structured_content["version"]["effective_from"] == "2019-11-06"


@pytest.mark.usefixtures("ley_40_text_routes")
async def test_read_article_not_found_is_a_result_with_candidates_not_an_error(
    mcp: Client[Any],
) -> None:
    result = await mcp.call_tool("read_article", {"law_id": LEY_40, "article": "artículo 9999"})

    article = ReadArticleResult.model_validate(result.structured_content)
    assert article.outcome == "not_found"
    assert article.candidates
    assert article.hint


@pytest.mark.usefixtures("ley_40_text_routes")
async def test_compare_article_versions_returns_structured_content(mcp: Client[Any]) -> None:
    result = await mcp.call_tool(
        "compare_article_versions",
        {"law_id": LEY_40, "article": "48", "from_date": "2017-01-01", "to_date": "2022-01-01"},
    )

    comparison = CompareArticleVersionsResult.model_validate(result.structured_content)
    assert comparison.identical is False
    assert comparison.amending_laws == ["BOE-A-2020-17340"]
    assert comparison.changed == 1
    assert comparison.from_date == date(2017, 1, 1)


@pytest.mark.usefixtures("ley_40_text_routes")
async def test_article_tools_take_a_block_id_instead_of_the_wording(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("read_article", {"law_id": LEY_40, "block_id": "a4-2"})

    article = ReadArticleResult.model_validate(result.structured_content)
    assert (article.outcome, article.block_id) == ("found", "a4-2")
    assert article.law_repealed is False


@pytest.mark.usefixtures("ley_40_text_routes")
@pytest.mark.parametrize(
    "arguments",
    [{}, {"article": "48", "block_id": "a48"}, {"article": "x" * 201}],
    ids=["neither", "both", "too-long"],
)
@pytest.mark.parametrize("tool", ["read_article", "get_article_history"])
async def test_article_tools_reject_a_bad_selector_as_an_invalid_argument(
    mcp: Client[Any], tool: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="invalid_argument"):
        await mcp.call_tool(tool, {"law_id": LEY_40, **arguments})


@pytest.mark.usefixtures("ley_40_text_routes")
async def test_get_article_history_returns_structured_content(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("get_article_history", {"law_id": LEY_40, "article": "48"})

    history = ArticleHistoryResult.model_validate(result.structured_content)
    assert [e.introduced_by for e in history.events] == ["BOE-A-2015-10566", "BOE-A-2020-17340"]
    assert [e.kind for e in history.events] == ["original", "amended"]


async def test_get_amendment_history_returns_structured_content(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )

    result = await mcp.call_tool(
        "get_amendment_history", {"law_id": LEY_40, "group": "modifies", "limit": 5}
    )

    history = AmendmentHistoryResult.model_validate(result.structured_content)
    assert history.group == "modifies"
    assert history.total > 5
    assert len(history.items) == 5
    assert history.has_more is True
    assert history.next_offset == 5
    assert {item.group for item in history.items} == {"modifies"}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("read_article", {"article": "1"}),
        ("compare_article_versions", {"article": "1", "from_date": "2020-01-01"}),
        ("get_article_history", {"article": "1"}),
        ("get_amendment_history", {}),
    ],
)
async def test_unknown_law_error_names_the_tool_advice_for_every_new_tool(
    mcp: Client[Any],
    router: respx.MockRouter,
    xml_response: XmlResponse,
    load_fixture: Loader,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    router.get(url__regex=rf"{SEARCH_URL}/id/BOE-A-1999-1/.*").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool(tool, {"law_id": "BOE-A-1999-1", **arguments})

    assert "not_found" in str(caught.value)
    assert "resolve_reference" in str(caught.value)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("read_article", {"article": "1"}),
        ("get_article_history", {"article": "1"}),
        ("get_amendment_history", {}),
    ],
)
async def test_malformed_identifier_is_an_invalid_argument_for_every_new_tool(
    mcp: Client[Any], tool: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*invalid law identifier"):
        await mcp.call_tool(tool, {"law_id": "Ley 40/2015", **arguments})


async def test_rejected_article_request_advice_names_the_article_tools(
    mcp: Client[Any], router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/texto/indice").mock(
        return_value=xml_response(load_fixture("error_400_unsupported_accept.xml"), 400)
    )

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("read_article", {"law_id": LEY_40, "article": "1"})

    assert "invalid_query" in str(caught.value)
    assert "article wording" in str(caught.value)
    assert "simplify" not in str(caught.value).lower()


async def test_inverted_article_dates_are_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*from_date"):
        await mcp.call_tool(
            "compare_article_versions",
            {"law_id": LEY_40, "article": "48", "from_date": "2022-01-02", "to_date": "2022-01-01"},
        )


async def test_blank_article_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*article"):
        await mcp.call_tool("read_article", {"law_id": LEY_40, "article": "  "})


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("read_article", {"max_chars": 100}, "max_chars"),
        ("read_article", {"as_of": "last year"}, "as_of"),
        ("compare_article_versions", {"from_date": "2020-01-01", "max_changes": 0}, "max_changes"),
        ("compare_article_versions", {}, "from_date"),
        ("get_amendment_history", {"group": "everything"}, "group"),
        ("get_amendment_history", {"limit": 101}, "limit"),
    ],
)
async def test_new_tool_arguments_violating_the_schema_name_the_parameter(
    mcp: Client[Any], tool: str, arguments: dict[str, Any], expected: str
) -> None:
    base = (
        {"law_id": LEY_40, "article": "48"}
        if tool != "get_amendment_history"
        else {"law_id": LEY_40}
    )
    with pytest.raises(ToolError, match=expected):
        await mcp.call_tool(tool, {**base, **arguments})


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


async def test_rejected_identifier_error_does_not_advise_simplifying_the_search(
    mcp: Client[Any], router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(url__regex=rf"{SEARCH_URL}/id/{LEY_40}/.*").mock(
        return_value=xml_response(load_fixture("error_400_unsupported_query_format.xml"), 400)
    )

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("get_law_overview", {"law_id": LEY_40})

    message = str(caught.value)
    assert "invalid_query" in message
    assert "Simplify" not in message
    assert "identifier" in message


async def test_rejected_reference_search_does_not_advise_removing_filters(
    mcp: Client[Any], router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(SEARCH_URL).mock(
        return_value=xml_response(load_fixture("error_500_syntax_code101.xml"), 500)
    )

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("resolve_reference", {"reference": "Ley 40/2015"})

    message = str(caught.value)
    assert "invalid_query" in message
    assert "filters" not in message
    assert "Ley 40/2015" in message or "reference" in message


async def test_an_internal_validation_error_is_masked_not_reported_as_an_argument_error(
    mcp: Client[Any], boe_client: BoeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        LawSummary.model_validate({"identificador": "secret-input-value", "titulo": 5})

    monkeypatch.setattr(boe_client, "search_laws", broken)

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("search_legislation", {})

    assert "secret-input-value" not in str(caught.value)
    assert "invalid_argument" not in str(caught.value)
    assert "ValidationError" not in str(caught.value)


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
