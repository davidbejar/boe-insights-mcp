"""End-to-end checks against the real BOE API (run with ``pytest -m live``; use sparingly).

Each test calls one tool through the in-memory MCP client and asserts only stable facts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client

from boe_insights_mcp.schemas import (
    AmendmentHistoryResult,
    ArticleHistoryResult,
    CompareArticleVersionsResult,
    LawOverview,
    ReadArticleResult,
    ResolveReferenceResult,
    SearchLegislationResult,
)
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

pytestmark = pytest.mark.live

LEY_40 = "BOE-A-2015-10566"
LOPD = "BOE-A-1999-23750"


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
    # Not flagged repealed, but one later norm repeals part of it, hence partially_repealed.
    assert overview.status in {"in_force", "partially_repealed"}
    assert overview.amendments.affected_by_count > 0


async def test_live_escaped_reserved_characters_in_titles_are_accepted(mcp: Client[Any]) -> None:
    # The query builder backslash-escapes reserved characters: `titulo:40\/2015` and
    # `titulo:(Decreto\-ley AND medidas AND urgentes)`. The API must accept both (HTTP 200).
    by_number = await mcp.call_tool("search_legislation", {"title": "40/2015", "limit": 5})
    by_words = await mcp.call_tool(
        "search_legislation", {"title": "Decreto-ley medidas urgentes", "limit": 5}
    )

    assert SearchLegislationResult.model_validate(by_number.structured_content).returned >= 0
    hits = SearchLegislationResult.model_validate(by_words.structured_content)
    assert hits.returned >= 1


async def test_live_search_sorts_by_enactment_date(mcp: Client[Any]) -> None:
    result = await mcp.call_tool(
        "search_legislation",
        {"rank": "ley_organica", "sort_by": "enacted_on", "descending": False, "limit": 3},
    )

    page = SearchLegislationResult.model_validate(result.structured_content)
    assert page.items
    assert (page.items[0].rank, page.items[0].official_number) == ("Ley Orgánica", "1/1979")


async def test_live_read_article_resolves_46_bis_to_its_block(mcp: Client[Any]) -> None:
    result = await mcp.call_tool("read_article", {"law_id": LEY_40, "article": "46 bis"})

    article = ReadArticleResult.model_validate(result.structured_content)
    assert (article.outcome, article.block_id) == ("found", "a4-2")
    assert article.block_title == "Artículo 46 bis"
    assert article.text.startswith("Artículo 46 bis")


async def test_live_read_article_as_of_2017_returns_the_original_version(
    mcp: Client[Any],
) -> None:
    result = await mcp.call_tool(
        "read_article", {"law_id": LEY_40, "article": "artículo 48", "as_of": "2017-01-01"}
    )

    article = ReadArticleResult.model_validate(result.structured_content)
    assert article.version is not None
    assert article.version.introduced_by == LEY_40
    assert article.version_count >= 2
    assert article.is_latest is False
    assert article.state == "in_force"


async def test_live_compare_article_versions_lists_the_amending_laws(mcp: Client[Any]) -> None:
    result = await mcp.call_tool(
        "compare_article_versions",
        {"law_id": LEY_40, "article": "artículo 48", "from_date": "2017-01-01"},
    )

    comparison = CompareArticleVersionsResult.model_validate(result.structured_content)
    assert comparison.from_version is not None
    assert comparison.from_version.introduced_by == LEY_40
    assert comparison.identical is False
    assert comparison.amending_laws


async def test_live_get_article_history_of_article_48_has_several_versions(
    mcp: Client[Any],
) -> None:
    result = await mcp.call_tool("get_article_history", {"law_id": LEY_40, "article": "48"})

    history = ArticleHistoryResult.model_validate(result.structured_content)
    assert history.version_count >= 2
    assert history.events[0].kind == "original"
    assert history.events[0].introduced_by == LEY_40


async def test_live_get_amendment_history_of_ley_40_2015(mcp: Client[Any]) -> None:
    result = await mcp.call_tool(
        "get_amendment_history", {"law_id": LEY_40, "group": "modifies", "limit": 5}
    )

    history = AmendmentHistoryResult.model_validate(result.structured_content)
    assert history.total > 0
    assert {item.group for item in history.items} == {"modifies"}
    assert history.by_year


async def test_live_lopd_1999_is_repealed_although_the_metadata_flag_is_not_set(
    mcp: Client[Any],
) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": LOPD})

    overview = LawOverview.model_validate(result.structured_content)
    assert overview.status == "repealed"
    assert overview.initial_note is not None
    assert overview.initial_note.startswith("Norma derogada")


async def test_live_article_of_a_wholly_repealed_law_reads_as_repealed(mcp: Client[Any]) -> None:
    # Ley 30/1992 was repealed as a whole; its article 1 carries no repeal marker of its own.
    result = await mcp.call_tool("read_article", {"law_id": "BOE-A-1992-26318", "article": "1"})

    article = ReadArticleResult.model_validate(result.structured_content)
    assert article.outcome == "found"
    assert article.state == "repealed"
    assert article.law_repealed is True
    assert article.text
