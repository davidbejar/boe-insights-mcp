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
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from boe_insights_mcp.clock import utc_now
from boe_insights_mcp.errors import BoeResponseError
from boe_insights_mcp.models import LawSummary
from boe_insights_mcp.query import build_search_query

__all__ = [
    "CATALOGUE_PAGE_SIZE",
    "CATALOGUE_TTL",
    "MAX_CATALOGUE_PAGES",
    "Catalogue",
    "CatalogueStore",
    "ProgressCallback",
    "SearchClient",
]

# 6.7 MB per page, a quarter of the response cap (api-notes, "Bulk download").
CATALOGUE_PAGE_SIZE = 5000
CATALOGUE_TTL = 24 * 3600.0
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


@dataclass(frozen=True)
class Catalogue:
    """A snapshot of every consolidated law."""

    laws: tuple[LawSummary, ...]
    """Unique by identifier, in API order (publication date, newest first)."""
    fetched_at: datetime
    """UTC time at which the download finished."""
    newest_update: datetime | None
    """The latest ``updated_at`` of any law: the BOE's own freshness stamp for the data."""


@dataclass
class _Download:
    """One download in flight, shared by every caller that asked while it ran.

    ``waiters`` counts the callers awaiting ``task``; when it drops to zero the task is
    cancelled. ``listeners`` are those callers' progress callbacks.
    """

    task: asyncio.Task[Catalogue]
    listeners: list[ProgressCallback] = field(default_factory=list)
    waiters: int = 0


async def _notify(callbacks: Sequence[ProgressCallback], done: int, message: str) -> None:
    """Call every progress callback, ignoring the ones that fail."""
    # Progress is a courtesy. A callback can fail because the client that wanted it went away,
    # and that must not abort a 30-second download other callers are waiting for.
    for callback in list(callbacks):
        try:
            await callback(done, None, message)
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
        if download is None:
            listeners: list[ProgressCallback] = []
            task = asyncio.create_task(self._download_all(listeners))
            download = _Download(task, listeners)
            self._download = download
            task.add_done_callback(lambda done, download=download: self._land(download, done))
        if progress is not None:
            download.listeners.append(progress)
        download.waiters += 1
        try:
            # Same ownership scheme as ``BoeClient._fetch_shared``: the task belongs to the
            # download, not to the caller that started it, and callers await it through
            # ``shield`` so cancelling one caller cancels only its own wait.
            return await asyncio.shield(download.task)
        finally:
            download.waiters -= 1
            if progress is not None:
                download.listeners.remove(progress)
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
        ``ValueError`` for a malformed subject code.
        """
        if text is None and subject_code is None:
            return list((await self.get(progress=progress)).laws)
        query = build_search_query(text=text, subject_code=subject_code, sort_by="published_on")
        callbacks = [] if progress is None else [progress]
        # ``ttl=None``: a selection is specific to this request, so the normal 30-minute search
        # lifetime applies rather than the catalogue's day.
        return await self._crawl(query, None, callbacks, "Searching")

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

    async def _download_all(self, listeners: list[ProgressCallback]) -> Catalogue:
        """Download the whole collection and store it as the snapshot."""
        # Sorted by publication date, which never changes for a record, unlike the default
        # update-date order: a law updated mid-crawl would otherwise jump between pages
        # (api-notes, "Bulk download").
        query = build_search_query(sort_by="published_on")
        laws = await self._crawl(query, CATALOGUE_TTL, listeners, "Downloading the catalogue")
        updates = [law.updated_at for law in laws if law.updated_at is not None]
        snapshot = Catalogue(
            laws=tuple(laws), fetched_at=self._clock(), newest_update=max(updates, default=None)
        )
        # Set only when the whole download succeeded, so a failure or cancellation leaves the
        # previous state (no snapshot) and the next caller retries.
        self._snapshot = snapshot
        return snapshot

    async def _crawl(
        self, query: str | None, ttl: float | None, listeners: list[ProgressCallback], label: str
    ) -> list[LawSummary]:
        """Page through ``query`` until a short page; return the laws without duplicates.

        Raises ``BoeResponseError`` when ``MAX_CATALOGUE_PAGES`` full pages come back.
        """
        laws: list[LawSummary] = []
        seen: set[str] = set()
        for page_number in range(MAX_CATALOGUE_PAGES):
            # The offset counts raw API positions, duplicates included, so that no record is
            # skipped when an earlier one was added or removed during the crawl.
            page = await self._client.search_laws(
                query, offset=page_number * CATALOGUE_PAGE_SIZE, limit=CATALOGUE_PAGE_SIZE, ttl=ttl
            )
            for law in page:
                # A record added mid-crawl shifts everything after it by one, so the last
                # record of one page reappears at the start of the next: keep the first copy.
                if law.identifier not in seen:
                    seen.add(law.identifier)
                    laws.append(law)
            await _notify(listeners, len(laws), f"{label}: {len(laws)} laws so far")
            if len(page) < CATALOGUE_PAGE_SIZE:
                return laws
        raise BoeResponseError(
            f"BOE returned more than {MAX_CATALOGUE_PAGES * CATALOGUE_PAGE_SIZE} records"
        )
