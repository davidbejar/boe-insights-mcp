"""The MCP server: tool registration, client lifecycle and error translation.

This layer is deliberately thin. Every tool calls a use case in :mod:`boe_insights_mcp.legislation`
or :mod:`boe_insights_mcp.articles` and turns client errors into messages the model can act on.

``create_server`` builds the ``FastMCP`` instance and registers the tool functions
(``search_legislation``, ``resolve_reference``, ``get_law_overview``, ``find_codes``,
``get_amendment_history``, ``read_article``, ``compare_article_versions``,
``get_article_history``, ``get_law_structure``, and the trend tools ``legislation_over_time``,
``top_departments``, ``compare_topics``, ``explain_spike`` and ``most_amended_laws``, the
gazette tools ``get_daily_summary`` and ``scan_gazette``, ``verify_citation`` and
``explore_amendment_chain``); ``main`` runs it over stdio.
Layering: tool functions here -> use cases in ``legislation``, ``articles``, ``trends``,
``gazette``, ``citations`` and ``amendment_chains`` -> ``client`` (and ``catalogue`` for the
trend tools and the chain explorer).
This is also where the cache, rate limiter, client and catalogue store are created, in the
lifespan. The tool
docstrings, ``Field`` descriptions and ``_INSTRUCTIONS`` are sent to the language model, so they
are model-facing text; explanations for human readers live in ``#`` comments.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from boe_insights_mcp import (
    amendment_chains,
    articles,
    gazette,
    legislation,
    trends,
)
from boe_insights_mcp import citations as citation_checks
from boe_insights_mcp.analytics import Period
from boe_insights_mcp.cache import Cache, DiskCache, MemoryCache
from boe_insights_mcp.catalogue import CatalogueStore
from boe_insights_mcp.chains import Direction
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.errors import BoeError
from boe_insights_mcp.legislation import (
    MAX_AMENDMENT_LIMIT,
    MAX_LIMIT,
    AmendmentFilter,
    RankFilter,
    ScopeFilter,
)
from boe_insights_mcp.query import SortField
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import (
    AmendmentChainResult,
    AmendmentHistoryResult,
    ArticleHistoryResult,
    CitationRequest,
    CompareArticleVersionsResult,
    CompareTopicsResult,
    DailySummaryResult,
    ExplainSpikeResult,
    FindCodesResult,
    LawOverview,
    LawStructureResult,
    LegislationOverTimeResult,
    MostAmendedLawsResult,
    ReadArticleResult,
    ResolveReferenceResult,
    ScanGazetteResult,
    SearchLegislationResult,
    TopDepartmentsResult,
    TopicSpec,
    VerifyCitationResult,
)
from boe_insights_mcp.settings import Settings
from boe_insights_mcp.trends import DateBasis, GroupBy, TrendFilters

__all__ = ["create_server", "main"]

_T = TypeVar("_T")

_INSTRUCTIONS = (
    "Tools over Spanish legislation from the BOE (Boletín Oficial del Estado) consolidated "
    "legislation open data. Call resolve_reference first to turn a name such as 'Ley 40/2015' "
    "into a BOE identifier, then use that identifier with the other tools. Use "
    "search_legislation to explore by topic, rank, department or date; find_codes looks up the "
    "department, subject and rank codes its filters take. read_article gives an article's text "
    "on a date, compare_article_versions shows what changed between two dates and "
    "get_article_history lists the amendments of one article; get_amendment_history lists the "
    "norms that amended a whole law; get_law_structure gives a law's table of contents with the "
    "block_id of each article. legislation_over_time, top_departments, compare_topics, "
    "explain_spike and most_amended_laws count laws from a downloaded copy of the collection "
    "(the first call can take about 30 seconds); most_amended_laws examines a limited number of "
    "laws per call, so repeat it while complete is false. The status of a law is derived from "
    "BOE flags and amendment references, so treat it as an indication. get_daily_summary and "
    "scan_gazette read the daily BOE issues (everything published, since 1960), which the other "
    "tools do not cover. verify_citation checks whether a law and article exist and whether "
    "quoted words match the text. explore_amendment_chain follows amendments between laws for "
    "several levels; repeat it while complete is false. When quoting results, "
    "pass on the attribution 'Basado en datos de la Agencia Estatal Boletín Oficial del "
    "Estado' (https://www.boe.es) and say that consolidated texts are informational and have "
    "no official legal value."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Tool error messages are assembled from these fixed texts, keyed by ``BoeError.code``. The API's
# own message is deliberately not included: it is Spanish, and can leak internals such as the
# index name ``TITULOS`` (api-notes 2, "Errors"). Only what to *do* about it is told to the model.
_ERROR_SUMMARIES = {
    "not_found": "BOE has no law with that identifier",
    "invalid_query": "BOE rejected the search",
    "unavailable": "BOE API unavailable",
    "bad_response": "BOE returned an unexpected response",
}
_ERROR_ADVICE = {
    "not_found": "Use resolve_reference to find the correct identifier.",
    "unavailable": "Wait a few seconds and try again.",
    "bad_response": "Try again later; if it persists, the BOE API may have changed.",
}
# What to do when the BOE rejects a request as invalid depends on what the tool sent.
_INVALID_QUERY_ADVICE = {
    "search_legislation": "Simplify the search terms or remove filters, then try again.",
    "resolve_reference": "Check the reference: use a form such as 'Ley 40/2015' or an "
    "identifier, or search by title with search_legislation.",
    "get_law_overview": "Check the identifier (for example BOE-A-2015-10566) or get it again "
    "from resolve_reference.",
    "get_amendment_history": "Check the identifier (for example BOE-A-2015-10566) or get it "
    "again from resolve_reference.",
    "read_article": "Check the identifier and the article wording, or get the identifier again "
    "from resolve_reference.",
    "compare_article_versions": "Check the identifier, the article wording and the ISO dates, "
    "or get the identifier again from resolve_reference.",
    "get_article_history": "Check the identifier and the article wording, or get the identifier "
    "again from resolve_reference.",
    "get_law_structure": "Check the identifier (for example BOE-A-2015-10566) or get it again "
    "from resolve_reference.",
    # The trend tools send the BOE only the text and subject_code filters.
    "legislation_over_time": "Simplify the text filter or check subject_code, then try again.",
    "top_departments": "Simplify the text filter or check subject_code, then try again.",
    "compare_topics": "Simplify the text words or check the subject codes, then try again.",
    "explain_spike": "Simplify the text filter or check subject_code, then try again.",
    "most_amended_laws": "Simplify the text filter or check subject_code, then try again.",
    # The gazette tools send the BOE nothing but a date, which the arguments already constrain.
    "get_daily_summary": "Check the date, then try again.",
    "scan_gazette": "Check the dates, then try again.",
    "verify_citation": "Check the law references, the article wordings and the ISO dates, then "
    "try again.",
    "explore_amendment_chain": "Check the identifier (for example BOE-A-2020-3824) or get it "
    "again from resolve_reference.",
}


def _boe_error_message(error: BoeError, tool: str) -> str:
    """Describe a client error with its stable code, retryability and what to do next."""
    retry = "retryable" if error.retryable else "not retryable"
    summary = _ERROR_SUMMARIES.get(error.code, "BOE request failed")
    if error.code == "invalid_query":
        advice = _INVALID_QUERY_ADVICE.get(tool, "Check the arguments, then try again.")
    else:
        advice = _ERROR_ADVICE.get(error.code, "Try again later.")
    return f"{summary} ({error.code}, {retry}): {advice}"


async def _guard(call: Awaitable[_T], tool: str) -> _T:
    """Await a use case of ``tool``, translating its expected errors into ``ToolError``.

    Only deliberate argument errors (``ValueError`` raised by the use cases) are shown to the
    model. A pydantic ``ValidationError`` is a ``ValueError`` too, but here it means a bug in
    our own models and carries input values, so it propagates to be masked.
    """
    # The server is created with ``mask_error_details=True``: FastMCP shows the model only the
    # message of a ``ToolError`` and replaces any other exception with a generic one, so
    # internals (paths, input values, stack details) never leave the process. ``_guard`` is where
    # the exceptions we *want* the model to see are turned into ``ToolError``:
    #   BoeError    -> a curated message with code, retryability and next step;
    #   ValueError  -> bad arguments detected by the use cases or the client;
    # and everything else, including ``ValidationError``, propagates to be masked.
    try:
        return await call
    except BoeError as error:
        raise ToolError(_boe_error_message(error, tool)) from error
    except ValidationError:
        # Must come before ``except ValueError``, which it would otherwise be caught by.
        raise
    except ValueError as error:
        raise ToolError(f"Invalid argument (invalid_argument, not retryable): {error}") from error


def _build_cache(settings: Settings) -> Cache:
    """Create the cache backend that ``settings`` selects."""
    if settings.cache_backend == "memory":
        return MemoryCache()
    return DiskCache(settings.cache_dir)


def _lifespan(client: BoeClient | None, settings: Settings | None) -> Any:
    """Build the server lifespan, which makes the client available to the tools.

    An injected client is used as it is and never closed. Otherwise the cache, limiter and
    client are created when the server starts and closed when it stops.
    """
    # Why a lifespan: the client owns long-lived state (an HTTP connection pool, the disk cache,
    # the rate limiter's timing) that every tool call must share. Creating it per call would
    # defeat caching and rate limiting, and creating it at import time would touch the disk and
    # network before the server runs. The lifespan opens it when the server starts and closes it
    # when it stops; tools reach it through ``ctx.lifespan_context``.

    @lifespan
    async def client_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        """Provide ``{"client": ..., "catalogue": ...}`` to the tools while the server runs."""
        if client is not None:
            # Injected (tests): the caller owns it, so it is not closed here.
            yield {"client": client, "catalogue": CatalogueStore(client)}
            return
        config = settings if settings is not None else Settings.from_env()
        cache = _build_cache(config)
        limiter = RateLimiter(config.min_interval, config.max_concurrency)
        # The BoeClient context closes its HTTP client; the cache is not part of it, so the
        # ``finally`` closes it even if the server stops with an error.
        try:
            async with BoeClient(cache=cache, limiter=limiter) as own_client:
                # The catalogue store is created beside the client for the same reason: its
                # in-memory snapshot (about 12 500 laws) must be shared by every tool call.
                yield {"client": own_client, "catalogue": CatalogueStore(own_client)}
        finally:
            await cache.close()

    return client_lifespan


def _client(ctx: Context) -> BoeClient:
    """Return the shared client that the lifespan stored in the request context."""
    return ctx.lifespan_context["client"]


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
    the laws that amended those (amended_by), or what a law amended (amends). Examines a limited
    number of laws per call; when complete is false, call again to continue.
    """
    return await _guard(
        amendment_chains.explore_amendment_chain(
            _catalogue(ctx),
            _client(ctx),
            law_id,
            direction=direction,
            depth=depth,
            include=include,
            max_laws=max_laws,
            progress=ctx.report_progress,
        ),
        "explore_amendment_chain",
    )


# (function, display title). Registered by ``create_server``; every tool is read-only and
# idempotent, and "open world" because it reads an external service.
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


def create_server(*, client: BoeClient | None = None, settings: Settings | None = None) -> FastMCP:
    """Build the MCP server without touching the network or the disk.

    With no ``client``, the cache, rate limiter and client are created from ``settings`` (or
    the environment) when the server starts. An injected ``client`` is used as it is and left
    open.
    """
    # ``mask_error_details=True``: see ``_guard``.
    server = FastMCP(
        "boe-insights",
        instructions=_INSTRUCTIONS,
        lifespan=_lifespan(client, settings),
        mask_error_details=True,
    )
    for function, title in _TOOLS:
        server.tool(function, title=title, annotations=_READ_ONLY)
    return server


def main() -> None:
    """Run the server over stdio. Logs go to stderr, so stdout carries only the protocol."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    create_server().run(show_banner=False)
