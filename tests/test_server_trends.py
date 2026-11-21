"""Tests of the five trend tools through FastMCP's in-memory client; HTTP is mocked with respx.

The catalogue is the recorded ``search_bulk_sample_selected.json`` (8 distinct laws, one of them
listed twice). Dates in the arguments are fixed so that results do not depend on the day the
tests run, except where a far-future range is used on purpose to get unfinished periods.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Tool

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.legislation import relation_group
from boe_insights_mcp.models import LawAnalysis
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import (
    CompareTopicsResult,
    ExplainSpikeResult,
    LegislationOverTimeResult,
    MostAmendedLawsResult,
    TopDepartmentsResult,
)
from boe_insights_mcp.server import create_server

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
TREND_TOOLS = [
    "legislation_over_time",
    "top_departments",
    "compare_topics",
    "explain_spike",
    "most_amended_laws",
]
# 2010 to 2025: three laws of the sample (2011, 2019, 2025) and no unfinished period.
PAST = {"from_date": "2010-01-01", "to_date": "2025-12-31"}

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]


@pytest.fixture
def catalogue_route(
    router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> respx.Route:
    """Answer every search with the sample catalogue, except body-text searches (one law) and
    the one-record confirmation after the last page (nothing)."""
    sample = json.loads(load_fixture("search_bulk_sample_selected.json"))
    one_law = {**sample, "data": [sample["data"][1]]}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        body = one_law if "texto:" in query else sample
        if request.url.params.get("limit") == "1":
            body = {**sample, "data": []}
        return json_response(json.dumps(body).encode())

    return router.get(SEARCH_URL).mock(side_effect=handler)


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


@pytest.fixture
async def mcp(boe_client: BoeClient, catalogue_route: respx.Route) -> AsyncIterator[Client[Any]]:
    async with Client(create_server(client=boe_client)) as client:
        yield client


async def call(mcp: Client[Any], tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


# --- discovery ---------------------------------------------------------------------------------


async def test_the_server_instructions_mention_the_trend_tools(boe_client: BoeClient) -> None:
    async with Client(create_server(client=boe_client)) as client:
        instructions = client.instructions or ""

    for name in TREND_TOOLS:
        assert name in instructions
    assert "Basado en datos de la Agencia Estatal Boletín Oficial del Estado" in instructions


async def test_the_trend_tools_are_registered_with_object_schemas(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    for name in TREND_TOOLS:
        assert name in tools
        output = tools[name].output_schema
        assert output is not None
        assert {"coverage", "caveats", "provenance"} <= set(output["properties"])


async def test_parameter_bounds_are_in_the_schemas(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    def bounds(tool: str, parameter: str) -> tuple[Any, Any]:
        schema = tools[tool].input_schema["properties"][parameter]
        return schema["minimum"], schema["maximum"]

    assert bounds("legislation_over_time", "top_groups") == (1, 10)
    assert bounds("top_departments", "limit") == (1, 50)
    assert bounds("explain_spike", "baseline_periods") == (2, 20)
    assert bounds("explain_spike", "max_laws") == (1, 50)
    assert bounds("most_amended_laws", "limit") == (1, 25)
    assert bounds("most_amended_laws", "max_laws") == (1, 60)
    topics = tools["compare_topics"].input_schema["properties"]["topics"]
    assert (topics["minItems"], topics["maxItems"]) == (2, 5)
    assert tools["compare_topics"].input_schema["required"] == ["topics"]


def property_descriptions(schema: Any, name: str) -> list[str]:
    """Every description given to a property called ``name`` anywhere in a JSON schema."""
    found: list[str] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict) and isinstance(properties.get(name), dict):
            found.append(properties[name].get("description", ""))
        for value in schema.values():
            found.extend(property_descriptions(value, name))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(property_descriptions(value, name))
    return found


async def test_date_range_and_matching_descriptions_are_the_decided_ones(
    mcp: Client[Any],
) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    def described(tool: str, parameter: str) -> str:
        return tools[tool].input_schema["properties"][parameter]["description"]

    assert described("most_amended_laws", "from_date") == (
        "Only laws published on or after this date, ISO format."
    )
    assert described("most_amended_laws", "to_date") == (
        "Only laws published on or before this date, ISO format."
    )
    assert "19 years before to_date" in described("legislation_over_time", "from_date")
    assert "'vivienda' does not match 'viviendas'" in described("explain_spike", "title")
    assert property_descriptions(tools["compare_topics"].output_schema, "first") == [
        "First period in the range with a law of the topic."
    ]
    # Once for the series and once for each group's series.
    assert set(
        property_descriptions(tools["legislation_over_time"].output_schema, "highest_number")
    ) == {
        "Highest official number seen that year for this rank: a lower bound of how many were "
        "enacted, to judge coverage."
    }


# --- legislation_over_time ---------------------------------------------------------------------


async def test_legislation_over_time_returns_the_series_coverage_and_provenance(
    mcp: Client[Any],
) -> None:
    content = await call(mcp, "legislation_over_time", **PAST)

    result = LegislationOverTimeResult.model_validate(content)
    counts = {p.period: p.count for p in result.series}
    assert len(result.series) == 16
    assert (counts["2011"], counts["2019"], counts["2025"]) == (1, 1, 1)
    assert result.total == 3
    assert result.coverage.catalogue_size == 8
    assert result.coverage.matched == 8
    assert content["provenance"]["attribution"].startswith("Basado en datos de la Agencia")
    assert content["provenance"]["data_updated_at"] is not None
    assert content["from_date"] == "2010-01-01"
    assert "groups" in content


async def test_a_false_partial_flag_is_left_out_and_a_true_one_is_kept(mcp: Client[Any]) -> None:
    past = await call(mcp, "legislation_over_time", **PAST)
    future = await call(mcp, "legislation_over_time", from_date="2095-01-01", to_date="2099-06-30")

    assert all("partial" not in point for point in past["series"])
    assert future["series"][-1] == {"period": "2099", "count": 0, "partial": True}
    assert all(point["partial"] is True for point in future["series"])  # all after today
    assert any("partial" in caveat for caveat in future["caveats"])


async def test_filters_and_grouping_reach_the_use_case(mcp: Client[Any]) -> None:
    content = await call(
        mcp,
        "legislation_over_time",
        title="ley",
        scope="regional",
        group_by="scope",
        top_groups=2,
        **PAST,
    )

    # The regional laws titled "ley" published in 2010-2025: DOGC 2019, Basque 2011, BOJA 2025.
    assert content["total"] == 3
    assert content["groups"][0]["label"] == "Autonómico"
    assert content["groups"][0]["code"] == "2"


async def test_the_default_range_covers_twenty_years(mcp: Client[Any]) -> None:
    content = await call(mcp, "legislation_over_time")

    assert len(content["series"]) in (19, 20, 21)
    assert content["to_date"] >= "2026-11-21"


async def test_an_inverted_range_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*from_date"):
        await mcp.call_tool(
            "legislation_over_time", {"from_date": "2020-01-01", "to_date": "2019-01-01"}
        )


async def test_a_value_outside_the_schema_names_the_parameter(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match="top_groups"):
        await mcp.call_tool("legislation_over_time", {"top_groups": 11})
    with pytest.raises(ToolError, match="period"):
        await mcp.call_tool("legislation_over_time", {"period": "decade"})


async def test_a_malformed_subject_code_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match="invalid_argument"):
        await mcp.call_tool("legislation_over_time", {"subject_code": "1 OR 2"})


# --- the catalogue is shared -------------------------------------------------------------------


async def test_the_catalogue_is_downloaded_once_for_all_the_tools(
    mcp: Client[Any], catalogue_route: respx.Route
) -> None:
    await call(mcp, "legislation_over_time", **PAST)
    await call(mcp, "top_departments", **PAST)
    await call(mcp, "explain_spike", period_key="2025")

    assert catalogue_route.call_count == 2  # one page and its end-of-catalogue confirmation


async def test_progress_is_reported_while_the_catalogue_downloads(
    boe_client: BoeClient, catalogue_route: respx.Route
) -> None:
    reports: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        reports.append((progress, total, message))

    async with Client(create_server(client=boe_client)) as client:
        await client.call_tool("legislation_over_time", dict(PAST), progress_handler=on_progress)

    assert reports
    assert reports[0][0] == 8  # the sample has 8 distinct laws
    assert "catalogue" in (reports[0][2] or "")


async def test_a_failed_catalogue_download_is_a_retryable_tool_error(
    router: respx.MockRouter, boe_client: BoeClient
) -> None:
    router.get(SEARCH_URL).mock(return_value=httpx.Response(503, content=b"<status/>"))

    async with Client(create_server(client=boe_client)) as client:
        with pytest.raises(ToolError, match="unavailable, retryable"):
            await client.call_tool("top_departments", {})


# --- top_departments ---------------------------------------------------------------------------


async def test_top_departments_lists_departments_with_counts_and_shares(mcp: Client[Any]) -> None:
    content = await call(
        mcp, "top_departments", from_date="2019-01-01", to_date="2026-12-31", limit=2
    )

    result = TopDepartmentsResult.model_validate(content)
    assert result.total == 6  # 2019, 2025 and four of 2026; 1934 and 2011 are outside
    assert [d.count for d in result.departments] == [1, 1]
    assert result.other_count == 4
    assert result.distinct_departments == 6
    assert content["coverage"]["matched"] == 8
    assert content["caveats"][0].startswith("Counts cover")


async def test_top_departments_limit_is_validated_by_the_schema(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match="limit"):
        await mcp.call_tool("top_departments", {"limit": 0})


# --- compare_topics ----------------------------------------------------------------------------


async def test_compare_topics_returns_one_entry_per_topic_in_order(mcp: Client[Any]) -> None:
    content = await call(
        mcp,
        "compare_topics",
        topics=[{"name": "leyes", "title": "ley"}, {"name": "resoluciones", "title": "resolución"}],
        **PAST,
    )

    result = CompareTopicsResult.model_validate(content)
    assert [t.name for t in result.topics] == ["leyes", "resoluciones"]
    assert result.topics[0].total == 3  # DOGC 2019, Basque 2011, BOJA Decreto-ley 2025
    assert result.topics[0].first == "2011"
    assert result.topics[0].share_of_all == pytest.approx(1.0)
    assert result.coverage.matched == 8


async def test_a_body_text_topic_uses_the_api_and_adds_its_caveat(
    mcp: Client[Any], catalogue_route: respx.Route
) -> None:
    content = await call(
        mcp,
        "compare_topics",
        topics=[{"name": "a", "text": "incendios"}, {"name": "b", "title": "ley"}],
        **PAST,
    )

    assert content["topics"][0]["total"] == 1
    assert any("Body-text matching" in caveat for caveat in content["caveats"])
    assert any("texto" in str(call_.request.url) for call_ in catalogue_route.calls)


async def test_compare_topics_needs_two_to_five_topics(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match="topics"):
        await mcp.call_tool("compare_topics", {"topics": [{"name": "a", "title": "ley"}]})


async def test_a_topic_without_a_criterion_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*criterion"):
        await mcp.call_tool(
            "compare_topics", {"topics": [{"name": "a"}, {"name": "b", "title": "ley"}]}
        )


# --- explain_spike -----------------------------------------------------------------------------


async def test_explain_spike_explains_a_named_period(mcp: Client[Any]) -> None:
    content = await call(mcp, "explain_spike", period_key="2026", baseline_periods=3)

    result = ExplainSpikeResult.model_validate(content)
    assert result.found is True
    assert (result.period, result.count, result.baseline_periods) == ("2026", 4, 3)
    assert result.laws_total == 4
    assert {law.identifier for law in result.laws} == {
        "BOE-A-2026-20822",
        "BOE-A-2026-22008",
        "BOE-A-2026-23272",
        "BOE-A-2026-23892",
    }
    assert result.contributions
    assert (
        any("partial" in caveat for caveat in result.caveats) or content["to_date"] > "2026-12-31"
    )


async def test_explain_spike_without_a_key_reports_when_nothing_stands_out(
    mcp: Client[Any],
) -> None:
    content = await call(mcp, "explain_spike", **PAST)

    assert content["found"] is False
    assert content["period"] is None
    assert content["note"]
    assert content["provenance"]["generated_at"]


async def test_a_period_that_contradicts_the_key_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*period"):
        await mcp.call_tool("explain_spike", {"period_key": "2020-Q2", "period": "year"})
    with pytest.raises(ToolError, match="period_key"):
        await mcp.call_tool("explain_spike", {"period_key": "20-20"})


# --- most_amended_laws -------------------------------------------------------------------------


def amendment_count(body: bytes) -> int:
    """Amendments in an analysis fixture, counted with the classifier of get_amendment_history."""
    analysis = LawAnalysis.model_validate(json.loads(body)["data"][0])
    return sum(
        relation_group(ref.relation.text) in ("modifies", "adds", "repeals")
        for ref in analysis.later
    )


async def test_most_amended_laws_ranks_and_reports_completeness(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    body = load_fixture("analisis_ley40_2015.json")
    analysis_routes = router.get(url__regex=rf"{SEARCH_URL}/id/[A-Za-z0-9-]+/analisis").mock(
        return_value=json_response(body)
    )

    content = await call(mcp, "most_amended_laws", title="ley", **PAST)

    result = MostAmendedLawsResult.model_validate(content)
    assert (result.candidates, result.examined, result.failed, result.complete) == (3, 3, 0, True)
    assert result.note is None
    assert len(result.laws) == 3
    assert {item.amendments for item in result.laws} == {amendment_count(body)}
    assert [item.law.identifier for item in result.laws] == sorted(
        item.law.identifier for item in result.laws
    )  # equal counts: ordered by identifier
    assert set(result.laws[0].by_kind) <= {"modifies", "adds", "repeals"}
    assert analysis_routes.call_count == 3
    assert content["provenance"]["notice"]


async def test_most_amended_laws_continues_where_it_stopped(
    mcp: Client[Any], router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    router.get(url__regex=rf"{SEARCH_URL}/id/[A-Za-z0-9-]+/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )

    first = await call(mcp, "most_amended_laws", title="ley", max_laws=1, **PAST)
    second = await call(mcp, "most_amended_laws", title="ley", max_laws=1, **PAST)

    assert (first["examined"], first["complete"]) == (1, False)
    assert first["note"].startswith("Examined 1 of 3 matching laws. Call again")
    assert (second["examined"], second["complete"]) == (2, False)  # one cached plus one new


async def test_most_amended_laws_requires_a_narrowing_filter(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*filter"):
        await mcp.call_tool("most_amended_laws", {"scope": "state"})


async def test_most_amended_laws_skips_laws_whose_analysis_is_missing(
    mcp: Client[Any], router: respx.MockRouter, xml_response: JsonResponse, load_fixture: Loader
) -> None:
    # One law's analysis failing, retryable or not, no longer fails the ranking: it is counted.
    router.get(url__regex=rf"{SEARCH_URL}/id/[A-Za-z0-9-]+/analisis").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    result = await call(mcp, "most_amended_laws", title="ley", **PAST)

    assert result["laws"] == []
    assert (result["examined"], result["failed"], result["complete"]) == (
        0,
        result["candidates"],
        False,  # a law that could not be read leaves the ranking incomplete
    )
    assert result["candidates"] > 0
