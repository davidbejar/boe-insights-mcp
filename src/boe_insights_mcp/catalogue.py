"""The in-process snapshot of the whole consolidated legislation collection.

Trend questions ("how many decree-laws per year?") need every law's rank, department and date,
and the API has no aggregate endpoint, so the only way is to download the collection (about
12 500 records, 16 MB in three requests; api-notes, third probe, "Bulk download") and count
locally. :class:`CatalogueStore` does that once, keeps the result in memory for a day and shares
one download between concurrent callers. ``select`` covers the two criteria a search hit cannot
be tested against locally (body text and subject), by asking the API for exactly those laws.

Layering: ``trends`` calls the store; the store calls ``client.search_laws`` and
``query.build_search_query``; the counting itself lives in ``analytics``. The pages are also
cached by the client's own (disk) cache for the same day, so a restart does not repeat the
download.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from boe_insights_mcp.clock import utc_now
from boe_insights_mcp.errors import BoeResponseError
from boe_insights_mcp.models import LawSummary
from boe_insights_mcp.query import build_search_query

__all__ = [
    "CATALOGUE_PAGE_SIZE",
    "CATALOGUE_TTL",
    "CONFIRMATION_TTL",
    "MAX_CATALOGUE_PAGES",
    "Catalogue",
    "CatalogueStore",
    "ProgressCallback",
    "SearchClient",
]

# 6.7 MB per page, a quarter of the response cap (api-notes, "Bulk download").
CATALOGUE_PAGE_SIZE = 5000
CATALOGUE_TTL = 24 * 3600.0
# The stamp of the download expires this much before the cached pages, which are written
# first and so expire first; without the margin a stamp could outlive its pages and make a fresh
# download look old.
_STAMP_MARGIN = 120.0
# How long the confirming request after a download may be cached. It must never be served stale,
# or it would confirm an old answer instead of checking the API now; a minute only covers an
# immediate repeat.
CONFIRMATION_TTL = 60.0
# A safety stop, not an expectation: 40 000 records is three times today's collection. Without
# it a misbehaving API that never returns a short page would make us download forever.
MAX_CATALOGUE_PAGES = 8

_LOG = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int | None, str], Awaitable[None]]
"""``(done, total, message)``; ``total`` is ``None`` because the API gives no count."""


class SearchClient(Protocol):
    """The one client method the store needs; ``BoeClient`` satisfies it (tests use a fake)."""

    async def search_laws(
        self,
        query: str | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        ttl: float | None = None,
    ) -> list[LawSummary]:
        """Return one page of search hits."""
        ...


@runtime_checkable
class StampingClient(Protocol):
    """Optional client capability: remembering when the cached pages were downloaded.

    ``BoeClient`` has it (the stamp lives in the same disk cache as the pages); a client
    without it makes the store use the time its own download finished.
    """

    async def read_catalogue_stamp(self) -> datetime | None:
        """The time the cached pages were downloaded, or ``None``."""
        ...

    async def write_catalogue_stamp(self, when: datetime, ttl: float) -> None:
        """Remember ``when`` for ``ttl`` seconds."""
        ...


@dataclass(frozen=True)
class Catalogue:
    """A snapshot of every consolidated law."""

    laws: tuple[LawSummary, ...]
    """Unique by identifier, in API order (publication date, newest first)."""
    fetched_at: datetime
    """UTC time at which the pages were really downloaded (from the stamp when they were cached)."""
    newest_update: datetime | None
    """The latest ``updated_at`` of any law: the BOE's own freshness stamp for the data."""


@dataclass
class _Download:
    """One download in flight, shared by every caller that asked while it ran.

    ``waiters`` counts the callers awaiting ``task``; when it drops to zero the task is
    cancelled. ``listeners`` are those callers' progress callbacks.
    """

    task: asyncio.Task[Catalogue]
    listeners: list[_Listener] = field(default_factory=list)
    waiters: int = 0


@dataclass(frozen=True)
class _Listener:
    """A progress callback together with the context of the caller that registered it."""

    callback: ProgressCallback
    context: contextvars.Context

    @staticmethod
    def of(callback: ProgressCallback) -> _Listener:
        """A listener for ``callback`` that runs in a copy of the current context."""
        return _Listener(callback, contextvars.copy_context())


def _unique(page: Sequence[LawSummary]) -> list[LawSummary]:
    """The laws of ``page`` without repeated identifiers, keeping the first copy of each."""
    seen: set[str] = set()
    laws: list[LawSummary] = []
    for law in page:
        if law.identifier not in seen:
            seen.add(law.identifier)
            laws.append(law)
    return laws


