"""MCP resources: a law, an article, a day of the gazette, code tables and saved searches.

In apps that support MCP resources a person can attach one of these to the conversation. Many
apps do not let the model open resources, so a resource offers nothing a tool does not, with one
exception: the department and rank tables are offered whole only as a resource (``find_codes``
searches them but never lists them). Every other resource returns exactly what the matching tool
returns (``legislation.get_law_overview``, ``articles.read_article``, ``gazette.daily_summary``).
The JSON is the result model's own serialisation, so provenance, the BOE attribution and the
informational notice travel with it, as the BOE reuse conditions require.

The one static resource, ``boe://saved-searches``, is the owner's list of saved searches, read from
the store the server lifespan provides.

``server.create_server`` calls :func:`register`; the use cases do all the work and this module
only maps URIs onto them and turns expected failures into ``ResourceError`` messages.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Literal, TypeVar

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ResourceError
from pydantic import BaseModel, ConfigDict, Field

from boe_insights_mcp import articles, gazette, legislation, owner, saved_searches
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.clock import utc_now
from boe_insights_mcp.guard import guarded, invalid_argument
from boe_insights_mcp.legislation import CODE_TABLES
from boe_insights_mcp.schemas import Provenance, SavedSearchList

__all__ = ["CodeEntry", "CodeTable", "register"]

_T = TypeVar("_T")
_JSON = "application/json"
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# The subject table has about 6,800 entries (about 0.5 MB): too large to attach to a conversation,
# and ``find_codes`` is the way to search it. The other two tables are small enough to attach.
ResourceCodeKind = Literal["department", "rank"]
_KINDS: tuple[ResourceCodeKind, ...] = ("department", "rank")


class CodeEntry(BaseModel):
    """One row of a BOE code table."""

    model_config = ConfigDict(frozen=True)

    code: str
    text: str


class CodeTable(BaseModel):
    """A whole BOE code table (departments or ranks), sorted by text."""

    model_config = ConfigDict(frozen=True)

    kind: ResourceCodeKind
    total: int = Field(description="Number of entries in codes.")
    codes: list[CodeEntry]
    provenance: Provenance


async def _guard(call: Awaitable[_T], tool: str) -> _T:
    """Await a use case of ``tool``, translating its expected errors into ``ResourceError``."""
    # Same policy as ``server._guard`` (``guard.guarded``) but raising ``ResourceError``: the
    # server masks every other exception, and only a ``ResourceError`` message reaches the person.
    return await guarded(call, tool, ResourceError)


def _invalid(message: str) -> ResourceError:
    """The curated error for a bad argument."""
    return ResourceError(invalid_argument(message))


def _client(ctx: Context) -> BoeClient:
    """Return the shared client that the server lifespan stored in the request context."""
    return ctx.lifespan_context["client"]


def _parse_day(day: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` day, raising a curated ``ResourceError`` otherwise."""
    # Taken as a string, not ``date``: a pydantic failure on a typed parameter is masked to a
    # generic message, and this way the person is told what format is expected. The pattern is
    # checked first because ``date.fromisoformat`` also accepts basic forms such as 20261120.
    if _ISO_DAY.fullmatch(day):
        try:
            return date.fromisoformat(day)
        except ValueError:
            pass
    raise _invalid("day must be an ISO date (YYYY-MM-DD), e.g. 2026-11-20")


def _as_json(result: BaseModel) -> str:
    """Serialise a result model like the tools do (same field omission rules)."""
    return result.model_dump_json()


async def _owned(use_case: Callable[[str], Awaitable[_T]]) -> _T:
    """Run ``use_case`` for the owner of the current request (see ``server._owned``)."""
    return await use_case(owner.current_owner())


async def _law(law_id: str, ctx: Context) -> str:
    """Overview of a law, as ``get_law_overview`` returns it."""
    return _as_json(
        await _guard(legislation.get_law_overview(_client(ctx), law_id), "get_law_overview")
    )


async def _article(law_id: str, block_id: str, ctx: Context) -> str:
    """One article block as in force today, as ``read_article`` returns it."""
    return _as_json(
        await _guard(articles.read_article(_client(ctx), law_id, block_id=block_id), "read_article")
    )


async def _gazette_day(day: str, ctx: Context) -> str:
    """One day's gazette summary with as many items as one page may hold."""
    return _as_json(
        await _guard(
            gazette.daily_summary(_client(ctx), day=_parse_day(day), limit=gazette.MAX_LIMIT),
            "get_daily_summary",
        )
    )


async def _build_table(client: BoeClient, kind: str) -> CodeTable:
    """Fetch the table for ``kind`` and sort it by text (case-insensitive, then by code)."""
    if kind == "subject":
        raise ValueError(
            "the subject table is too large to attach; use the find_codes tool for subjects"
        )
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {', '.join(_KINDS)}")
    table = await client.get_auxiliary_table(CODE_TABLES[kind])
    # Texts may carry trailing spaces (api-notes, "Auxiliary tables"): strip them for display.
    entries = sorted(
        (CodeEntry(code=code, text=text.strip()) for code, text in table.items()),
        key=lambda entry: (entry.text.casefold(), entry.code),
    )
    return CodeTable(
        kind=kind,
        total=len(entries),
        codes=entries,
        provenance=Provenance(generated_at=utc_now()),
    )


async def _codes(kind: str, ctx: Context) -> str:
    """A whole code table: the data behind the filters of ``search_legislation``."""
    return _as_json(await _guard(_build_table(_client(ctx), kind), "find_codes"))


async def _saved_searches(ctx: Context) -> str:
    """The current owner's saved searches, as ``list_saved_searches`` returns them."""

    # The owner comes from the request, never from the URI, so one person's list cannot be read
    # by naming another.
    async def listing(who: str) -> SavedSearchList:
        """The list for ``who``."""
        return await saved_searches.list_saved_searches(ctx.lifespan_context["searches"], who)

    # The owner is looked up inside the guard so that a missing identity is a curated message.
    return _as_json(await _guard(_owned(listing), "list_saved_searches"))


def register(server: FastMCP) -> None:
    """Register the four resource templates and the saved-searches resource on ``server``."""
    # Registered by call rather than with decorators on the module-level functions so that the
    # texts below, which the model and the person see, sit in one place.
    specs: list[tuple[Any, str, str, str, str]] = [
        (
            _law,
            "boe://law/{law_id}",
            "law",
            "Law overview",
            "Overview of a consolidated law: status, dates, department and amendment counts.",
        ),
        (
            _article,
            "boe://law/{law_id}/article/{block_id}",
            "article",
            "Article of a law",
            "Current text of one article of a consolidated law, by its block id.",
        ),
        (
            _gazette_day,
            "boe://gazette/{day}",
            "gazette_day",
            "Gazette of a day",
            "What the BOE published on one day (ISO date), grouped by section and department; "
            "gives the first 100 items of the day.",
        ),
        (
            _codes,
            "boe://codes/{kind}",
            "codes",
            "Code table",
            "Every code and name of one BOE table; kind is department or rank. For subject codes "
            "use the find_codes tool.",
        ),
    ]
    specs.append(
        (
            _saved_searches,
            "boe://saved-searches",
            "saved_searches",
            "Saved searches",
            "Your saved searches, with their filters and when each was last checked.",
        )
    )
    for function, uri, name, title, description in specs:
        server.resource(uri, name=name, title=title, description=description, mime_type=_JSON)(
            function
        )
