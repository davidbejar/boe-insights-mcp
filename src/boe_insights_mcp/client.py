"""Async client for the BOE open data API.

All network access goes through :class:`BoeClient`, which centralises caching, rate limiting,
retries, error mapping and data normalisation. Responses are cached as raw bodies, and only
after they have parsed and validated.

Two wire formats share one request path: the JSON endpoints and the XML-only block endpoint
(api-notes 6).

Public names: ``BoeClient`` (the endpoint methods), the ``*_TTL`` cache lifetimes, ``LAW_ID``
(the identifier pattern, reused by ``references``) and ``ClientStats``. Layering: ``legislation``
calls this module; this module composes ``cache`` (raw bodies), ``ratelimit`` (spacing and
concurrency) and ``httpx``, and parses into the ``models`` types. Response quirks that explain
the code here are written up in ``docs/api-notes.md`` (cited below as "api-notes").
"""

from __future__ import annotations

import asyncio
import html
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from types import TracebackType
from typing import Literal, Self, TypeVar, get_args
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from boe_insights_mcp import __version__
from boe_insights_mcp.cache import Cache, make_key
from boe_insights_mcp.errors import (
    BoeError,
    BoeNotFoundError,
    BoeQueryError,
    BoeResponseError,
    BoeUnavailableError,
)
from boe_insights_mcp.models import (
    Block,
    IndexEntry,
    LawAnalysis,
    LawMetadata,
    LawSummary,
    StrippedText,
    as_list,
)
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.textparse import parse_block

__all__ = [
    "ANALYSIS_TTL",
    "AUXILIARY_TTL",
    "BASE_URL",
    "DEFAULT_USER_AGENT",
    "INITIAL_NOTE_TTL",
    "LAW_ID",
    "METADATA_TTL",
    "SEARCH_TTL",
    "TEXT_BLOCK_TTL",
    "TEXT_BLOCK_VERSIONED_TTL",
    "TEXT_INDEX_TTL",
    "AuxiliaryTable",
    "BoeClient",
    "ClientStats",
]

BASE_URL = "https://www.boe.es/datosabiertos/api/"
DEFAULT_USER_AGENT = (
    f"boe-insights-mcp/{__version__} (+https://github.com/davidbejar/boe-insights-mcp)"
)

# Cache lifetimes in seconds, following the suggestions in api-notes "Implications for the
# client": code tables are static, search results move within the day. The API sends no
# validators (ETag, Last-Modified), so a client-side TTL is the only freshness control
# (api-notes 9).
AUXILIARY_TTL = 7 * 24 * 3600.0
SEARCH_TTL = 30 * 60.0
METADATA_TTL = 6 * 3600.0
ANALYSIS_TTL = 6 * 3600.0
TEXT_INDEX_TTL = 6 * 3600.0
TEXT_BLOCK_TTL = 6 * 3600.0
# A block cannot change without its ``fecha_actualizacion`` in the index changing, and that date
# is part of the cache key, so a block fetched with it can be kept for a long time.
TEXT_BLOCK_VERSIONED_TTL = 30 * 24 * 3600.0
INITIAL_NOTE_TTL = 6 * 3600.0

AuxiliaryTable = Literal[
    "materias",
    "ambitos",
    "estados-consolidacion",
    "departamentos",
    "rangos",
    "relaciones-anteriores",
    "relaciones-posteriores",
]

