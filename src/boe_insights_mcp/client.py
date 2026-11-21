"""Async client for the BOE open data API.

All network access goes through :class:`BoeClient`, which centralises caching, rate limiting,
retries, error mapping and data normalisation. Responses are cached as raw bodies, and only
after they have parsed and validated.
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
from types import TracebackType
from typing import Literal, Self, TypeVar, get_args

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
    LawAnalysis,
    LawMetadata,
    LawSummary,
    StrippedText,
    as_list,
)
from boe_insights_mcp.ratelimit import RateLimiter

__all__ = [
    "ANALYSIS_TTL",
    "AUXILIARY_TTL",
    "BASE_URL",
    "DEFAULT_USER_AGENT",
    "LAW_ID",
    "METADATA_TTL",
    "SEARCH_TTL",
    "AuxiliaryTable",
    "BoeClient",
    "ClientStats",
]

BASE_URL = "https://www.boe.es/datosabiertos/api/"
DEFAULT_USER_AGENT = (
    f"boe-insights-mcp/{__version__} (+https://github.com/davidbejar/boe-insights-mcp)"
)

AUXILIARY_TTL = 7 * 24 * 3600.0
SEARCH_TTL = 30 * 60.0
METADATA_TTL = 6 * 3600.0
ANALYSIS_TTL = 6 * 3600.0

AuxiliaryTable = Literal[
    "materias",
    "ambitos",
    "estados-consolidacion",
    "departamentos",
    "rangos",
    "relaciones-anteriores",
    "relaciones-posteriores",
]

_ACCEPT = "application/json"
_MAX_RETRY_AFTER = 60.0
_JITTER_FRACTION = 0.25
_MAX_LIMIT = 1000
LAW_ID = re.compile(r"[A-Z]{2,6}-[A-Za-z]-[0-9]{4}-[0-9]{1,6}")
_RETRY_AFTER_SECONDS = re.compile(r"[0-9]+")
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
    """One in-flight network fetch shared by every concurrent caller of the same cache key."""

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


def _parse(body: bytes, build: Callable[[object], _T]) -> _T:
    """Unwrap ``body`` and build a result; any failure becomes a ``BoeResponseError``."""
    try:
        return build(_unwrap_envelope(body))
    except ValueError as error:  # includes JSON and pydantic validation errors
        raise BoeResponseError(
            f"BOE returned a response that could not be parsed ({type(error).__name__})",
            status=200,
        ) from error


def _build_summaries(data: object) -> list[LawSummary]:
    return [LawSummary.model_validate(hit) for hit in as_list(data)]


def _build_metadata(data: object) -> LawMetadata:
    return LawMetadata.model_validate(_first_record(data))


def _build_analysis(data: object) -> LawAnalysis:
    return LawAnalysis.model_validate(_first_record(data))


def _build_table(data: object) -> dict[str, str]:
    return _TABLE.validate_python(data)


def _api_message(response: httpx.Response) -> str | None:
    """Extract the ``<text>`` of the XML error envelope the API sends even for JSON requests.

    Two fields are read with a regular expression rather than a XML parser, so a hostile or
    malformed body (entity declarations, deep nesting) is never interpreted.
    """
    match = _ERROR_TEXT.search(response.content.decode("utf-8", errors="replace"))
    if match is None:
        return None
    return html.unescape(match.group(1)).strip() or None


def _is_query_error(message: str | None) -> bool:
    """Whether an HTTP 500 message carries one of the codes that mean an invalid query."""
    match = _SERVER_ERROR_CODE.search(message or "")
    return match is not None and match.group(1) in _QUERY_ERROR_CODES


def _error_for(response: httpx.Response) -> BoeError:
    """Map a non-200 response to the error describing it (check ``retryable``)."""
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
    """Return a numeric ``Retry-After`` in seconds, capped; ``None`` if absent or not a number."""
    value = response.headers.get("Retry-After", "").strip()
    if not _RETRY_AFTER_SECONDS.fullmatch(value):
        return None
    return min(float(value), _MAX_RETRY_AFTER)


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
        self._headers = {"Accept": _ACCEPT, "User-Agent": user_agent}
        self._timeout = timeout
        self._total_timeout = total_timeout
        self._clock = clock
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
        return await self._get("legislacion-consolidada", params, SEARCH_TTL, _build_summaries)

    async def get_law_metadata(self, law_id: str) -> LawMetadata:
        """Return the metadata of one law, e.g. ``BOE-A-2015-10566``."""
        path = f"legislacion-consolidada/id/{_checked_law_id(law_id)}/metadatos"
        return await self._get(path, None, METADATA_TTL, _build_metadata)

    async def get_law_analysis(self, law_id: str) -> LawAnalysis:
        """Return the subjects and amendment references of one law."""
        path = f"legislacion-consolidada/id/{_checked_law_id(law_id)}/analisis"
        return await self._get(path, None, ANALYSIS_TTL, _build_analysis)

    async def get_auxiliary_table(self, name: AuxiliaryTable) -> dict[str, str]:
        """Return an auxiliary code table as a ``{code: text}`` mapping."""
        if name not in get_args(AuxiliaryTable):
            raise ValueError(f"unknown auxiliary table: {name!r}")
        return await self._get(f"datos-auxiliares/{name}", None, AUXILIARY_TTL, _build_table)

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it. Safe to call repeatedly."""
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def stats(self) -> ClientStats:
        """Current request and cache-hit counters."""
        return ClientStats(requests_sent=self._requests_sent, cache_hits=self._cache_hits)

    async def _get(
        self,
        path: str,
        params: Mapping[str, str | int] | None,
        ttl: float,
        build: Callable[[object], _T],
    ) -> _T:
        """Answer from the cache or the API; a body is cached only once it has been parsed.

        A cached body that no longer parses (say, after an upgrade tightened a model) is
        deleted and fetched again once. Concurrent calls for the same key share one request.
        """
        url = f"{self._base_url}{path}"
        key = make_key(url, params, _ACCEPT)
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                result = _parse(cached, build)
            except BoeResponseError:
                await self._cache.delete(key)
            else:
                self._cache_hits += 1
                return result
        body = await self._fetch_shared(key, url, params, ttl, build)
        return _parse(body, build)

    async def _fetch_shared(
        self,
        key: str,
        url: str,
        params: Mapping[str, str | int] | None,
        ttl: float,
        build: Callable[[object], _T],
    ) -> bytes:
        """Fetch and cache a body once for all concurrent callers of ``key`` (single flight).

        The fetch runs in its own task, so cancelling one caller leaves the others unharmed; it
        is cancelled only when no caller is left waiting. The entry is removed when it ends.
        """
        flight = self._flights.get(key)
        if flight is None:
            task = asyncio.create_task(self._fetch(key, url, params, ttl, build))
            flight = _Flight(task)
            self._flights[key] = flight
            task.add_done_callback(lambda done, flight=flight: self._land(key, flight, done))
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                flight.task.cancel()
                self._forget(key, flight)

    def _forget(self, key: str, flight: _Flight) -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]

    def _land(self, key: str, flight: _Flight, task: asyncio.Task[bytes]) -> None:
        """Done callback: drop the flight and mark its error as seen (waiters re-raise it)."""
        self._forget(key, flight)
        if not task.cancelled():
            task.exception()

    async def _fetch(
        self,
        key: str,
        url: str,
        params: Mapping[str, str | int] | None,
        ttl: float,
        build: Callable[[object], _T],
    ) -> bytes:
        body = await self._request(url, params)
        _parse(body, build)  # validate before caching
        await self._cache.set(key, body, ttl)
        return body

    async def _request(self, url: str, params: Mapping[str, str | int] | None) -> bytes:
        """GET ``url`` and return the body, retrying transient failures within the budget."""
        deadline = self._clock() + self._total_timeout
        attempt = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise self._out_of_time(None)
            delay: float | None = None
            async with self._limiter:
                self._requests_sent += 1
                try:
                    response = await self._http.get(
                        url,
                        params=params,
                        headers=self._headers,
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
            wait = self._backoff(attempt) if delay is None else delay
            if self._clock() + wait >= deadline:
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
