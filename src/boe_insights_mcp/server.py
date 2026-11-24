"""The MCP server: tool registration, client lifecycle and error translation.

This layer is deliberately thin. Every tool calls a use case in :mod:`boe_insights_mcp.legislation`
or :mod:`boe_insights_mcp.articles` and turns client errors into messages the model can act on.

``create_server`` builds the ``FastMCP`` instance and registers the tool functions
(``search_legislation``, ``resolve_reference``, ``get_law_overview``, ``find_codes``,
``get_amendment_history``, ``read_article``, ``compare_article_versions``,
``get_article_history``, ``get_law_structure``, and the trend tools ``legislation_over_time``,
``top_departments``, ``compare_topics``, ``explain_spike`` and ``most_amended_laws``, the
gazette tools ``get_daily_summary`` and ``scan_gazette``, ``verify_citation``,
``explore_amendment_chain`` and the five saved-search tools ``save_search``,
``list_saved_searches``, ``check_saved_searches``, ``edit_saved_search`` and
``delete_saved_search``, left out when the server is open over HTTP); it also registers the
resources and prompts of ``resources`` and ``prompts``.
``main`` starts it over stdio, or over HTTP when asked (see ``launch``).
Layering: tool functions here -> use cases in ``legislation``, ``articles``, ``trends``,
``gazette``, ``citations``, ``amendment_chains`` and ``saved_searches`` -> ``client`` (and
``catalogue`` for the trend tools and the chain explorer, ``searchstore`` for saved searches).
This is also where the cache, rate limiter, client, catalogue store and saved-search store are
created, in the lifespan. The tool
docstrings, ``Field`` descriptions and ``_INSTRUCTIONS_*`` are sent to the language model, so they
are model-facing text; explanations for human readers live in ``#`` comments.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.authorization import AuthMiddleware
from mcp.types import ToolAnnotations
from pydantic import Field

from boe_insights_mcp import (
    amendment_chains,
    articles,
    gazette,
    launch,
    legislation,
    prompts,
    resources,
    saved_searches,
    trends,
)
from boe_insights_mcp import citations as citation_checks
from boe_insights_mcp.access import AllowList, allow_check
from boe_insights_mcp.analytics import Period
from boe_insights_mcp.cache import Cache, DiskCache, MemoryCache
from boe_insights_mcp.callrate import CallRateLimit
from boe_insights_mcp.catalogue import CatalogueStore
from boe_insights_mcp.chains import Direction
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.guard import guarded, guarded_for_owner
from boe_insights_mcp.legislation import (
    MAX_AMENDMENT_LIMIT,
    MAX_LIMIT,
    AmendmentFilter,
    RankFilter,
    ScopeFilter,
)
from boe_insights_mcp.login import AllowListedGitHubProvider, redirect_patterns
from boe_insights_mcp.query import SortField
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import (
    AmendmentChainResult,
    AmendmentHistoryResult,
    ArticleHistoryResult,
    CheckSearchesResult,
    CitationRequest,
    CompareArticleVersionsResult,
    CompareTopicsResult,
    DailySummaryResult,
    DeleteSearchResult,
    ExplainSpikeResult,
    FindCodesResult,
    LawOverview,
    LawStructureResult,
    LegislationOverTimeResult,
    MostAmendedLawsResult,
    ReadArticleResult,
    ResolveReferenceResult,
    SavedSearchList,
    SaveSearchResult,
    ScanGazetteResult,
    SearchLegislationResult,
    TopDepartmentsResult,
    TopicSpec,
    VerifyCitationResult,
)
from boe_insights_mcp.searchstore import SavedSearchStore
from boe_insights_mcp.settings import Settings
from boe_insights_mcp.trends import DateBasis, GroupBy, TrendFilters

__all__ = ["create_server", "main"]

_T = TypeVar("_T")

_INSTRUCTIONS_HEAD = (
    "Tools over Spanish legislation from the BOE (Boletín Oficial del Estado): the consolidated "
    "legislation (most tools) and the daily gazette (get_daily_summary and scan_gazette: "
    "everything published since 1960). Most identifiers found in the gazette are not in the "
    "consolidated collection, so the law tools will not find them. resolve_reference turns a "
    "name such as 'Ley 40/2015' into an identifier for the law tools; verify_citation takes "
    "names directly and checks whether a law and article exist and whether quoted words match "
    "the text. search_legislation explores by topic, rank, department or date; find_codes looks "
    "up the codes its filters take; get_law_overview summarises a law. read_article, "
    "compare_article_versions and get_article_history work on one article; "
    "get_amendment_history lists the norms that amended a law; get_law_structure gives its "
    "table of contents with each article's block_id. legislation_over_time, top_departments, "
    "compare_topics, explain_spike, most_amended_laws and explore_amendment_chain download the "
    "collection on first use (about 30 seconds). Repeat most_amended_laws, "
    "explore_amendment_chain and scan_gazette while complete is false. "
)
# The one sentence about saved searches, kept apart so the instructions of a server without
# saved searches are the same text minus exactly this.
_INSTRUCTIONS_SAVED_SEARCHES = (
    "Saved searches (save_search, list_saved_searches, edit_saved_search, delete_saved_search) "
    "live on the server and nothing is sent automatically: when the user asks what is new, call "
    "check_saved_searches and repeat it while complete is false. "
)
_INSTRUCTIONS_TAIL = (
    "Resources and prompts only mirror these tools. The status of a law is derived from BOE "
    "flags, so treat it as an indication. When quoting results, pass on the attribution "
    "'Basado en datos de la Agencia Estatal Boletín Oficial del Estado' "
    "(https://www.boe.es) and say that consolidated texts are informational and have no "
    "official legal value."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
# The saved-search tools are the first that write (to the server's own store, not to the BOE).
# ``save_search`` and ``check_saved_searches`` add or move state and read the BOE, so they are not
# idempotent and are open-world; edit and delete overwrite or remove, so they are destructive,
# and repeating them is harmless (idempotent); none of those two touches an external service.
_SAVES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
_LISTS = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
_CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


async def _guard(call: Awaitable[_T], tool: str) -> _T:
    """Await a use case of ``tool``, translating its expected errors into ``ToolError``."""
    # The policy (and why the server masks everything else) lives in ``guard``, shared with the
    # resources, which raise ``ResourceError`` instead.
    return await guarded(call, tool, ToolError)


def _build_cache(settings: Settings) -> Cache:
    """Create the cache backend that ``settings`` selects."""
    if settings.cache_backend == "memory":
        return MemoryCache()
    return DiskCache(settings.cache_dir)


def _lifespan(
    client: BoeClient | None,
    settings: Settings | None,
    store: SavedSearchStore | None,
    remote: bool,
) -> Any:
    """Build the server lifespan, which makes the client and the saved-search store available.

    An injected client or store is used as it is and never closed. Otherwise the cache, limiter,
    client and store are created when the server starts and closed when it stops.
    """
    # Why a lifespan: the client owns long-lived state (an HTTP connection pool, the disk cache,
    # the rate limiter's timing) that every tool call must share. Creating it per call would
    # defeat caching and rate limiting, and creating it at import time would touch the disk and
    # network before the server runs. The lifespan opens it when the server starts and closes it
    # when it stops; tools reach it through ``ctx.lifespan_context``.

    @lifespan
    async def client_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        """Provide ``{"client", "catalogue", "searches", "remote"}`` while the server runs."""
        # An injected client (tests) needs no settings, so the environment is not read.
        config = (
            (settings if settings is not None else Settings.from_env()) if client is None else None
        )
        # The saved searches are not in the cache: the cache may be deleted at any time, they may
        # not. With an injected client and no store (tests) they live in memory, so a test can
        # never write to the user's data directory.
        searches = (
            store
            if store is not None
            else SavedSearchStore.in_directory(
                config.saved_searches_dir if config is not None else None
            )
        )
        try:
            if config is None:
                # Injected (tests): the caller owns the client, so it is not closed here.
                assert client is not None
                yield {
                    "client": client,
                    "catalogue": CatalogueStore(client),
                    "searches": searches,
                    "remote": remote,
                }
                return
            cache = _build_cache(config)
            limiter = RateLimiter(config.min_interval, config.max_concurrency)
            # The BoeClient context closes its HTTP client; the cache is not part of it, so the
            # ``finally`` closes it even if the server stops with an error.
            try:
                async with BoeClient(cache=cache, limiter=limiter) as own_client:
                    # The catalogue store is created beside the client for the same reason: its
                    # in-memory snapshot (about 12 500 laws) must be shared by every tool call.
                    yield {
                        "client": own_client,
                        "catalogue": CatalogueStore(own_client),
                        "searches": searches,
                        "remote": remote,
                    }
            finally:
                await cache.close()
        finally:
            if store is None:
                await searches.close()

    return client_lifespan


def _client(ctx: Context) -> BoeClient:
    """Return the shared client that the lifespan stored in the request context."""
    return ctx.lifespan_context["client"]


def _searches(ctx: Context) -> SavedSearchStore:
    """Return the saved-search store that the lifespan stored in the request context."""
    return ctx.lifespan_context["searches"]


def _remote(ctx: Context) -> bool:
    """Whether this server runs over HTTP, so owners come from tokens and are never ``local``."""
    return ctx.lifespan_context["remote"]


def _catalogue(ctx: Context) -> CatalogueStore:
    """Return the shared catalogue store that the lifespan stored in the request context."""
    return ctx.lifespan_context["catalogue"]


# The tool functions below are plain module-level coroutines. Their docstrings, parameter
# ``Field`` descriptions and return types are what the model sees as the tool description and
# the input and output schemas, so edit those texts deliberately (see CLAUDE.md, "Documentation
# standard"). Notes for human readers:
#   - ``ctx: Context`` is injected by FastMCP and is not part of the tool's input schema.
#   - Each body only delegates to ``legislation`` through ``_guard``, which converts expected
#     failures into ``ToolError`` messages.
#   - They are registered in ``create_server`` from ``_TOOLS``, not with decorators.
async def search_legislation(
    ctx: Context,
    title: Annotated[
        str | None,
        Field(description="Words that must all appear in the title, e.g. 'protección de datos'."),
    ] = None,
    text: Annotated[
        str | None, Field(description="Words that must all appear in the law's body text.")
    ] = None,
    rank: Annotated[
        RankFilter | None, Field(description="Only laws of this rank, e.g. 'ley_organica'.")
    ] = None,
    department_code: Annotated[
        str | None,
        Field(
            description="BOE department code, e.g. '7723' (Jefatura del Estado). "
            "Look codes up with find_codes."
        ),
    ] = None,
    scope: Annotated[
        ScopeFilter,
        Field(description="'state' (national), 'regional' (autonomous communities) or 'any'."),
    ] = "any",
    subject_code: Annotated[
        str | None,
        Field(description="BOE subject (materia) code. Look codes up with find_codes."),
    ] = None,
    published_from: Annotated[
        date | None, Field(description="Earliest publication date, ISO format, e.g. 2020-01-01.")
    ] = None,
    published_to: Annotated[
        date | None, Field(description="Latest publication date, ISO format, e.g. 2020-12-31.")
    ] = None,
    sort_by: Annotated[
        SortField,
        Field(description="Sort by publication, last-update or enactment date."),
    ] = "published_on",
    descending: Annotated[bool, Field(description="Newest first when true.")] = True,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Results per page.")] = 20,
    offset: Annotated[
        int, Field(ge=0, description="Start position; pass next_offset from the previous page.")
    ] = 0,
) -> SearchLegislationResult:
    """Find laws by title, text, rank, department, scope or publication date.

    Answers 'which laws are about X?' or 'what was published recently?'. Use
    resolve_reference instead when you already know a law's name or number. Returns compact
    records. The API gives no total count: while has_more is true, request the next page
    with next_offset. Only consolidated legislation is covered, not every item published
    in the BOE. With no filters it lists the most recently published laws.
    """
    return await _guard(
        legislation.search_legislation(
            _client(ctx),
            title=title,
            text=text,
            rank=rank,
            department_code=department_code,
            scope=scope,
            subject_code=subject_code,
            published_from=published_from,
            published_to=published_to,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            offset=offset,
        ),
        "search_legislation",
    )


async def resolve_reference(
    ctx: Context,
    reference: Annotated[
        str,
        Field(
            description="A law as people name it: 'Ley 40/2015', 'Real Decreto-ley 6/2019', "
            "'LO 3/2018', 'BOE-A-2015-10566' or a title fragment. Years need four digits."
        ),
    ],
) -> ResolveReferenceResult:
    """Turn a law's name or number into its BOE identifier.

    Answers 'what is the identifier of Ley 40/2015?'. Call this before get_law_overview,
    which needs an identifier. Accepts a rank and number ('Ley Orgánica 3/2018',
    'RDL 6/2019'), a bare number, an identifier or a title fragment. Returns status
    'resolved' (one match), 'ambiguous' (candidates; follow the hint) or 'not_found'. To
    explore laws by topic rather than look up a known one, use search_legislation.
    """
    return await _guard(legislation.resolve_reference(_client(ctx), reference), "resolve_reference")


async def get_law_overview(
    ctx: Context,
    law_id: Annotated[str, Field(description="BOE identifier of the law, e.g. BOE-A-2015-10566.")],
) -> LawOverview:
    """Summarise one law: identity, legal status and how much it has been amended.

    Answers 'is this law still in force, and how much has it changed?'. Returns title, rank,
    dates, a status with a plain-English note, the BOE's initial note when it has one, and
    counts and samples of the norms that amend it or that it amends. The status is derived from
    the BOE flags (repealed, annulled, validity exhausted), from the amendment references (later
    norms that repeal it, giving 'partially_repealed') and from a repeal note opening the text,
    so it is an indication, not a legal determination: 'in_force' does not mean every provision
    is current. Get the identifier from resolve_reference.
    """
    return await _guard(legislation.get_law_overview(_client(ctx), law_id), "get_law_overview")


async def find_codes(
    ctx: Context,
    kind: Annotated[
        Literal["department", "subject", "rank"],
        Field(description="Which BOE code table to search."),
    ],
    text: Annotated[
        str,
        Field(description="Words that must all appear in the name, e.g. 'protección de datos'."),
    ],
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Maximum matches to return.")] = 20,
) -> FindCodesResult:
    """Look up BOE department, subject or rank codes by name.

    Answers 'what is the department_code of the Agencia Española de Protección de Datos?'.
    Use it to get the values for the department_code and subject_code filters of
    search_legislation. Matching ignores case and accents and needs every word; the shortest
    names come first. Subjects are a large flat list, so use specific words.
    """
    return await _guard(legislation.find_codes(_client(ctx), kind, text, limit=limit), "find_codes")


_LAW_ID = Annotated[
    str,
    Field(
        description="BOE identifier of the law, e.g. BOE-A-2015-10566. "
        "Get it from resolve_reference."
    ),
]
_ARTICLE = Annotated[
    str | None,
    Field(
        description="The article or provision designation, e.g. '46 bis', 'art. 1.902', "
        "'disposición adicional tercera'. Give this or block_id."
    ),
]
_BLOCK_ID = Annotated[
    str | None,
    Field(description="Exact block_id taken from candidates. Give this or article, not both."),
]


async def get_amendment_history(
    ctx: Context,
    law_id: _LAW_ID,
    group: Annotated[
        AmendmentFilter,
        Field(
            description="Kind of amending norm: 'repeals' (also annulments), 'modifies', 'adds', "
            "'corrections', 'other' or 'all'."
        ),
    ] = "all",
    limit: Annotated[int, Field(ge=1, le=MAX_AMENDMENT_LIMIT, description="Items per page.")] = 50,
    offset: Annotated[
        int, Field(ge=0, description="Start position; pass next_offset from the previous page.")
    ] = 0,
) -> AmendmentHistoryResult:
    """List the later norms that amended, repealed or otherwise affected a whole law.

    Answers 'which laws changed Ley 40/2015, and when?'. Get law_id from resolve_reference.
    Newest first. Filter with group (repeals, modifies, adds, corrections, other; default
    all). Returns counts per group and per year, then one page of items with law_id, year,
    relation and text; while has_more is true, pass next_offset as offset. It names the norms,
    not the articles they touched: for one article use get_article_history. Derived from BOE
    references, so informational.
    """
    return await _guard(
        legislation.get_amendment_history(_client(ctx), law_id, group, limit, offset),
        "get_amendment_history",
    )


async def read_article(
    ctx: Context,
    law_id: _LAW_ID,
    article: _ARTICLE = None,
    block_id: _BLOCK_ID = None,
    as_of: Annotated[
        date | None,
        Field(
            description="Show the text valid on this date, ISO format, e.g. 2017-01-01. "
            "Default: today."
        ),
    ] = None,
    max_chars: Annotated[
        int,
        Field(
            ge=articles.MIN_PAGE_CHARS,
            le=articles.MAX_PAGE_CHARS,
            description="Largest part of the text to return.",
        ),
    ] = articles.DEFAULT_PAGE_CHARS,
    offset: Annotated[
        int,
        Field(
            ge=0,
            description="Start character; pass next_offset from the previous part, with the "
            "same other arguments.",
        ),
    ] = 0,
) -> ReadArticleResult:
    """Read the text of one article of a law as in force on a date.

    Answers 'what does article 46 bis of Ley 40/2015 say, today or on 2017-01-01?'. Get law_id
    from resolve_reference. If several blocks match, or none, outcome is 'ambiguous' or
    'not_found' and candidates lists blocks to retry with as block_id. When the whole law is
    repealed, state is 'repealed' although the article shows no marker. Long texts come in
    parts: while truncated is true, pass next_offset as offset. For changes over time use
    compare_article_versions or get_article_history. Consolidated text is informational, not
    official.
    """
    return await _guard(
        articles.read_article(
            _client(ctx),
            law_id,
            article,
            block_id=block_id,
            as_of=as_of,
            max_chars=max_chars,
            offset=offset,
        ),
        "read_article",
    )


async def compare_article_versions(
    ctx: Context,
    law_id: _LAW_ID,
    from_date: Annotated[date, Field(description="Earlier date, ISO format, e.g. 2017-01-01.")],
    article: _ARTICLE = None,
    block_id: _BLOCK_ID = None,
    to_date: Annotated[
        date | None, Field(description="Later date, ISO format. Default: today.")
    ] = None,
    max_changes: Annotated[
        int,
        Field(ge=1, le=articles.MAX_CHANGES, description="Most changed paragraphs to list."),
    ] = articles.DEFAULT_CHANGES,
    max_chars: Annotated[
        int,
        Field(
            ge=articles.MIN_COMPARE_CHARS,
            le=articles.MAX_COMPARE_CHARS,
            description="Largest total text of the listed changes.",
        ),
    ] = articles.DEFAULT_COMPARE_CHARS,
) -> CompareArticleVersionsResult:
    """Show how one article's text differs between two dates.

    Answers 'what changed in article 48 between 2017-01-01 and today?'. Returns the added,
    removed and reworded paragraphs (rewordings marked [-old-] {+new+}; tables give both
    texts), a unified diff and the norms that amended the article in between. If the article
    did not exist on from_date, everything shows as added. If the whole law was repealed, note
    says so. Output is capped: see changes_truncated and unified_truncated, and raise
    max_changes or max_chars for more. To list every amendment use get_article_history.
    Consolidated text is informational, not official.
    """
    return await _guard(
        articles.compare_article_versions(
            _client(ctx),
            law_id,
            article,
            from_date,
            to_date,
            max_changes,
            max_chars,
            block_id=block_id,
        ),
        "compare_article_versions",
    )


async def get_article_history(
    ctx: Context, law_id: _LAW_ID, article: _ARTICLE = None, block_id: _BLOCK_ID = None
) -> ArticleHistoryResult:
    """List the versions of one article: when each took effect, who amended it, what changed.

    Answers 'when and by which laws was article 48 amended?'. Each event is original, amended
    or repealed, with the paragraphs added, removed and changed against the previous version. To
    see what changed between two dates use compare_article_versions; for the amendments of the
    whole law, not one article, use get_amendment_history. At most the 60 most recent versions
    are returned; truncated says when older ones are left out. current_state is 'repealed' also
    when the whole law is repealed. Informational, not official.
    """
    return await _guard(
        articles.get_article_history(_client(ctx), law_id, article, block_id=block_id),
        "get_article_history",
    )


_TITLE_FILTER = Annotated[
    str | None,
    Field(
        description="Words that must all appear in the title (whole words; singular and plural "
        "forms both match), e.g. 'teletrabajo'."
    ),
]
_TEXT_FILTER = Annotated[
    str | None,
    Field(description="Words that must all appear in the law's body text (exact words)."),
]
_SUBJECT_FILTER = Annotated[
    str | None,
    Field(description="BOE subject (materia) code. Look codes up with find_codes."),
]
_RANK_FILTER = Annotated[
    RankFilter | None, Field(description="Only laws of this rank, e.g. 'real_decreto_ley'.")
]
_SCOPE_FILTER = Annotated[
    ScopeFilter,
    Field(description="'state' (national), 'regional' (autonomous communities) or 'any'."),
]
_DEPARTMENT_FILTER = Annotated[
    str | None,
    Field(description="BOE department code. Look codes up with find_codes."),
]
_FROM_DATE = Annotated[
    date | None,
    Field(
        description="Start of the range, ISO format. Default: 1 January of the year 19 years "
        "before to_date."
    ),
]
_TO_DATE = Annotated[
    date | None, Field(description="End of the range, ISO format. Default: today.")
]
_PERIOD = Annotated[Period, Field(description="Period size: 'year', 'quarter' or 'month'.")]
_DATE_BASIS = Annotated[
    DateBasis,
    Field(description="Date to count by: 'published' (BOE publication) or 'enacted'."),
]


async def legislation_over_time(
    ctx: Context,
    title: _TITLE_FILTER = None,
    text: _TEXT_FILTER = None,
    subject_code: _SUBJECT_FILTER = None,
    rank: _RANK_FILTER = None,
    scope: _SCOPE_FILTER = "any",
    department_code: _DEPARTMENT_FILTER = None,
    from_date: _FROM_DATE = None,
    to_date: _TO_DATE = None,
    period: _PERIOD = "year",
    date_basis: _DATE_BASIS = "published",
    group_by: Annotated[
        GroupBy,
        Field(description="Split by 'rank', 'department' or 'scope'; 'none' gives one series."),
    ] = "none",
    top_groups: Annotated[
        int,
        Field(ge=1, le=trends.MAX_TOP_GROUPS, description="Groups to list; the rest is 'other'."),
    ] = 5,
) -> LegislationOverTimeResult:
    """Count laws per year, quarter or month, optionally split by rank, department or scope.

    Answers 'how much legislation on X over time?'. Counts consolidated legislation only.
    """
    return await _guard(
        trends.legislation_over_time(
            _catalogue(ctx),
            TrendFilters(title, text, subject_code, rank, scope, department_code),
            from_date=from_date,
            to_date=to_date,
            period=period,
            date_basis=date_basis,
            group_by=group_by,
            top_groups=top_groups,
            progress=ctx.report_progress,
        ),
        "legislation_over_time",
    )


async def top_departments(
    ctx: Context,
    title: _TITLE_FILTER = None,
    text: _TEXT_FILTER = None,
    subject_code: _SUBJECT_FILTER = None,
    rank: _RANK_FILTER = None,
    scope: _SCOPE_FILTER = "any",
    from_date: _FROM_DATE = None,
    to_date: _TO_DATE = None,
    date_basis: _DATE_BASIS = "published",
    limit: Annotated[
        int, Field(ge=1, le=trends.MAX_DEPARTMENTS, description="Departments to list.")
    ] = 10,
) -> TopDepartmentsResult:
    """Rank the departments (ministries, Jefatura del Estado, autonomous communities) by laws.

    Counts laws in a period. Answers 'who legislates most on X?'.
    """
    return await _guard(
        trends.top_departments(
            _catalogue(ctx),
            TrendFilters(title, text, subject_code, rank, scope),
            from_date=from_date,
            to_date=to_date,
            date_basis=date_basis,
            limit=limit,
            progress=ctx.report_progress,
        ),
        "top_departments",
    )


async def compare_topics(
    ctx: Context,
    topics: Annotated[
        list[TopicSpec],
        Field(
            min_length=trends.MIN_TOPICS,
            max_length=trends.MAX_TOPICS,
            description="2 to 5 topics, each with a name and at least one of title, text or "
            "subject_code.",
        ),
    ],
    rank: _RANK_FILTER = None,
    scope: _SCOPE_FILTER = "any",
    department_code: _DEPARTMENT_FILTER = None,
    from_date: _FROM_DATE = None,
    to_date: _TO_DATE = None,
    period: _PERIOD = "year",
    date_basis: _DATE_BASIS = "published",
) -> CompareTopicsResult:
    """Compare how often several topics appear in legislation over time.

    Each topic is title words, body words or a subject code.
    """
    return await _guard(
        trends.compare_topics(
            _catalogue(ctx),
            topics,
            rank=rank,
            scope=scope,
            department_code=department_code,
            from_date=from_date,
            to_date=to_date,
            period=period,
            date_basis=date_basis,
            progress=ctx.report_progress,
        ),
        "compare_topics",
    )


async def explain_spike(
    ctx: Context,
    title: _TITLE_FILTER = None,
    text: _TEXT_FILTER = None,
    subject_code: _SUBJECT_FILTER = None,
    rank: _RANK_FILTER = None,
    scope: _SCOPE_FILTER = "any",
    department_code: _DEPARTMENT_FILTER = None,
    period_key: Annotated[
        str | None,
        Field(
            description="Period to explain: '2020', '2020-Q2' or '2020-03'. "
            "Default: the strongest spike in the range."
        ),
    ] = None,
    period: Annotated[
        Period | None,
        Field(
            description="'year', 'quarter' or 'month'. Default: what period_key shows, else 'year'."
        ),
    ] = None,
    from_date: _FROM_DATE = None,
    to_date: _TO_DATE = None,
    date_basis: _DATE_BASIS = "published",
    baseline_periods: Annotated[
        int,
        Field(
            ge=trends.MIN_BASELINE_PERIODS,
            le=trends.MAX_BASELINE_PERIODS,
            description="Preceding periods to compare with.",
        ),
    ] = 5,
    max_laws: Annotated[
        int, Field(ge=1, le=trends.MAX_EXPLAINED_LAWS, description="Laws to list.")
    ] = 15,
) -> ExplainSpikeResult:
    """Explain why a period has unusually many laws.

    Shows which ranks, departments and scopes grew against the preceding periods, with the laws
    themselves. Without period_key it picks the strongest spike in the range.
    """
    return await _guard(
        trends.explain_spike(
            _catalogue(ctx),
            TrendFilters(title, text, subject_code, rank, scope, department_code),
            period_key=period_key,
            period=period,
            from_date=from_date,
            to_date=to_date,
            date_basis=date_basis,
            baseline_periods=baseline_periods,
            max_laws=max_laws,
            progress=ctx.report_progress,
        ),
        "explain_spike",
    )


async def most_amended_laws(
    ctx: Context,
    title: _TITLE_FILTER = None,
    text: _TEXT_FILTER = None,
    subject_code: _SUBJECT_FILTER = None,
    rank: _RANK_FILTER = None,
    scope: _SCOPE_FILTER = "any",
    department_code: _DEPARTMENT_FILTER = None,
    from_date: Annotated[
        date | None,
        Field(description="Only laws published on or after this date, ISO format."),
    ] = None,
    to_date: Annotated[
        date | None,
        Field(description="Only laws published on or before this date, ISO format."),
    ] = None,
    date_basis: _DATE_BASIS = "published",
    limit: Annotated[int, Field(ge=1, le=trends.MAX_AMENDED, description="Laws to return.")] = 10,
    max_laws: Annotated[
        int,
        Field(
            ge=1,
            le=trends.MAX_EXAMINED,
            description="Most laws to examine in this call (one request each).",
        ),
    ] = 30,
) -> MostAmendedLawsResult:
    """Rank laws by how many later laws amended them, within a filtered set.

    Examines a limited number of laws per call; when complete is false, call again to continue.
    """
    return await _guard(
        trends.most_amended_laws(
            _catalogue(ctx),
            _client(ctx),
            TrendFilters(title, text, subject_code, rank, scope, department_code),
            from_date=from_date,
            to_date=to_date,
            date_basis=date_basis,
            limit=limit,
            max_laws=max_laws,
            progress=ctx.report_progress,
        ),
        "most_amended_laws",
    )


async def get_law_structure(
    ctx: Context,
    law_id: _LAW_ID,
    as_of: Annotated[
        date | None,
        Field(
            description="Show the structure valid on this date, ISO format, e.g. 2017-01-01. "
            "Default: today."
        ),
    ] = None,
    detail: Annotated[
        Literal["headings", "all"],
        Field(description="'headings' leaves out the articles and provisions."),
    ] = "all",
    offset: Annotated[
        int, Field(ge=0, description="Start position; pass next_offset from the previous page.")
    ] = 0,
    limit: Annotated[
        int, Field(ge=1, le=articles.MAX_STRUCTURE_LIMIT, description="Entries per page.")
    ] = articles.DEFAULT_STRUCTURE_LIMIT,
) -> LawStructureResult:
    """Table of contents of a law: books, titles, chapters, sections and article headings, each
    with the block_id to pass to the article tools. Use it to find which article deals with a
    matter before reading it. Large codes need paging or detail='headings'.
    """
    return await _guard(
        articles.get_law_structure(
            _client(ctx), law_id, as_of=as_of, detail=detail, offset=offset, limit=limit
        ),
        "get_law_structure",
    )


async def get_daily_summary(
    ctx: Context,
    date: Annotated[
        date | None,
        Field(description="Day to show, ISO format, e.g. 2020-03-14. Default: the latest issue."),
    ] = None,
    section: Annotated[
        str | None, Field(description="Only this section code, e.g. '1', '2A', '3', '5B'.")
    ] = None,
    department_code: Annotated[
        str | None,
        Field(description="Only this department code. Look codes up with find_codes."),
    ] = None,
    words: Annotated[
        str | None,
        Field(
            description="Words that must all appear in the title (whole words; singular and "
            "plural forms both match)."
        ),
    ] = None,
    offset: Annotated[
        int, Field(ge=0, description="Start position; pass next_offset from the previous page.")
    ] = 0,
    limit: Annotated[
        int, Field(ge=1, le=gazette.MAX_LIMIT, description="Items per page.")
    ] = gazette.DEFAULT_LIMIT,
) -> DailySummaryResult:
    """Everything the BOE published on one day: dispositions, appointments, grants and
    announcements, by section and department. Covers the whole gazette since 1960-09-01, not just
    consolidated laws. Without a date it gives the latest issue.
    """
    # The parameter is called ``date`` because that is the name the model sees; the use case
    # calls it ``day`` so that it does not hide the ``date`` type.
    return await _guard(
        gazette.daily_summary(
            _client(ctx),
            day=date,
            section=section,
            department_code=department_code,
            words=words,
            offset=offset,
            limit=limit,
        ),
        "get_daily_summary",
    )


async def scan_gazette(
    ctx: Context,
    words: Annotated[
        str | None,
        Field(
            description="Words that must all appear in the title (whole words; singular and "
            "plural forms both match)."
        ),
    ] = None,
    section: Annotated[
        str | None, Field(description="Only this section code, e.g. '1', '2A', '3', '5B'.")
    ] = None,
    department_code: Annotated[
        str | None,
        Field(description="Only this department code. Look codes up with find_codes."),
    ] = None,
    from_date: Annotated[
        date | None,
        Field(description="First day, ISO format. Default: 6 days before to_date."),
    ] = None,
    to_date: Annotated[
        date | None, Field(description="Last day, ISO format. Default: today.")
    ] = None,
    offset: Annotated[
        int, Field(ge=0, description="Start position; pass next_offset from the previous page.")
    ] = 0,
    limit: Annotated[
        int, Field(ge=1, le=gazette.MAX_LIMIT, description="Items per page.")
    ] = gazette.DEFAULT_LIMIT,
) -> ScanGazetteResult:
    """Find items published in the BOE over a range of up to 31 days whose title has given
    words, or that belong to a section or department. Matches titles only, whole words; singular
    and plural forms both match. When complete is false, call again to continue.
    """
    return await _guard(
        gazette.scan_gazette(
            _client(ctx),
            words=words,
            section=section,
            department_code=department_code,
            from_date=from_date,
            to_date=to_date,
            offset=offset,
            limit=limit,
            progress=ctx.report_progress,
        ),
        "scan_gazette",
    )


async def verify_citation(
    ctx: Context,
    citations: Annotated[
        list[CitationRequest],
        Field(
            min_length=1,
            max_length=citation_checks.MAX_CITATIONS,
            description="1 to 10 citations to check.",
        ),
    ],
) -> VerifyCitationResult:
    """Check citations of Spanish laws against the consolidated text: whether the law and
    article exist, their state on a date, and whether quoted words match the text, the current
    version or an older one. Reports facts, not legal correctness. Compares words, not meaning: a
    paraphrase does not match.
    """
    return await _guard(
        citation_checks.verify_citations(_client(ctx), citations), "verify_citation"
    )


async def explore_amendment_chain(
    ctx: Context,
    law_id: _LAW_ID,
    direction: Annotated[
        Direction,
        Field(description="'amended_by': the laws that amended it; 'amends': the laws it amended."),
    ] = "amended_by",
    depth: Annotated[
        int, Field(ge=1, le=3, description="Levels to follow: 1 is only the direct amendments.")
    ] = amendment_chains.DEFAULT_DEPTH,
    include: Annotated[
        amendment_chains.Include,
        Field(
            description="'amendments' (modifications, repeals, additions, suspensions) or 'all', "
            "which adds corrections, citations and judicial references."
        ),
    ] = "amendments",
    details: Annotated[
        amendment_chains.Details,
        Field(
            description="Which edges carry the BOE's reference text: 'root' (those touching the "
            "starting law), 'all' or 'none'."
        ),
    ] = "root",
    max_laws: Annotated[
        int,
        Field(
            ge=1,
            le=amendment_chains.MAX_LAWS,
            description="Most laws to read in this call (one request each).",
        ),
    ] = amendment_chains.DEFAULT_MAX_LAWS,
) -> AmendmentChainResult:
    """Follow amendments between laws for up to three levels: the laws that amended a law and
    the laws that amended those (amended_by), or what a law amended (amends). A law two levels
    away amended the intermediate law, not necessarily the parts that touch the starting law.
    Examines a limited number of laws per call; when complete is false, call again to continue.
    """
    return await _guard(
        amendment_chains.explore_amendment_chain(
            _catalogue(ctx),
            _client(ctx),
            law_id,
            direction=direction,
            depth=depth,
            include=include,
            details=details,
            max_laws=max_laws,
            progress=ctx.report_progress,
        ),
        "explore_amendment_chain",
    )


async def save_search(
    ctx: Context,
    name: Annotated[
        str | None,
        Field(description="Name for the search; default: one built from its filters."),
    ] = None,
    words: Annotated[
        str | None,
        Field(
            description="Words that must all appear in the title (whole words; singular and "
            "plural forms both match)."
        ),
    ] = None,
    section: Annotated[
        str | None, Field(description="Only this section code, e.g. '1', '2A', '3', '5B'.")
    ] = None,
    department_code: Annotated[
        str | None,
        Field(description="Only this department code. Look codes up with find_codes."),
    ] = None,
    law_id: Annotated[
        str | None,
        Field(
            description="BOE identifier of a consolidated law to follow, e.g. BOE-A-2015-10566 "
            "(use resolve_reference)."
        ),
    ] = None,
) -> SaveSearchResult:
    """Save a search to re-check later: gazette items by title words, section or department, or
    one law to follow for amendments and court rulings. Nothing is sent automatically; call
    check_saved_searches to see what is new. A name is proposed when none is given.
    """
    # The owner comes from the authenticated request and is never an argument: a model (or
    # anyone steering it) must not be able to name another person's searches.
    return await guarded_for_owner(
        lambda who: saved_searches.save_search(
            _searches(ctx),
            _client(ctx),
            who,
            name=name,
            words=words,
            section=section,
            department_code=department_code,
            law_id=law_id,
        ),
        "save_search",
        ToolError,
        remote=_remote(ctx),
    )


async def list_saved_searches(ctx: Context) -> SavedSearchList:
    """The saved searches, with their filters and when each was last checked."""
    return await guarded_for_owner(
        lambda who: saved_searches.list_saved_searches(_searches(ctx), who),
        "list_saved_searches",
        ToolError,
        remote=_remote(ctx),
    )


async def check_saved_searches(
    ctx: Context,
    name: Annotated[
        str | None, Field(description="Check only this saved search; default: all.")
    ] = None,
    since: Annotated[
        date | None,
        Field(
            description="Re-read gazette searches from this date (at most 90 days back) without "
            "marking anything as seen."
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=saved_searches.MAX_ITEMS,
            description="New items or changes shown per search per call; the rest stay new.",
        ),
    ] = saved_searches.DEFAULT_ITEMS,
) -> CheckSearchesResult:
    """What is new for the saved searches since each was last checked; what is shown is marked
    as seen. Covers at most the last 31 days; shows up to 60 items per call, so call again while
    complete is false. since re-reads gazette searches up to 90 days back without marking
    anything.
    """
    return await guarded_for_owner(
        lambda who: saved_searches.check_saved_searches(
            _searches(ctx),
            _client(ctx),
            who,
            name=name,
            since=since,
            limit=limit,
            progress=ctx.report_progress,
        ),
        "check_saved_searches",
        ToolError,
        remote=_remote(ctx),
    )


async def edit_saved_search(
    ctx: Context,
    name: Annotated[str, Field(description="Name of the saved search (see list_saved_searches).")],
    new_name: Annotated[str | None, Field(description="New name for the search.")] = None,
    words: Annotated[
        str | None,
        Field(description="New value; an empty string removes this filter."),
    ] = None,
    section: Annotated[
        str | None,
        Field(description="New value; an empty string removes this filter."),
    ] = None,
    department_code: Annotated[
        str | None,
        Field(description="New value; an empty string removes this filter."),
    ] = None,
) -> SaveSearchResult:
    """Rename a saved search or change its gazette filters. Its last-checked point is kept."""
    return await guarded_for_owner(
        lambda who: saved_searches.edit_saved_search(
            _searches(ctx),
            who,
            name,
            new_name=new_name,
            words=words,
            section=section,
            department_code=department_code,
        ),
        "edit_saved_search",
        ToolError,
        remote=_remote(ctx),
    )


async def delete_saved_search(
    ctx: Context,
    name: Annotated[str, Field(description="Name of the saved search (see list_saved_searches).")],
) -> DeleteSearchResult:
    """Delete a saved search. This cannot be undone."""
    return await guarded_for_owner(
        lambda who: saved_searches.delete_saved_search(_searches(ctx), who, name),
        "delete_saved_search",
        ToolError,
        remote=_remote(ctx),
    )


# (function, display title). Registered by ``create_server`` in this order.
_TOOLS: tuple[tuple[Callable[..., Awaitable[Any]], str], ...] = (
    (search_legislation, "Search Spanish legislation"),
    (resolve_reference, "Resolve a law reference"),
    (get_law_overview, "Get a law overview"),
    (find_codes, "Find BOE codes"),
    (get_amendment_history, "List the amendments of a law"),
    (read_article, "Read an article"),
    (compare_article_versions, "Compare article versions"),
    (get_article_history, "Get an article's history"),
    (get_law_structure, "Get a law's table of contents"),
    (legislation_over_time, "Count laws over time"),
    (top_departments, "Rank departments by laws"),
    (compare_topics, "Compare topics over time"),
    (explain_spike, "Explain a spike in legislation"),
    (most_amended_laws, "Find the most amended laws"),
    (get_daily_summary, "Get a day's BOE summary"),
    (scan_gazette, "Scan the BOE over several days"),
    (verify_citation, "Verify citations of laws"),
    (explore_amendment_chain, "Explore an amendment chain"),
)

# Registered after ``_TOOLS``, and only in a mode that knows who the caller is (see ``_Mode``).
_SAVED_SEARCH_TOOLS: tuple[tuple[Callable[..., Awaitable[Any]], str], ...] = (
    (save_search, "Save a search"),
    (list_saved_searches, "List saved searches"),
    (check_saved_searches, "Check saved searches for news"),
    (edit_saved_search, "Edit a saved search"),
    (delete_saved_search, "Delete a saved search"),
)

# Every tool is ``_READ_ONLY`` unless it is listed here: only the saved-search tools differ.
_ANNOTATIONS: dict[Callable[..., Awaitable[Any]], ToolAnnotations] = {
    save_search: _SAVES,
    list_saved_searches: _LISTS,
    check_saved_searches: _SAVES,
    edit_saved_search: _CHANGES,
    delete_saved_search: _CHANGES,
}

# FastMCP's default is ["user"], which grants write access to the profile. This server needs only
# the numeric id and the login that GitHub's /user returns for any valid token, public profile
# included, so nothing is required of tokens: GitHubTokenVerifier skips its scope check when none
# is required (fastmcp/server/auth/providers/github.py:236) and the OAuth proxy sends no ``scope``
# to GitHub for a client that sent none (oauth_proxy/proxy.py:953, 1253). Clients that ask for a
# scope anyway are served by advertising ``read:user`` (below), read-only profile access, through
# ``AllowListedGitHubProvider``: metadata and the 401 challenge name it, registration and
# /authorize accept it and then ask GitHub for exactly that. ``user`` and ``repo`` stay refused.
# Observed in ``tests/test_login_door.py``; only a real sign-in confirms that GitHub accepts a
# sign-in with no scope and with ``read:user`` and that /user answers for both.
_GITHUB_SCOPES: list[str] = []
_GITHUB_ADVERTISED_SCOPES: list[str] = ["read:user"]
# How long FastMCP remembers that a token passed GitHub's check, so that a burst of calls does
# not repeat its two requests to api.github.com (the account of a revoked token stays usable
# for at most this long).
_GITHUB_CACHE_SECONDS = 300


@dataclass(frozen=True)
class _Mode:
    """How the server is used, decided once, when it is built; everything else follows.

    * local (stdio): one person, every row owned by ``local``, no authentication;
    * open HTTP: anyone who can reach the port, so there is no one to own a saved search and
      the saved-search tools, resource and instructions are left out;
    * signed-in HTTP: a verified GitHub user on the allow-list, rows owned by their id.
    """

    remote: bool
    """Served over HTTP, signed in or not: owners must come from a token, never ``local``."""
    signed_in: bool
    """HTTP with an authentication provider in effect."""

    @classmethod
    def of(cls, settings: Settings | None, auth: AuthProvider | None) -> _Mode:
        """The mode of a server with these settings and authentication provider."""
        remote = settings is not None and settings.transport == "http"
        # An authentication provider means nothing over stdio, which has no HTTP layer.
        return cls(remote=remote, signed_in=remote and auth is not None)

    @property
    def saved_searches(self) -> bool:
        """Whether saved searches exist: they need to know whose they are."""
        return not self.remote or self.signed_in


def create_server(
    *,
    client: BoeClient | None = None,
    settings: Settings | None = None,
    store: SavedSearchStore | None = None,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Build the MCP server without touching the network or the disk.

    With no ``client``, the cache, rate limiter and client are created from ``settings`` (or
    the environment) when the server starts. An injected ``client`` or ``store`` is used as it
    is and left open; with an injected client and no store, saved searches live in memory.
    ``settings`` (stdio when ``None``) and ``auth`` choose the mode (see ``_Mode``); with ``auth``
    over HTTP, only the users of ``settings.allowed_users`` may use anything.
    """
    mode = _Mode.of(settings, auth)
    # ``mask_error_details=True``: see ``_guard``.
    server = FastMCP(
        "boe-insights",
        instructions=_INSTRUCTIONS_HEAD
        + (_INSTRUCTIONS_SAVED_SEARCHES if mode.saved_searches else "")
        + _INSTRUCTIONS_TAIL,
        lifespan=_lifespan(client, settings, store, mode.remote),
        mask_error_details=True,
        auth=auth if mode.signed_in else None,
    )
    if mode.remote and settings is not None and settings.calls_per_minute > 0:
        # First, so it is the outermost: callers the allow-list refuses are counted too, and a
        # refused call costs the allow-list nothing. Signed in, one bucket per GitHub id; open,
        # one for everybody.
        server.add_middleware(
            CallRateLimit(settings.calls_per_minute, settings.calls_burst, by_user=mode.signed_in)
        )
    if mode.signed_in:
        assert settings is not None  # signed_in implies HTTP settings
        # The provider only proves who the caller is; this decides whether they may use the
        # server. It applies to every tool, resource and prompt, on listing and on use.
        server.add_middleware(
            AuthMiddleware(auth=allow_check(AllowList.parse(settings.allowed_users)))
        )
    for function, title in (*_TOOLS, *(_SAVED_SEARCH_TOOLS if mode.saved_searches else ())):
        server.tool(function, title=title, annotations=_ANNOTATIONS.get(function, _READ_ONLY))
    launch.register_health(server)
    resources.register(server, with_saved_searches=mode.saved_searches)
    prompts.register(server, with_saved_searches=mode.saved_searches)
    return server