async def _call(callback: ProgressCallback, done: int, message: str) -> None:
    """Await one progress callback; a coroutine, which is what ``create_task`` accepts."""
    await callback(done, None, message)


async def _notify(listeners: Sequence[_Listener], done: int, message: str) -> None:
    """Call every progress callback in its own caller's context, ignoring the ones that fail."""
    # Progress is a courtesy. A callback can fail because the client that wanted it went away,
    # and that must not abort a 30-second download other callers are waiting for.
    for listener in list(listeners):
        try:
            # The download runs in the task of whoever started it, but FastMCP's
            # ``Context.report_progress`` finds its request through a context variable. Running
            # the callback in a task created with the caller's context makes it report to the
            # caller's own request instead of to the first one's.
            await asyncio.create_task(
                _call(listener.callback, done, message), context=listener.context
            )
        except Exception:
            _LOG.debug("progress callback failed", exc_info=True)


class CatalogueStore:
    """Downloads, de-duplicates and remembers the catalogue, and runs API-side selections."""

    def __init__(self, client: SearchClient, *, clock: Callable[[], datetime] = utc_now) -> None:
        """Create a store over ``client``; ``clock`` returns the current UTC time (injectable)."""
        self._client = client
        self._clock = clock
        self._snapshot: Catalogue | None = None
        self._download: _Download | None = None

    async def get(self, *, progress: ProgressCallback | None = None) -> Catalogue:
        """Return the snapshot, downloading it first when there is none or it is a day old.

        Concurrent callers share one download. A failed or cancelled download leaves no
        snapshot, so the next call tries again. ``progress`` is awaited after each page.
        """
        snapshot = self._snapshot
        if snapshot is not None and self._is_fresh(snapshot):
            return snapshot
        download = self._download
        listener = _Listener.of(progress) if progress is not None else None
        if download is None:
            listeners: list[_Listener] = []
            task = asyncio.create_task(self._download_all(listeners))
            download = _Download(task, listeners)
            self._download = download
            task.add_done_callback(lambda done, download=download: self._land(download, done))
        if listener is not None:
            download.listeners.append(listener)
        download.waiters += 1
        try:
            # Same ownership scheme as ``BoeClient._fetch_shared``: the task belongs to the
            # download, not to the caller that started it, and callers await it through
            # ``shield`` so cancelling one caller cancels only its own wait.
            return await asyncio.shield(download.task)
        except Exception:
            if snapshot is None:
                raise
            # A refresh that fails must not take a working tool down: the previous snapshot is
            # served, and its own ``fetched_at`` tells the caller how old it is. Cancellation is
            # a ``BaseException`` and passes through. The next call tries to refresh again.
            _LOG.warning("catalogue refresh failed, serving the previous snapshot", exc_info=True)
            return snapshot
        finally:
            download.waiters -= 1
            if listener is not None:
                download.listeners.remove(listener)
            if download.waiters == 0 and not download.task.done():
                # Nobody wants the result any more: stop spending requests. The download is
                # forgotten at once, because ``cancel`` only *requests* cancellation and a new
                # caller must start a fresh download instead of joining a dying one.
                download.task.cancel()
                self._forget(download)

    async def select(
        self,
        *,
        text: str | None = None,
        subject_code: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[LawSummary]:
        """The laws matching a criterion only the API can evaluate (body text, subject).

        Search hits carry no body text and no subjects, so these two filters cannot be applied
        to the snapshot. With neither argument this is the whole catalogue. Raises
        ``ValueError`` for a malformed subject code and when the criterion matches
        ``CATALOGUE_PAGE_SIZE`` laws or more.
        """
        if text is None and subject_code is None:
            return list((await self.get(progress=progress)).laws)
        query = build_search_query(text=text, subject_code=subject_code, sort_by="published_on")
        # One page only. A "topic" is a selective set of laws; a criterion that fills a page
        # (a common word such as "ley" matches most of the collection) is not one, and paging
        # through it would cost several 6 MB requests per topic for a result that says nothing.
        # A full page cannot tell "exactly this many" from "more", and a set that large is too
        # broad either way, so a full page is rejected without a second request.
        # ``ttl=None``: a selection is specific to this request, so the normal 30-minute search
        # lifetime applies rather than the catalogue's day.
        page = await self._client.search_laws(query, offset=0, limit=CATALOGUE_PAGE_SIZE, ttl=None)
        if len(page) >= CATALOGUE_PAGE_SIZE:
            criterion = " + ".join(part for part in (text, subject_code) if part is not None)
            raise ValueError(
                f"'{criterion}' matches more than {CATALOGUE_PAGE_SIZE:,} laws, too many to be "
                "a topic; use more specific words."
            )
        laws = _unique(page)
        if progress is not None:
            await _notify([_Listener.of(progress)], len(laws), f"Searching: {len(laws)} laws")
        return laws

    def _is_fresh(self, snapshot: Catalogue) -> bool:
        """Whether ``snapshot`` is younger than ``CATALOGUE_TTL``."""
        return (self._clock() - snapshot.fetched_at).total_seconds() < CATALOGUE_TTL

    def _forget(self, download: _Download) -> None:
        """Drop ``download`` from the registry, unless a newer one has already replaced it."""
        # The identity check matters because this can run late (from the done callback), when
        # another caller may already have registered a new download.
        if self._download is download:
            self._download = None

    def _land(self, download: _Download, task: asyncio.Task[Catalogue]) -> None:
        """Done callback: drop the download and mark its error as retrieved."""
        self._forget(download)
        if not task.cancelled():
            # Reading the exception marks it as seen. If every caller was cancelled nobody
            # would read it, and asyncio would log "exception was never retrieved".
            task.exception()

    async def _download_all(self, listeners: list[_Listener]) -> Catalogue:
        """Download the whole collection and store it as the snapshot."""
        # The client's disk cache can serve the pages after a restart. Without the stamp the
        # snapshot would claim to be downloaded now and then be kept another full day.
        stampable = self._client if isinstance(self._client, StampingClient) else None
        stamp = await stampable.read_catalogue_stamp() if stampable else None
        # Sorted by publication date, which never changes for a record, unlike the default
        # update-date order: a law updated mid-crawl would otherwise jump between pages
        # (api-notes, "Bulk download").
        query = build_search_query(sort_by="published_on")
        laws = await self._crawl(query, CATALOGUE_TTL, listeners, "Downloading the catalogue")
        updates = [law.updated_at for law in laws if law.updated_at is not None]
        fetched_at = stamp if stamp is not None else self._clock()
        if stampable is not None and stamp is None:
            try:
                await stampable.write_catalogue_stamp(fetched_at, CATALOGUE_TTL - _STAMP_MARGIN)
            except Exception:  # a cache that cannot store the stamp must not lose the download
                _LOG.warning("could not store the catalogue stamp", exc_info=True)
        snapshot = Catalogue(
            laws=tuple(laws), fetched_at=fetched_at, newest_update=max(updates, default=None)
        )
        # Set only when the whole download succeeded, so a failure or cancellation leaves the
        # previous state (no snapshot) and the next caller retries.
        self._snapshot = snapshot
        return snapshot

    async def _crawl(
        self, query: str | None, ttl: float | None, listeners: list[_Listener], label: str
    ) -> list[LawSummary]:
        """Page through ``query`` until a short page, confirm the end, and return the laws.

        Raises ``BoeResponseError`` when ``MAX_CATALOGUE_PAGES`` full pages come back or when
        the confirming request finds a record after the last page.
        """
        laws: list[LawSummary] = []
        seen: set[str] = set()
        received = 0
        for page_number in range(MAX_CATALOGUE_PAGES):
            # The offset counts raw API positions, duplicates included, so that no record is
            # skipped when an earlier one was added or removed during the crawl.
            page = await self._client.search_laws(
                query, offset=page_number * CATALOGUE_PAGE_SIZE, limit=CATALOGUE_PAGE_SIZE, ttl=ttl
            )
            received += len(page)
            for law in page:
                # A record added mid-crawl shifts everything after it by one, so the last
                # record of one page reappears at the start of the next: keep the first copy.
                if law.identifier not in seen:
                    seen.add(law.identifier)
                    laws.append(law)
            await _notify(listeners, len(laws), f"{label}: {len(laws)} laws so far")
            if len(page) < CATALOGUE_PAGE_SIZE:
                await self._confirm_end(query, received)
                return laws
        raise BoeResponseError(
            f"BOE returned more than {MAX_CATALOGUE_PAGES * CATALOGUE_PAGE_SIZE} records"
        )

    async def _confirm_end(self, query: str | None, received: int) -> None:
        """Check that no record follows the ``received`` ones; raise ``BoeResponseError`` if so."""
        # A short page is the only signal that the crawl is over, so one wrongly short page
        # would silently cut the catalogue, and the snapshot would then be kept for a day. Asking
        # for the record at the position just past the end costs one tiny request and turns that
        # silent truncation into an error, which makes the store fall back to the previous
        # snapshot. The short lifetime keeps this answer from ever being served stale.
        extra = await self._client.search_laws(
            query, offset=received, limit=1, ttl=CONFIRMATION_TTL
        )
        if extra:
            raise BoeResponseError(
                f"BOE paging was inconsistent: a page was short after {received} records, "
                "but more records follow"
            )
