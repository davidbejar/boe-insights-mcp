"""Tests of the two gazette tools through FastMCP's in-memory client; HTTP is mocked with respx.

Every call names its dates, so nothing depends on the day the tests run: the default-date
behaviour is covered with a fixed clock in ``test_gazette``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Tool

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import DailySummaryResult, ScanGazetteResult
from boe_insights_mcp.server import create_server

SUMMARY_URL = f"{BASE_URL}boe/sumario/"
Loader = Callable[[str], bytes]
Xml = Callable[..., httpx.Response]


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


@pytest.fixture
async def mcp(boe_client: BoeClient) -> AsyncIterator[Client[Any]]:
    async with Client(create_server(client=boe_client)) as client:
        yield client


@pytest.fixture
def friday(router: respx.MockRouter, xml_response: Xml, load_fixture: Loader) -> respx.Route:
    """2026-11-20 answers with the recorded ordinary day."""
    return router.get(f"{SUMMARY_URL}20261120").mock(
        return_value=xml_response(load_fixture("sumario_boe_20261120_trimmed.xml"), 200)
    )


@pytest.fixture
def sunday(router: respx.MockRouter, xml_response: Xml, load_fixture: Loader) -> respx.Route:
    """2026-11-15 answers 404, as every Sunday does."""
    return router.get(f"{SUMMARY_URL}20261115").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )


async def call(mcp: Client[Any], tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


# --- discovery ---------------------------------------------------------------------------------


async def test_the_instructions_mention_the_gazette_tools(boe_client: BoeClient) -> None:
    async with Client(create_server(client=boe_client)) as client:
        instructions = client.instructions or ""
        names = [tool.name for tool in await client.list_tools()]

    assert (
        "the daily gazette (get_daily_summary and scan_gazette: everything published since 1960)"
    ) in instructions
    assert "Most identifiers found in the gazette are not in the consolidated collection" in (
        instructions
    )
    assert "resolve_reference turns a name" in instructions
    # Every tool is named once the server has 23 of them, and the text stays short.
    assert len(names) == 23
    assert all(name in instructions for name in names)
    assert len(instructions) <= 1760


async def test_the_tools_carry_the_agreed_descriptions_and_schemas(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    summary, scan = tools["get_daily_summary"], tools["scan_gazette"]
    assert " ".join((summary.description or "").split()) == (
        "Everything the BOE published on one day: dispositions, appointments, grants and "
        "announcements, by section and department. Covers the whole gazette since 1960-09-01, "
        "not just consolidated laws. Without a date it gives the latest issue."
    )
    assert " ".join((scan.description or "").split()) == (
        "Find items published in the BOE over a range of up to 31 days whose title has given "
        "words, or that belong to a section or department. Matches titles only, whole words; "
        "singular and plural forms both match. When complete is false, call again to continue."
    )
    for tool in (summary, scan):
        assert tool.input_schema["properties"]["words"]["description"] == (
            "Words that must all appear in the title (whole words; singular and plural forms "
            "both match)."
        )
        limit = tool.input_schema["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 25)
        assert tool.input_schema.get("required", []) == []
    assert set(summary.input_schema["properties"]) == {
        "date",
        "section",
        "department_code",
        "words",
        "offset",
        "limit",
    }
    assert set(scan.input_schema["properties"]) == {
        "words",
        "section",
        "department_code",
        "from_date",
        "to_date",
        "offset",
        "limit",
    }
    assert summary.output_schema is not None and scan.output_schema is not None
    assert {"matched", "items", "note", "provenance"} <= set(summary.output_schema["properties"])
    assert {"complete", "days_pending", "days_failed", "by_day"} <= set(
        scan.output_schema["properties"]
    )


# --- get_daily_summary -------------------------------------------------------------------------


async def test_a_day_is_returned_as_structured_content(
    mcp: Client[Any], friday: respx.Route
) -> None:
    result = await call(mcp, "get_daily_summary", date="2026-11-20", limit=5)

    page = DailySummaryResult.model_validate(result)
    assert friday.calls.last.request.headers["Accept"] == "application/xml"
    assert page.published is True
    assert page.issues[0].identifier == "BOE-S-2026-289"
    assert (page.returned, page.has_more, page.next_offset) == (5, True, 5)
    assert page.total_items == page.matched == 31
    assert page.provenance.attribution.startswith("Basado en datos")
    assert page.provenance.data_updated_at == datetime(2026, 11, 20, tzinfo=UTC)
    assert result["provenance"]["source_url"] == "https://www.boe.es"


async def test_a_heading_is_left_out_of_an_entry_that_has_none(
    mcp: Client[Any], friday: respx.Route
) -> None:
    result = await call(mcp, "get_daily_summary", date="2026-11-20", limit=100)

    keys = [set(entry) for entry in result["items"]]
    assert {"identifier", "title", "section", "department", "url"} <= set.intersection(*keys)
    assert any("heading" in entry for entry in result["items"])
    assert any("heading" not in entry for entry in result["items"])
    assert all(entry.get("heading") is None or entry["heading"] for entry in result["items"])


async def test_filters_narrow_the_matches_but_not_the_day_totals(
    mcp: Client[Any], friday: respx.Route
) -> None:
    result = await call(mcp, "get_daily_summary", date="2026-11-20", section="1")

    assert 0 < result["matched"] < result["total_items"]
    assert {entry["section"] for entry in result["items"]} == {"1"}
    assert len(result["sections"]) == 7


async def test_a_sunday_is_not_an_error(mcp: Client[Any], sunday: respx.Route) -> None:
    result = await call(mcp, "get_daily_summary", date="2026-11-15")

    assert result["published"] is False
    assert result["note"] == (
        "No BOE issue on 2026-11-15: no publication that day, or not yet published."
    )
    assert result["items"] == []


@pytest.mark.parametrize("day", ["1960-08-31", "2999-01-01"])
async def test_a_date_outside_the_archive_is_an_invalid_argument(
    mcp: Client[Any], router: respx.MockRouter, day: str
) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*1960-09-01"):
        await call(mcp, "get_daily_summary", date=day)

    assert router.calls.call_count == 0


async def test_a_server_error_is_reported_as_unavailable(
    mcp: Client[Any], router: respx.MockRouter, xml_response: Xml
) -> None:
    router.get(f"{SUMMARY_URL}20261120").mock(return_value=xml_response(b"<response/>", 503))

    with pytest.raises(ToolError, match="unavailable, retryable"):
        await call(mcp, "get_daily_summary", date="2026-11-20")


async def test_paging_arguments_are_checked_by_the_schema(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError):
        await call(mcp, "get_daily_summary", date="2026-11-20", limit=101)


# --- scan_gazette ------------------------------------------------------------------------------


async def test_a_scan_collects_matches_newest_first_and_reports_progress(
    mcp: Client[Any],
    friday: respx.Route,
    sunday: respx.Route,
    router: respx.MockRouter,
    xml_response: Xml,
    load_fixture: Loader,
) -> None:
    router.get(f"{SUMMARY_URL}20261116").mock(
        return_value=xml_response(load_fixture("sumario_boe_20261120_trimmed.xml"), 200)
    )
    reports: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        reports.append((progress, total, message))

    result = await mcp.call_tool(
        "scan_gazette",
        {"section": "1", "from_date": "2026-11-15", "to_date": "2026-11-16", "limit": 3},
        progress_handler=on_progress,
    )

    page = ScanGazetteResult.model_validate(result.structured_content)
    assert (page.from_date.isoformat(), page.to_date.isoformat()) == ("2026-11-15", "2026-11-16")
    assert (page.days_scanned, page.days_with_issue, page.days_pending) == (2, 1, 0)
    assert (page.complete, page.days_failed, page.note) == (True, 0, None)
    assert page.matched > 3 and page.has_more and page.next_offset == 3
    assert {entry.date.isoformat() for entry in page.items} == {"2026-11-16"}
    assert [(d.date.isoformat(), d.matches) for d in page.by_day] == [("2026-11-16", page.matched)]
    assert [progress for progress, _, _ in reports] == [1, 2]


async def test_a_scan_without_any_filter_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*at least one"):
        await call(mcp, "scan_gazette", from_date="2026-11-15", to_date="2026-11-16")


async def test_a_range_over_31_days_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*31 days"):
        await call(mcp, "scan_gazette", words="ley", from_date="2026-01-01", to_date="2026-02-01")


async def test_a_day_that_fails_leaves_the_scan_incomplete_with_a_note(
    mcp: Client[Any], friday: respx.Route, router: respx.MockRouter, xml_response: Xml
) -> None:
    router.get(f"{SUMMARY_URL}20261119").mock(return_value=xml_response(b"<response/>", 503))

    result = await call(
        mcp, "scan_gazette", section="1", from_date="2026-11-19", to_date="2026-11-20"
    )

    assert (result["days_scanned"], result["days_pending"], result["complete"]) == (1, 1, False)
    assert result["note"].startswith("Scanned 1 of 2 days. Call again")
    assert "1 day could not be read and will be retried" in result["note"]