_JSON = "application/json"
# The block endpoint answers HTTP 400 to a JSON ``Accept`` (api-notes 6).
_XML = "application/xml"
# Real block ids contain dots (``a51ter.``, ``arts.1863bisa1873bis.``; api-notes 6.1) so dots are
# allowed, but only these characters: nothing that could end the path segment, start a query or
# fragment, or be decoded into something else. ``.`` and ``..`` alone are refused separately
# because URL normalisation would treat them as path navigation.
_BLOCK_ID = re.compile(r"[A-Za-z0-9_.-]{1,60}")
# The initial note of a law is the block with this id (api-notes, "Initial note").
_NOTE_BLOCK_ID = "no"
# Cached in place of a body to remember that a law has no initial note. It is not XML, so a
# reader that mistook it for a block would fail to parse it and drop it (the stale-body path).
_NO_NOTE = b"boe-insights-mcp: no initial note"
# Retry-After is honoured but capped; the total time budget of one call (``total_timeout``) can
# still cut it short.
_MAX_RETRY_AFTER = 60.0
_JITTER_FRACTION = 0.25
_MAX_LIMIT = 1000
LAW_ID = re.compile(r"[A-Z]{2,6}-[A-Za-z]-[0-9]{4}-[0-9]{1,6}")
_RETRY_AFTER_SECONDS = re.compile(r"[0-9]+")
# Only the shape ``<status>...<text>MESSAGE</text>`` is read from error bodies (api-notes 2, 9).
_ERROR_TEXT = re.compile(r"<status>.*?<text>([^<]*)</text>", re.DOTALL)
_SERVER_ERROR_CODE = re.compile(r"Code:\s*([0-9]+)")
# "Server error" codes the API uses for a query it cannot parse (bad syntax, unknown field,
# not JSON); the HTTP status is 500 but retrying cannot help.
_QUERY_ERROR_CODES = frozenset({"101", "109"})
_TABLE = TypeAdapter(dict[str, StrippedText])

_T = TypeVar("_T")


@dataclass(frozen=True)
class ClientStats:
    """Counters of a client's activity."""

    requests_sent: int
    """HTTP requests actually sent, including retries."""
    cache_hits: int
    """Calls answered from the cache."""


@dataclass
class _Flight:
    """One in-flight network fetch shared by every concurrent caller of the same cache key.

    ``waiters`` counts the callers currently awaiting ``task``; it decides when the task may be
    cancelled (see ``BoeClient._fetch_shared``).
    """

    task: asyncio.Task[bytes]
    waiters: int = 0


def _unwrap_envelope(body: bytes) -> object:
    """Return the ``data`` of a ``{"status": ..., "data": ...}`` response body.

    Raises ``ValueError`` (which includes JSON decoding errors) if the envelope is missing.
    """
    envelope = json.loads(body)
    if not isinstance(envelope, dict) or "status" not in envelope or "data" not in envelope:
        raise ValueError("response lacks the status/data envelope")
    return envelope["data"]


def _first_record(data: object) -> object:
    """Return the single record of a ``data`` that is a one-element list or a bare object."""
    records = as_list(data)
    if not records:
        raise ValueError("response contains no record")
    return records[0]


def _from_envelope(build: Callable[[object], _T]) -> Callable[[bytes], _T]:
    """Adapt a builder of ``data`` into a builder of a JSON response body."""
    return lambda body: build(_unwrap_envelope(body))


def _parse(body: bytes, build: Callable[[bytes], _T]) -> _T:
    """Build a result from ``body``; any failure becomes a ``BoeResponseError``."""
    try:
        return build(body)
    except ValueError as error:  # includes JSON, XML and pydantic validation errors
        raise BoeResponseError(
            f"BOE returned a response that could not be parsed ({type(error).__name__})",
            status=200,
        ) from error


def _build_summaries(data: object) -> list[LawSummary]:
    """Build the search hits; ``as_list`` turns the API's ``""`` (no hits) into ``[]``."""
    return [LawSummary.model_validate(hit) for hit in as_list(data)]


def _build_metadata(data: object) -> LawMetadata:
    """Build the metadata of one law from the one-record ``data``."""
    return LawMetadata.model_validate(_first_record(data))


def _build_analysis(data: object) -> LawAnalysis:
    """Build the analysis of one law from the one-record ``data``."""
    return LawAnalysis.model_validate(_first_record(data))


def _build_table(data: object) -> dict[str, str]:
    """Validate an auxiliary table: an object mapping code to text (api-notes 7)."""
    return _TABLE.validate_python(data)


