"""The MCP server: tool registration, client lifecycle and error translation.

This layer is deliberately thin. Every tool calls a use case in :mod:`boe_insights_mcp.legislation`
and turns client errors into messages the model can act on.
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

from boe_insights_mcp import legislation
from boe_insights_mcp.cache import Cache, DiskCache, MemoryCache
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.errors import BoeError
from boe_insights_mcp.legislation import MAX_LIMIT, RankFilter, ScopeFilter
from boe_insights_mcp.query import SortField
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import (
    FindCodesResult,
    LawOverview,
    ResolveReferenceResult,
    SearchLegislationResult,
)
from boe_insights_mcp.settings import Settings

__all__ = ["create_server", "main"]

_T = TypeVar("_T")

_INSTRUCTIONS = (
    "Tools over Spanish legislation from the BOE (Boletín Oficial del Estado) consolidated "
    "legislation open data. Call resolve_reference first to turn a name such as 'Ley 40/2015' "
    "into a BOE identifier, then use that identifier with the other tools. Use "
    "search_legislation to explore by topic, rank, department or date; find_codes looks up the "
    "department, subject and rank codes its filters take. The status of a law is derived from "
    "BOE flags and amendment references, so treat it as an indication. When quoting results, "
    "pass on the attribution 'Basado en datos de la Agencia Estatal Boletín Oficial del "
    "Estado' (https://www.boe.es) and say that consolidated texts are informational and have "
    "no official legal value."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

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
    try:
        return await call
    except BoeError as error:
        raise ToolError(_boe_error_message(error, tool)) from error
    except ValidationError:
        raise
    except ValueError as error:
        raise ToolError(f"Invalid argument (invalid_argument, not retryable): {error}") from error


def _build_cache(settings: Settings) -> Cache:
    if settings.cache_backend == "memory":
        return MemoryCache()
    return DiskCache(settings.cache_dir)


def _lifespan(client: BoeClient | None, settings: Settings | None) -> Any:
    """Build the server lifespan, which makes the client available to the tools.

    An injected client is used as it is and never closed. Otherwise the cache, limiter and
    client are created when the server starts and closed when it stops.
    """

    @lifespan
    async def client_lifespan(server: FastMCP) -> AsyncIterator[dict[str, BoeClient]]:
        if client is not None:
            yield {"client": client}
            return
        config = settings if settings is not None else Settings.from_env()
        cache = _build_cache(config)
        limiter = RateLimiter(config.min_interval, config.max_concurrency)
        try:
            async with BoeClient(cache=cache, limiter=limiter) as own_client:
                yield {"client": own_client}
        finally:
            await cache.close()

    return client_lifespan


def _client(ctx: Context) -> BoeClient:
    return ctx.lifespan_context["client"]


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
    dates, a status with a plain-English note, and counts and samples of the norms that amend
    it or that it amends. The status is derived from the BOE flags (repealed, annulled,
    validity exhausted) and from the amendment references (later norms that repeal it, giving
    'partially_repealed'), so it is an indication, not a legal determination: 'in_force' does
    not mean every provision is current. Get the identifier from resolve_reference.
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


_TOOLS: tuple[tuple[Callable[..., Awaitable[Any]], str], ...] = (
    (search_legislation, "Search Spanish legislation"),
    (resolve_reference, "Resolve a law reference"),
    (get_law_overview, "Get a law overview"),
    (find_codes, "Find BOE codes"),
)


def create_server(*, client: BoeClient | None = None, settings: Settings | None = None) -> FastMCP:
    """Build the MCP server without touching the network or the disk.

    With no ``client``, the cache, rate limiter and client are created from ``settings`` (or
    the environment) when the server starts. An injected ``client`` is used as it is and left
    open.
    """
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
