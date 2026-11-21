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
    DailySummaryResult,
    LawOverview,
    LawStructureResult,
    ReadArticleResult,
    ResolveReferenceResult,
    ScanGazetteResult,
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


async def test_live_trend_tools_share_one_catalogue_download(mcp: Client[Any]) -> None:
    # One test for all five tools: the first call downloads the whole catalogue (3 large
    # requests), the others reuse it, so the BOE is asked as little as possible.
    window = {"from_date": "2015-01-01", "to_date": "2024-12-31"}

    over_time = (
        await mcp.call_tool("legislation_over_time", {"rank": "real_decreto_ley", **window})
    ).structured_content
    assert over_time is not None
    assert over_time["coverage"]["catalogue_size"] > 10000
    assert len(over_time["series"]) == 10
    assert over_time["total"] > 50

    departments = (
        await mcp.call_tool("top_departments", {"scope": "state", **window, "limit": 3})
    ).structured_content
    assert departments is not None
    assert len(departments["departments"]) == 3

    compared = (
        await mcp.call_tool(
            "compare_topics",
            {
                "topics": [
                    {"name": "teletrabajo", "title": "teletrabajo"},
                    {"name": "datos", "title": "protección de datos"},
                ],
                **window,
            },
        )
    ).structured_content
    assert compared is not None
    assert [t["name"] for t in compared["topics"]] == ["teletrabajo", "datos"]

    spike = (await mcp.call_tool("explain_spike", {"period_key": "2020"})).structured_content
    assert spike is not None
    assert spike["found"] is True
    assert spike["count"] > 100

    amended = (
        await mcp.call_tool(
            "most_amended_laws", {"title": "protección de datos", "max_laws": 3, **window}
        )
    ).structured_content
    assert amended is not None
    assert amended["examined"] <= 3
    assert amended["candidates"] >= 1


async def test_live_law_structure_of_ley_40_2015_in_headings_mode(mcp: Client[Any]) -> None:
    # One whole-law request (about 0.7 MB) plus the metadata.
    result = await mcp.call_tool("get_law_structure", {"law_id": LEY_40, "detail": "headings"})

    page = LawStructureResult.model_validate(result.structured_content)
    assert page.entries[0].label == "TÍTULO PRELIMINAR"
    assert page.counts["article"] > 100  # counts cover the articles that headings mode leaves out
    assert {e.kind for e in page.entries} <= {
        "preamble",
        "book",
        "title",
        "chapter",
        "section",
        "subsection",
        "group",
        "annex",
        "other",
    }


async def test_live_overview_of_an_asturian_law_names_the_regional_gazette(
    mcp: Client[Any],
) -> None:
    result = await mcp.call_tool("get_law_overview", {"law_id": "BOE-A-2026-23272"})

    overview = LawOverview.model_validate(result.structured_content)
    assert overview.first_published is not None
    assert (overview.first_published.gazette, overview.first_published.number) == ("BOPA", "206")


async def test_live_daily_summary_of_the_day_of_the_state_of_alarm(mcp: Client[Any]) -> None:
    # 2020-03-14 had two issues: the extraordinary one, with Real Decreto 463/2020 alone.
    result = await mcp.call_tool(
        "get_daily_summary", {"date": "2020-03-14", "words": "estado de alarma"}
    )

    page = DailySummaryResult.model_validate(result.structured_content)
    assert page.published is True
    assert [issue.identifier for issue in page.issues] == ["BOE-S-2020-67", "BOE-S-2020-66"]
    assert "BOE-A-2020-3692" in [entry.identifier for entry in page.items]
    assert page.total_items > 100


async def test_live_scan_of_one_day_finds_the_same_decree(mcp: Client[Any]) -> None:
    result = await mcp.call_tool(
        "scan_gazette",
        {"words": "estado de alarma", "from_date": "2020-03-14", "to_date": "2020-03-14"},
    )

    page = ScanGazetteResult.model_validate(result.structured_content)
    assert (page.complete, page.days_scanned, page.days_with_issue) == (True, 1, 1)
    assert "BOE-A-2020-3692" in [entry.identifier for entry in page.items]