def _build_index(data: object) -> list[IndexEntry]:
    """Build the block index from ``data``, which holds the block list in any collapsed shape."""
    # ``data`` is ``[{"bloque": [...]}]``, but a one-element list collapses to an object and a
    # one-block index to a bare entry, so each layer goes through ``as_list`` (api-notes 6).
    entries: list[IndexEntry] = []
    for holder in as_list(data):
        if not isinstance(holder, dict):
            raise ValueError("index record is not an object")
        entries.extend(IndexEntry.model_validate(entry) for entry in as_list(holder.get("bloque")))
    return entries


def _api_message(response: httpx.Response) -> str | None:
    """Extract the ``<text>`` of the XML error envelope the API sends even for JSON requests.

    Only that one field is read, with a regular expression rather than an XML parser, so a
    hostile or malformed body (entity declarations, deep nesting) is never interpreted.
    Returns ``None`` when there is no text.
    """
    # The body comes from a remote server we do not control, and the envelope is always the same
    # small shape (api-notes 2, "Errors"), so a regex is enough and avoids parser attack
    # surface such as entity expansion. ``errors="replace"`` keeps a bad byte from raising while
    # we are already handling a failure.
    match = _ERROR_TEXT.search(response.content.decode("utf-8", errors="replace"))
    if match is None:
        return None
    return html.unescape(match.group(1)).strip() or None


def _is_query_error(message: str | None) -> bool:
    """Whether an HTTP 500 message carries one of the codes that mean an invalid query."""
    # The API answers a malformed query with HTTP 500 ``Server error - Code: 101`` (or 109 for a
    # non-JSON ``query``) instead of a 4xx (api-notes 2). The status alone cannot tell that from
    # a real outage, so the code is parsed out of the message text. Retrying cannot fix it.
    match = _SERVER_ERROR_CODE.search(message or "")
    return match is not None and match.group(1) in _QUERY_ERROR_CODES


def _error_for(response: httpx.Response) -> BoeError:
    """Map a non-200 response to the error describing it (check ``retryable``)."""
    # 429 is mapped defensively: it was never seen (api-notes 9), but it plainly means "slow
    # down" and so is retryable like a 5xx. Anything else (3xx, other 4xx) is not retried.
    status = response.status_code
    message = _api_message(response)
    error_class: type[BoeError]
    if status == 404:
        error_class, summary = BoeNotFoundError, "BOE resource not found"
    elif status == 400 or (status == 500 and _is_query_error(message)):
        error_class, summary = BoeQueryError, "BOE rejected the query"
    elif status == 429 or status >= 500:
        error_class, summary = BoeUnavailableError, "BOE is unavailable"
    else:
        error_class, summary = BoeResponseError, "Unexpected response from BOE"
    detail = f": {message}" if message else ""
    return error_class(f"{summary} (HTTP {status}){detail}", status=status, api_message=message)


def _retry_after(response: httpx.Response) -> float | None:
    """Return a numeric ``Retry-After`` in seconds, capped; ``None`` if absent or not a number.

    The HTTP-date form of the header is deliberately not parsed; it is treated as absent.
    """
    value = response.headers.get("Retry-After", "").strip()
    if not _RETRY_AFTER_SECONDS.fullmatch(value):
        return None
    return min(float(value), _MAX_RETRY_AFTER)


def _checked_block_id(block_id: str) -> str:
    """Return ``block_id`` if it is a safe single path segment, else raise ``ValueError``."""
    if not _BLOCK_ID.fullmatch(block_id) or block_id in {".", ".."}:
        raise ValueError(f"invalid block identifier: {block_id!r}")
    return block_id


def _checked_law_id(law_id: str) -> str:
    """Return ``law_id`` if it is well formed; this also keeps it from altering the URL path."""
    if not LAW_ID.fullmatch(law_id):
        raise ValueError(f"invalid law identifier: {law_id!r}")
    return law_id


