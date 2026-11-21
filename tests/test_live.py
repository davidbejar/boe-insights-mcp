"""End-to-end checks against the real BOE API (run with ``pytest -m live``; use sparingly).

Each test calls one tool through the in-memory MCP client and asserts only stable facts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client

from boe_insights_mcp.schemas import LawOverview, ResolveReferenceResult, SearchLegislationResult
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

pytestmark = pytest.mark.live

LEY_40 = "BOE-A-2015-10566"


@pytest.fixture
async def mcp() -> AsyncIterator[Client[Any]]:
    settings = Settings(cache_backend="memory", min_interval=1.0)
    async with Client(create_server(settings=settings)) as client:
        yield client


async def test_live_resolve_reference_finds_ley_40_2015(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("resolve_reference", {"reference": "Ley 40/2015"})

    resolved = ResolveReferenceResult.model_validate(result.structured_content)
    assert resolved.status == "resolved"
    assert [match.identifier for match in resolved.matches] == [LEY_40]


async def test_live_search_legislation_lists_organic_laws(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("search_legislation", {"rank": "ley_organica", "limit": 3})

    page = SearchLegislationResult.model_validate(result.structured_content)
    assert 1 <= page.returned <= 3
    assert {item.rank for item in page.items} == {"Ley Orgánica"}
    assert page.provenance.attribution.startswith("Basado en datos")


async def test_live_get_law_overview_of_ley_40_2015(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": LEY_40})

    overview = LawOverview.model_validate(result.structured_content)
    assert overview.identifier == LEY_40
    assert (overview.rank, overview.official_number) == ("Ley", "40/2015")
    assert overview.status == "in_force"
    assert overview.amendments.affected_by_count > 0