def main(argv: Sequence[str] | None = None) -> None:
    """Console-script entry point: stdio by default, HTTP with ``--transport http``."""
    launch.run(sys.argv[1:] if argv is None else argv, os.environ, _build)


def _build(settings: Settings) -> FastMCP:
    """Build the server for ``settings``, with GitHub login when HTTP and login are configured."""
    auth: AllowListedGitHubProvider | None = None
    if settings.transport == "http" and settings.login_enabled:
        client_id, client_secret = settings.github_client_id, settings.github_client_secret
        signing_key, base_url = settings.auth_signing_key, settings.base_url
        assert client_id and client_secret and signing_key and base_url  # login_enabled
        auth = AllowListedGitHubProvider(
            allow_list=AllowList.parse(settings.allowed_users),
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            jwt_signing_key=signing_key,
            required_scopes=_GITHUB_SCOPES,
            advertised_scopes=_GITHUB_ADVERTISED_SCOPES,
            cache_ttl_seconds=_GITHUB_CACHE_SECONDS,
            # Without this FastMCP accepts any redirect URI at registration and at /authorize
            # (fastmcp/server/auth/redirect_validation.py:536-573, ``None`` means "any safe
            # scheme"), so a page of the attacker's choosing could be handed a sign-in code.
            # Loopback plus what the operator names; see ``login.redirect_patterns``.
            allowed_client_redirect_uris=redirect_patterns(settings.allowed_redirect_uris),
        )
    return create_server(settings=settings, auth=auth)