class BoeClient:
    """Cached, rate-limited, retrying client for the BOE consolidated legislation API."""

    def __init__(
        self,
        *,
        cache: Cache,
        limiter: RateLimiter,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        total_timeout: float = 45.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a client.

        ``cache`` and ``limiter`` are shared, caller-owned objects. The remaining keyword
        arguments exist mostly so tests can inject fake time (``sleep``, ``jitter``, ``clock``).

        An injected ``http`` client is left open by :meth:`aclose`; otherwise the client owns
        one. A failed request is retried up to ``max_retries`` times, waiting
        ``backoff_base * 2**attempt`` seconds plus up to a quarter of ``backoff_base`` of
        jitter, or the server's ``Retry-After`` when it gives one. ``timeout`` limits one
        attempt; ``total_timeout`` limits the whole call, retries and waits included, measured
        with the monotonic ``clock``: no attempt or wait that cannot finish inside it is
        started, and the call then fails with ``BoeUnavailableError``.
        """
        self._cache = cache
        self._limiter = limiter
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient()
        self._base_url = base_url if base_url.endswith("/") else f"{base_url}/"
        self._user_agent = user_agent
        self._timeout = timeout
        self._total_timeout = total_timeout
        self._clock = clock
        # cache key -> fetch currently in flight; entries exist only while the fetch runs.
        self._flights: dict[str, _Flight] = {}
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._jitter = jitter
        self._requests_sent = 0
        self._cache_hits = 0

    async def search_laws(
        self, query: str | None = None, *, offset: int = 0, limit: int = 50
    ) -> list[LawSummary]:
        """Search consolidated legislation; ``query`` comes from ``build_search_query``."""
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must not be negative")
        params: dict[str, str | int] = {"limit": limit, "offset": offset}
        if query is not None:
            params["query"] = query
        return await self._get(
            "legislacion-consolidada", params, SEARCH_TTL, _from_envelope(_build_summaries)
        )

    async def get_law_metadata(self, law_id: str) -> LawMetadata:
        """Return the metadata of one law, e.g. ``BOE-A-2015-10566``."""
        path = f"legislacion-consolidada/id/{_checked_law_id(law_id)}/metadatos"
        return await self._get(path, None, METADATA_TTL, _from_envelope(_build_metadata))

    async def get_law_analysis(self, law_id: str) -> LawAnalysis:
        """Return the subjects and amendment references of one law."""
        path = f"legislacion-consolidada/id/{_checked_law_id(law_id)}/analisis"
        return await self._get(path, None, ANALYSIS_TTL, _from_envelope(_build_analysis))

    async def get_text_index(self, law_id: str) -> list[IndexEntry]:
        """Return the block index of one law's text, in document order."""
        path = f"legislacion-consolidada/id/{_checked_law_id(law_id)}/texto/indice"
        return await self._get(path, None, TEXT_INDEX_TTL, _from_envelope(_build_index))

    async def get_text_block(
        self, law_id: str, block_id: str, *, updated_on: date | None = None
    ) -> Block:
        """Return one block of a law's text with all its versions.

        ``updated_on`` is the block's ``fecha_actualizacion`` from the index. It never goes to
        the API; it only makes the cache entry specific to that revision and long-lived.
        """
        path = self._block_path(law_id, block_id)
        return await self._get(
            path,
            None,
            TEXT_BLOCK_TTL if updated_on is None else TEXT_BLOCK_VERSIONED_TTL,
            parse_block,
            accept=_XML,
            revision=updated_on,
        )

    async def get_initial_note(self, law_id: str) -> Block | None:
        """Return the law's initial note (block ``no``), or ``None`` when it has none.

        A 404 is read as "no note", so the caller must already know the law exists; an unknown
        law looks the same here. Other errors propagate and are never taken for absence.
        """
        path = self._block_path(law_id, _NOTE_BLOCK_ID)
        # Absence is stored under the very key the note itself would use, as ``_NO_NOTE``. Only a
        # 200 is cached by ``_get``, and without this a note-less law (most of them) would cost a
        # request on every overview. Exactly one entry is written, with the note's lifetime.
        key = self._cache_key(path, None, _XML, None)
        if await self._cache.get(key) == _NO_NOTE:
            self._cache_hits += 1
            return None
        try:
            return await self._get(path, None, INITIAL_NOTE_TTL, parse_block, accept=_XML)
        except BoeNotFoundError:
            await self._cache.set(key, _NO_NOTE, INITIAL_NOTE_TTL)
            return None

    async def get_auxiliary_table(self, name: AuxiliaryTable) -> dict[str, str]:
        """Return an auxiliary code table as a ``{code: text}`` mapping."""
        if name not in get_args(AuxiliaryTable):
            raise ValueError(f"unknown auxiliary table: {name!r}")
        return await self._get(
            f"datos-auxiliares/{name}", None, AUXILIARY_TTL, _from_envelope(_build_table)
        )

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it. Safe to call repeatedly."""
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        """Return the client; ``async with`` guarantees :meth:`aclose` runs on exit."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client (see :meth:`aclose`); never suppresses exceptions."""
        await self.aclose()

    @property
    def stats(self) -> ClientStats:
        """Current request and cache-hit counters."""
        return ClientStats(requests_sent=self._requests_sent, cache_hits=self._cache_hits)

    def _block_path(self, law_id: str, block_id: str) -> str:
        """The path of a block, validating both ids; the block id stays one path segment."""
        # ``safe=""`` percent-encodes anything but unreserved characters (dots are unreserved).
        # The pattern already excludes them; this is a second line of defence for the URL.
        segment = quote(_checked_block_id(block_id), safe="")
        return f"legislacion-consolidada/id/{_checked_law_id(law_id)}/texto/bloque/{segment}"

    def _cache_key(
        self,
        path: str,
        params: Mapping[str, str | int] | None,
        accept: str,
        revision: date | None,
    ) -> str:
        """The cache key of a request; ``revision`` separates entries without being requested."""
        # Why ``revision`` is in the key but not in the request: the index date says which
        # revision of a block we hold, so a changed date must miss the cache, yet the API has no
        # such parameter and would not understand it. It is added as a key-only pseudo parameter.
        extra = None if revision is None else {"updated_on": revision.isoformat()}
        return make_key(f"{self._base_url}{path}", {**(params or {}), **(extra or {})}, accept)

    async def _get(
        self,
        path: str,
        params: Mapping[str, str | int] | None,
        ttl: float,
        build: Callable[[bytes], _T],
        *,
        accept: str = _JSON,
        revision: date | None = None,
    ) -> _T:
        """Answer from the cache or the API; a body is cached only once it has been parsed.

        ``build`` turns a raw body into the result and raises ``ValueError`` when it cannot;
        ``accept`` is the wire format asked for (JSON by default, XML for blocks).

        A cached body that no longer parses (say, after an upgrade tightened a model) is
        deleted and fetched again once. Concurrent calls for the same key share one request.
        """
        # Flow: cache -> (single-flight) network -> parse. The cache stores raw bodies and each
        # call parses its own copy, so a model change takes effect without clearing the cache.
        url = f"{self._base_url}{path}"
        key = self._cache_key(path, params, accept, revision)
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                result = _parse(cached, build)
            except BoeResponseError:
                # A body we cannot parse would fail the same way on every future hit until its
                # TTL ran out, so drop it and go to the network. This is not
                # counted as a cache hit. If the fresh body is bad too, ``_fetch`` raises
                # before caching and the error reaches the caller.
                await self._cache.delete(key)
            else:
                self._cache_hits += 1
                return result
        body = await self._fetch_shared(key, url, params, ttl, build, accept)
        return _parse(body, build)

    async def _fetch_shared(
        self,
        key: str,
        url: str,
        params: Mapping[str, str | int] | None,
        ttl: float,
        build: Callable[[bytes], _T],
        accept: str,
    ) -> bytes:
        """Fetch and cache a body once for all concurrent callers of ``key`` (single flight).

        The fetch runs in its own task, so cancelling one caller leaves the others unharmed; it
        is cancelled only when no caller is left waiting. The entry is removed when it ends.
        """
        # Why single flight: identical requests can arrive at the same time (a client may issue
        # parallel tool calls), and the cache only helps once the first answer has landed. Each
        # duplicate would cost a slot of the polite request budget, so all callers of one key
        # await the same task.
        # Ownership: the task belongs to the flight, not to the caller that happened to start it.
        # That is why it is created with ``create_task`` and awaited through ``shield``:
        # cancelling the first caller then cancels only its own wait, and the other waiters
        # still get the result.
        flight = self._flights.get(key)
        if flight is None:
            task = asyncio.create_task(self._fetch(key, url, params, ttl, build, accept))
            flight = _Flight(task)
            self._flights[key] = flight
            # ``flight=flight`` binds the current value, not a late-bound loop variable.
            task.add_done_callback(lambda done, flight=flight: self._land(key, flight, done))
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                # Nobody wants the answer any more, so stop spending requests on it. The flight
                # is forgotten at once (``task.cancel`` only *requests* cancellation) so that a
                # new caller starts a fresh fetch instead of joining one that is about to die.
                flight.task.cancel()
                self._forget(key, flight)

    def _forget(self, key: str, flight: _Flight) -> None:
        """Remove ``flight`` from the registry, unless a newer flight already replaced it."""
        # The identity check matters: this can run late (from the done callback), by which time
        # another caller may have registered a new flight under the same key.
        if self._flights.get(key) is flight:
            del self._flights[key]

    def _land(self, key: str, flight: _Flight, task: asyncio.Task[bytes]) -> None:
        """Done callback: drop the flight and mark its error as seen (waiters re-raise it)."""
        self._forget(key, flight)
        if not task.cancelled():
            # Fetching the exception marks it as retrieved. If every waiter was cancelled, nobody
            # would otherwise read it and asyncio would log "exception was never retrieved".
            task.exception()

    async def _fetch(
        self,
        key: str,
        url: str,
        params: Mapping[str, str | int] | None,
        ttl: float,
        build: Callable[[bytes], _T],
        accept: str,
    ) -> bytes:
        """Download the body, check that it parses, cache it and return it."""
        body = await self._request(url, params, accept)
        _parse(body, build)  # validate before caching
        await self._cache.set(key, body, ttl)
        return body

    async def _request(
        self, url: str, params: Mapping[str, str | int] | None, accept: str
    ) -> bytes:
        """GET ``url`` and return the body, retrying transient failures within the budget."""
        # ``total_timeout`` bounds the whole call, so a caller (and the model behind it) is never
        # left waiting minutes on retries. The deadline is checked before each attempt, caps each
        # attempt's own timeout, and decides whether a backoff wait is worth starting.
        deadline = self._clock() + self._total_timeout
        attempt = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise self._out_of_time(None)
            delay: float | None = None
            # The limiter is held for one attempt only. The backoff sleep below happens outside
            # it, so a waiting retry does not occupy a concurrency slot.
            async with self._limiter:
                self._requests_sent += 1
                try:
                    response = await self._http.get(
                        url,
                        params=params,
                        headers={"Accept": accept, "User-Agent": self._user_agent},
                        timeout=min(self._timeout, remaining),
                    )
                except (httpx.TimeoutException, httpx.TransportError) as error:
                    failure: BoeError = BoeUnavailableError(
                        f"Could not reach BOE ({type(error).__name__})"
                    )
                else:
                    if response.status_code == 200:
                        return response.content
                    failure = _error_for(response)
                    delay = _retry_after(response)
            if not failure.retryable or attempt == self._max_retries:
                raise failure
            # A server-supplied Retry-After wins over our own backoff.
            wait = self._backoff(attempt) if delay is None else delay
            if self._clock() + wait >= deadline:
                # Sleeping would leave no time for another attempt, so fail now with the details
                # of the last failure instead of waiting only to give up.
                raise self._out_of_time(failure)
            await self._sleep(wait)
            attempt += 1

    def _out_of_time(self, last: BoeError | None) -> BoeUnavailableError:
        """The error for an exhausted ``total_timeout``, keeping the last failure's details."""
        error = BoeUnavailableError(
            f"BOE did not answer within {self._total_timeout:g} seconds",
            status=last.status if last else None,
            api_message=last.api_message if last else None,
        )
        if last is not None:
            error.__cause__ = last
        return error

    def _backoff(self, attempt: int) -> float:
        """Exponential delay for retry number ``attempt`` (from 0), with jitter."""
        return self._backoff_base * (2**attempt + self._jitter() * _JITTER_FRACTION)
