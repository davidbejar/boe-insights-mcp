"""Use cases of the five saved-search tools: save, list, check, edit and delete.

A saved search is a stored filter plus a bookmark. The server cannot alert anyone: nothing
happens until the model calls ``check_saved_searches``, which reads what the BOE published since
the bookmark and moves it. Two kinds exist: ``gazette`` (title words, section and department over
the daily summaries, reusing ``gazette.GazetteFilter``) and ``law`` (new amendments to one
consolidated law, reusing ``chains.relation_kind``).

Layering: ``server`` calls these functions with the owner from ``owner`` -> ``searchstore`` for
the rows, ``client`` for BOE data -> ``schemas`` for the results. Like the other use-case
modules, ``now`` and ``today`` exist so that tests can fix the clock, and the time budget of a
check takes an injected monotonic clock. Errors a caller can fix are ``ValueError``, which the
server turns into an "Invalid argument" tool error.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Protocol

from boe_insights_mcp.catalogue import ProgressCallback
from boe_insights_mcp.chains import AMENDING_KINDS, RelationKind, relation_kind
from boe_insights_mcp.clock import madrid_today, midnight_utc, utc_now
from boe_insights_mcp.errors import BoeError, BoeNotFoundError
from boe_insights_mcp.gazette import (
    TODAY_MISSING_NOTE,
    GazetteFilter,
    scanned_entry,
)
from boe_insights_mcp.identifiers import normalise_identifier
from boe_insights_mcp.models import (
    DailySummary,
    LawAnalysis,
    LawMetadata,
    LawReference,
    ellipsize,
    strip_or_none,
)
from boe_insights_mcp.naming import gazette_base, law_base, propose_name, slugify
from boe_insights_mcp.schemas import (
    GAZETTE_NOTICE,
    CheckSearchesResult,
    DeleteSearchResult,
    Provenance,
    SavedSearchInfo,
    SavedSearchList,
    SaveSearchResult,
    ScannedEntry,
    SearchChange,
    SearchCheck,
    SearchStatus,
)
from boe_insights_mcp.searchstore import (
    DuplicateNameError,
    SavedSearch,
    SavedSearchStore,
    SearchLimitError,
    SearchNotFoundError,
)

__all__ = [
    "CHECK_TIME_LIMIT",
    "DEFAULT_CHECK_DAYS",
    "DEFAULT_ITEMS",
    "MAX_ITEMS",
    "MAX_SEARCHES",
    "MAX_SINCE_DAYS",
    "REPORTED_KINDS",
    "SavedSearchClient",
    "check_saved_searches",
    "delete_saved_search",
    "edit_saved_search",
    "list_saved_searches",
    "save_search",
]

MAX_SEARCHES = 20
DEFAULT_CHECK_DAYS = 31
MAX_SINCE_DAYS = 90
# Like ``gazette.SCAN_TIME_LIMIT``: no uncached request starts when under ``_TIME_MARGIN``
# seconds remain, because a request that starts late can still take several seconds to answer.
CHECK_TIME_LIMIT = 40.0
_TIME_MARGIN = 5.0
DEFAULT_ITEMS = 20
MAX_ITEMS = 50
REPORTED_KINDS: frozenset[RelationKind] = AMENDING_KINDS | frozenset[RelationKind](["corrects"])
"""The relation kinds reported as changes; court rulings and citations are only counted."""

_MAX_TEXT = 200
_NO_SEARCHES = "No saved searches. Create one with save_search."
_NO_FILTER = "give at least one of words, section or department_code"
_NO_LAW_FILTER = "a law search can only be renamed; save a new search for another law"
_YEAR_IN_ID = re.compile(r"-(\d{4})-")


class SavedSearchClient(Protocol):
    """The client methods the saved searches need; ``BoeClient`` satisfies it."""

    async def get_law_metadata(self, law_id: str) -> LawMetadata:
        """Metadata of one law (title, rank, official number)."""
        ...

    async def get_law_analysis(self, law_id: str) -> LawAnalysis:
        """Subjects and amendment references of one law."""
        ...

    async def has_cached_analysis(self, law_id: str) -> bool:
        """Whether the law's analysis is cached (no request)."""
        ...

    async def get_daily_summary(self, day: date, *, today: date) -> DailySummary | None:
        """The summary of one day, or ``None`` when there was no issue."""
        ...

    async def has_cached_summary(self, day: date) -> bool:
        """Whether the day's summary (or remembered absence) is cached (no request)."""
        ...


# --- shared helpers ----------------------------------------------------------------------------


def _check_limit(limit: int) -> None:
    """Raise ``ValueError`` for a limit outside 1..``MAX_ITEMS``."""
    if not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_ITEMS}")


def _info(search: SavedSearch) -> SavedSearchInfo:
    """The model-facing description of a stored search."""
    return SavedSearchInfo(
        name=search.name,
        kind=search.kind,
        words=search.words,
        section=search.section,
        department_code=search.department_code,
        law_id=search.law_id,
        law_title=ellipsize(search.law_title, _MAX_TEXT) if search.law_title else None,
        created_at=search.created_at,
        last_checked_at=search.last_checked_at,
        checked_through=search.checked_through,
    )


def _not_found(slug: str) -> ValueError:
    """The error for a name the owner has not saved."""
    return ValueError(f"no saved search named '{slug}'; list_saved_searches shows the names")


def _duplicate(slug: str) -> ValueError:
    """The error for a name the owner already uses."""
    return ValueError(f"a saved search named '{slug}' already exists; choose another name")


def _limit_reached() -> ValueError:
    """The error for an owner who has the most searches allowed."""
    return ValueError(f"the limit of {MAX_SEARCHES} saved searches is reached; delete one first")


def _given_name(name: str) -> str:
    """Slugify a name the caller chose; ``ValueError`` when nothing is left of it."""
    slug = slugify(name)
    if not slug:
        raise ValueError("name must contain letters or digits")
    return slug


def _normalised(
    words: str | None, section: str | None, department: str | None
) -> tuple[str | None, str | None, str | None]:
    """Blank filters become ``None``; the section is upper-cased (as ``GazetteFilter`` does)."""
    section = strip_or_none(section)
    return strip_or_none(words), section.upper() if section else None, strip_or_none(department)


def _reference_key(reference: LawReference) -> str:
    """The identity of a reference in ``seen``: the acting norm, the relation code and the text.

    The same norm can act on a law several times (other articles, other relations), and each of
    those is a separate event, so the norm's identifier alone would hide the later ones.
    """
    return f"{reference.law_id}|{reference.relation.code}|{reference.text}"


# --- save_search -------------------------------------------------------------------------------


async def save_search(
    store: SavedSearchStore,
    client: SavedSearchClient,
    owner: str,
    *,
    name: str | None = None,
    words: str | None = None,
    section: str | None = None,
    department_code: str | None = None,
    law_id: str | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> SaveSearchResult:
    """Save a gazette search (words, section, department) or a law search (``law_id``).

    A gazette search makes no request: its bookmark is today, so the first check reports what
    is published after today. A law search reads the law once to remember every amendment it
    already has. Raises ``ValueError`` for bad input, a taken name or the limit;
    ``BoeNotFoundError`` propagates for an unknown law.
    """
    moment = utc_now(now)
    current = madrid_today(moment, today)
    words, section, department_code = _normalised(words, section, department_code)
    law = strip_or_none(law_id)
    has_filter = any(value is not None for value in (words, section, department_code))
    if (law is None) == (not has_filter):
        raise ValueError("give either law_id, or at least one of words, section or department_code")
    slug = _given_name(name) if name is not None else None
    taken = await store.names(owner)
    # Checked before any request: a full owner must not cost a law's two downloads.
    if len(taken) >= MAX_SEARCHES:
        raise _limit_reached()
    if slug is not None and slug in taken:
        raise _duplicate(slug)

    if law is None:
        # Built for its validation only: words without letters or digits would match everything.
        GazetteFilter(words, section, department_code)
        base = gazette_base(words, section, department_code)
        search = SavedSearch(
            owner=owner,
            name="",
            kind="gazette",
            words=words,
            section=section,
            department_code=department_code,
            # Saving is silent (no request): the first check reports what is dated after today.
            checked_through=current,
            created_at=moment,
        )
        first_check = f"The first check reports issues dated after {current.isoformat()}."
    else:
        # A malformed identifier is passed through so that the client raises its own ValueError.
        identifier = normalise_identifier(law) or law
        metadata = await client.get_law_metadata(identifier)
        analysis = await client.get_law_analysis(identifier)
        rank = metadata.rank.text if metadata.rank else None
        base = law_base(rank, metadata.official_number, identifier)
        search = SavedSearch(
            owner=owner,
            name="",
            kind="law",
            law_id=identifier,
            law_title=ellipsize(metadata.title, _MAX_TEXT),
            # The baseline: what the law already has is not news.
            seen=frozenset(_reference_key(ref) for ref in analysis.later),
            created_at=moment,
        )
        first_check = "The first check reports changes recorded after now."

    proposed = slug is None
    final = slug if slug is not None else propose_name(base, taken)
    try:
        await store.add(replace(search, name=final))
    except DuplicateNameError:
        raise _duplicate(final) from None
    except SearchLimitError:
        raise _limit_reached() from None
    note = first_check
    if proposed:
        note = (
            f"Saved as '{final}'; rename it with edit_saved_search if you prefer another name. "
            f"{first_check}"
        )
    return SaveSearchResult(
        search=_info(replace(search, name=final)),
        name_was_proposed=proposed,
        total=len(taken) + 1,
        note=note,
        provenance=Provenance(generated_at=moment),
    )


# --- list, edit, delete ------------------------------------------------------------------------


async def list_saved_searches(
    store: SavedSearchStore, owner: str, *, now: datetime | None = None
) -> SavedSearchList:
    """List the owner's saved searches, oldest first."""
    searches = await store.list(owner)
    return SavedSearchList(
        searches=[_info(search) for search in searches],
        total=len(searches),
        limit=MAX_SEARCHES,
        note=None if searches else _NO_SEARCHES,
        provenance=Provenance(generated_at=utc_now(now)),
    )


async def edit_saved_search(
    store: SavedSearchStore,
    owner: str,
    name: str,
    *,
    new_name: str | None = None,
    words: str | None = None,
    section: str | None = None,
    department_code: str | None = None,
    now: datetime | None = None,
) -> SaveSearchResult:
    """Rename a search or change the filters of a gazette search; the bookmark is kept.

    ``None`` leaves a value unchanged; a blank string clears a filter. Raises ``ValueError`` for
    an unknown name, nothing to change, a law search given filters, a gazette search left with
    no filter, or a new name that is invalid or taken.
    """
    slug = slugify(name)
    try:
        current = await store.get(owner, slug)
    except SearchNotFoundError:
        raise _not_found(slug) from None
    renamed = _given_name(new_name) if new_name is not None else current.name
    changed = current
    if current.kind == "law":
        # A law search is the law plus the references seen so far; other filters have no meaning.
        # "" counts as given: it asks to clear a filter this kind does not have.
        if any(value is not None for value in (words, section, department_code)):
            raise ValueError(_NO_LAW_FILTER)
    else:
        changed = replace(
            current,
            words=_edited(words, current.words),
            section=_edited(section, current.section, upper=True),
            department_code=_edited(department_code, current.department_code),
        )
        wanted = GazetteFilter(changed.words, changed.section, changed.department_code)
        if wanted.is_empty:
            raise ValueError(_NO_FILTER)
    changed = replace(changed, name=renamed)
    if changed == current:
        raise ValueError("nothing to change")
    taken = await store.names(owner)
    if renamed != current.name and renamed in taken:
        raise _duplicate(renamed)
    try:
        await store.replace(owner, current.name, changed)
    except DuplicateNameError:
        raise _duplicate(renamed) from None
    except SearchNotFoundError:
        raise _not_found(slug) from None
    return SaveSearchResult(
        search=_info(changed),
        name_was_proposed=False,
        total=len(taken),
        note=None,
        provenance=Provenance(generated_at=utc_now(now)),
    )


def _edited(value: str | None, stored: str | None, *, upper: bool = False) -> str | None:
    """The new value of a filter: unchanged for ``None``, cleared for a blank, else the text."""
    if value is None:
        return stored
    value = value.strip()
    if not value:
        return None
    return value.upper() if upper else value


async def delete_saved_search(
    store: SavedSearchStore, owner: str, name: str, *, now: datetime | None = None
) -> DeleteSearchResult:
    """Delete one saved search; raises ``ValueError`` for an unknown name."""
    slug = slugify(name)
    try:
        await store.delete(owner, slug)
    except SearchNotFoundError:
        raise _not_found(slug) from None
    return DeleteSearchResult(
        deleted=slug,
        remaining=len(await store.names(owner)),
        provenance=Provenance(generated_at=utc_now(now)),
    )


# --- check_saved_searches ----------------------------------------------------------------------


@dataclass
class _Reading:
    """The days read once for all gazette searches of one check."""

    summaries: dict[date, DailySummary] = field(default_factory=dict)
    """Days that had an issue."""
    empty: set[date] = field(default_factory=set)
    """Past days with no issue (read, nothing in them)."""
    failed: set[date] = field(default_factory=set)
    """Days skipped after an error that retrying cannot fix."""
    today_missing: bool = False
    """Today answered "no issue": not read, because it may just not be out yet."""

    def counts(self, day: date) -> bool:
        """Whether ``day`` is part of a contiguous run (read, empty, or skipped for good)."""
        return day in self.summaries or day in self.empty or day in self.failed


@dataclass
class _Tally:
    """Progress counting and the time budget shared by the laws and the days of one check."""

    total: int
    progress: ProgressCallback | None
    clock: Callable[[], float]
    deadline: float
    done: int = 0

    def out_of_time(self) -> bool:
        """Whether no new uncached request may start."""
        return self.clock() >= self.deadline

    async def step(self) -> None:
        """Count one law or day and report it."""
        self.done += 1
        if self.progress is not None:
            await self.progress(
                self.done, self.total, f"Read {self.done} of {self.total} laws and days."
            )


def _validate_since(since: date | None, today: date) -> None:
    """Raise ``ValueError`` for a ``since`` in the future or 90 or more days back."""
    if since is None:
        return
    if since > today:
        raise ValueError("since is in the future")
    if (today - since).days >= MAX_SINCE_DAYS:
        raise ValueError(f"since may be at most {MAX_SINCE_DAYS} days back")


def _year_of(law_id: str) -> int | None:
    """The year in an identifier such as ``BOE-A-2020-3692``, or ``None`` when it has none."""
    found = _YEAR_IN_ID.search(law_id)
    return int(found.group(1)) if found else None


async def check_saved_searches(
    store: SavedSearchStore,
    client: SavedSearchClient,
    owner: str,
    *,
    name: str | None = None,
    since: date | None = None,
    limit: int = DEFAULT_ITEMS,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit: float = CHECK_TIME_LIMIT,
) -> CheckSearchesResult:
    """Report what is new for the owner's saved searches and move their bookmarks.

    Law searches are read first (one analysis each), then the gazette days of all gazette
    searches once, oldest first. No uncached request starts once ``time_limit`` seconds minus a
    5 s margin have passed; a search that could not be finished is ``partial`` and a later call
    continues from its bookmark. With ``since`` nothing stored changes. Raises ``ValueError`` for
    a limit outside 1..``MAX_ITEMS``, an unknown ``name`` or a ``since`` out of range.
    """
    # Started first, as in ``scan_gazette``: the client bounds the whole call.
    deadline = clock() + time_limit - _TIME_MARGIN
    _check_limit(limit)
    moment = utc_now(now)
    current = madrid_today(moment, today)
    _validate_since(since, current)
    searches = await store.list(owner)
    if name is not None:
        slug = slugify(name)
        searches = [search for search in searches if search.name == slug]
        if not searches:
            raise _not_found(slug)
    if not searches:
        return CheckSearchesResult(
            checked=0,
            total=0,
            complete=True,
            results=[],
            note=_NO_SEARCHES,
            provenance=Provenance(generated_at=moment),
        )

    ranges = {
        search.name: _range(search, since, current)
        for search in searches
        if search.kind == "gazette"
    }
    union = sorted(
        {
            day
            for span in ranges.values()
            if span.start <= span.end
            for day in _days(span.start, span.end)
        }
    )
    laws = [search for search in searches if search.kind == "law"]
    tally = _Tally(len(laws) + len(union), progress, clock, deadline)

    outcomes: dict[str, tuple[SearchCheck, SavedSearch | None]] = {}
    for search in laws:
        outcomes[search.name] = await _check_law(
            client, search, since=since, limit=limit, moment=moment, tally=tally
        )
    reading = await _read_days(client, union, current, tally)
    for search in searches:
        if search.kind == "gazette":
            outcomes[search.name] = _check_gazette(
                search,
                ranges[search.name],
                reading,
                limit=limit,
                moment=moment,
                today=current,
            )

    results: list[SearchCheck] = []
    for search in searches:
        result, updated = outcomes[search.name]
        # With ``since`` the outcome carries no new row, so nothing stored changes.
        if updated is not None and since is None:
            await store.replace(owner, search.name, updated)
        results.append(result)
    checked = sum(result.status == "checked" for result in results)
    notes: list[str] = []
    if checked < len(results):
        notes.append(
            f"{checked} of {len(results)} saved searches fully checked. Call again to continue."
        )
    if reading.today_missing:
        notes.append(TODAY_MISSING_NOTE)
    newest = max(reading.summaries, default=None)
    reads_gazette = bool(ranges)
    return CheckSearchesResult(
        checked=checked,
        total=len(results),
        complete=checked == len(results),
        results=results,
        note=" ".join(notes) or None,
        provenance=Provenance(
            generated_at=moment,
            data_updated_at=midnight_utc(newest),
            **({"notice": GAZETTE_NOTICE} if reads_gazette else {}),
        ),
    )


def _days(start: date, end: date) -> list[date]:
    """Every day from ``start`` to ``end``, inclusive, oldest first."""
    return [start + timedelta(days=step) for step in range((end - start).days + 1)]


# -- law searches --


async def _check_law(
    client: SavedSearchClient,
    search: SavedSearch,
    *,
    since: date | None,
    limit: int,
    moment: datetime,
    tally: _Tally,
) -> tuple[SearchCheck, SavedSearch | None]:
    """Check one law search; returns its result and the row to store (``None``: leave as is)."""
    law_id = search.law_id or ""

    def unread(status: SearchStatus, note: str) -> tuple[SearchCheck, None]:
        """The result for a law that could not be read: nothing is reported or stored."""
        check = SearchCheck(
            name=search.name,
            kind="law",
            status=status,
            law_id=law_id,
            law_title=search.law_title,
            changes=[],
            changes_total=0,
            other_new=0,
            note=note,
        )
        return check, None

    try:
        # A cached analysis is free, so it never counts against the budget; an uncached one is
        # not requested once the deadline has passed and the law waits for the next call.
        if not await client.has_cached_analysis(law_id) and tally.out_of_time():
            return unread("partial", "Not read yet; call again.")
        analysis = await client.get_law_analysis(law_id)
    except BoeNotFoundError:
        return unread("failed", "The BOE no longer has this law.")
    except BoeError as error:
        # Never the API's own words: they are not ours to relay and mean nothing to the user.
        if error.retryable:
            return unread("partial", "Could not be read; try again.")
        return unread("failed", "Could not be read.")
    finally:
        await tally.step()

    if since is not None:
        return _law_since(search, analysis, since, limit), None
    return _law_new(search, analysis, limit, moment)


def _change(reference: LawReference) -> SearchChange:
    """The model-facing form of one reported reference."""
    return SearchChange(
        law_id=reference.law_id,
        kind=relation_kind(reference.relation.code),
        relation=reference.relation.text,
        text=ellipsize(reference.text, _MAX_TEXT),
    )


def _law_since(search: SavedSearch, analysis: LawAnalysis, since: date, limit: int) -> SearchCheck:
    """A law's changes whose amending norm is from ``since``'s year or later (nothing stored)."""
    # The references carry no date, only the amending norm's identifier, whose year is the
    # nearest thing: so ``since`` works by year for a law (and references without one are left out).
    found = [
        reference
        for reference in analysis.later
        if relation_kind(reference.relation.code) in REPORTED_KINDS
        and (year := _year_of(reference.law_id)) is not None
        and year >= since.year
    ]
    notes = []
    if len(found) > limit:
        notes.append(f"Showing {limit} of {len(found)} changes.")
    notes.append("For a law, since works by the year of the amending norm.")
    return SearchCheck(
        name=search.name,
        kind="law",
        law_id=search.law_id,
        law_title=search.law_title,
        status="checked",
        changes=[_change(reference) for reference in found[:limit]],
        changes_total=len(found),
        other_new=0,
        note=" ".join(notes),
    )


def _law_new(
    search: SavedSearch,
    analysis: LawAnalysis,
    limit: int,
    moment: datetime,
) -> tuple[SearchCheck, SavedSearch]:
    """The references not in ``seen``, and the row with ``seen`` grown by what was reported."""
    fresh: dict[str, LawReference] = {}
    for reference in analysis.later:
        key = _reference_key(reference)
        if key not in search.seen:
            fresh.setdefault(key, reference)
    reported = [
        (key, reference)
        for key, reference in fresh.items()
        if relation_kind(reference.relation.code) in REPORTED_KINDS
    ]
    others = [key for key, reference in fresh.items() if (key, reference) not in reported]
    shown = reported[:limit]
    # Why only the shown changes are remembered: the rest were never put in front of the reader,
    # so marking them seen would lose them for good; they stay new for the next call.
    # Why ``seen`` only grows: a reference that vanishes from the analysis and comes back (a
    # correction of the BOE's data) must not be reported twice.
    seen = search.seen | {key for key, _ in shown} | set(others)
    cut = len(reported) > len(shown)
    check = SearchCheck(
        name=search.name,
        kind="law",
        law_id=search.law_id,
        law_title=search.law_title,
        status="partial" if cut else "checked",
        changes=[_change(reference) for _, reference in shown],
        changes_total=len(reported),
        other_new=len(others),
        note=f"Showing {len(shown)} of {len(reported)} changes; call again for the rest."
        if cut
        else None,
    )
    return check, replace(search, seen=seen, last_checked_at=moment)


# -- gazette searches --


@dataclass(frozen=True)
class _Range:
    """The days one gazette search has to read, and the note about days skipped before them."""

    start: date
    end: date
    skipped: int = 0


def _range(search: SavedSearch, since: date | None, today: date) -> _Range:
    """The days to read for a gazette search: after its bookmark, or from ``since``."""
    if since is not None:
        return _Range(since, today)
    through = search.checked_through or today
    start = through + timedelta(days=1)
    earliest = today - timedelta(days=DEFAULT_CHECK_DAYS - 1)
    if start < earliest:
        # A bookmark months old would mean a hundred requests; the cap keeps a check bounded and
        # the note says what was left out (``since`` reads further back without moving anything).
        return _Range(earliest, today, skipped=(earliest - start).days)
    return _Range(start, today)


async def _read_days(
    client: SavedSearchClient, days: list[date], today: date, tally: _Tally
) -> _Reading:
    """Read ``days`` (oldest first) once, stopping where reading cannot go on.

    Oldest first, unlike ``scan_gazette`` (newest first): a bookmark can only move over an
    unbroken run of read days, so the days nearest the bookmark must be read before any later
    one, and stopping early (budget, a temporary error) leaves nothing unread in the middle.
    """
    reading = _Reading()
    for day in days:
        cached = await client.has_cached_summary(day)
        if not cached and tally.out_of_time():
            break
        try:
            found = await client.get_daily_summary(day, today=today)
        except BoeError as error:
            if error.retryable:
                break
            # A day that fails for good would fail again, so the bookmark passes it (counted).
            reading.failed.add(day)
        else:
            if found is not None:
                reading.summaries[day] = found
            elif day == today:
                # Today's missing issue is not remembered by the client and means "not out yet"
                # as often as "no issue", so it is not read and never moves the bookmark.
                reading.today_missing = True
            else:
                reading.empty.add(day)
        await tally.step()
    return reading


def _check_gazette(
    search: SavedSearch,
    span: _Range,
    reading: _Reading,
    *,
    limit: int,
    moment: datetime,
    today: date,
) -> tuple[SearchCheck, SavedSearch | None]:
    """The result of one gazette search over the days read, and the row with its bookmark moved."""
    if span.start > span.end:
        through = search.checked_through
        check = SearchCheck(
            name=search.name,
            kind="gazette",
            status="checked",
            days_pending=0,
            days_failed=0,
            matched=0,
            items=[],
            has_more=False,
            note=f"Already checked through {through.isoformat() if through else today}.",
        )
        return check, replace(search, last_checked_at=moment)

    # The run: consecutive days from the start that were read (or skipped for good).
    run: list[date] = []
    for day in _days(span.start, span.end):
        if not reading.counts(day):
            break
        run.append(day)
    last = run[-1] if run else None
    # Today without an issue is not "pending": the range is done once it reaches yesterday, but
    # the bookmark stays there so that the next check asks for today again.
    target = (
        span.end - timedelta(days=1) if reading.today_missing and span.end == today else span.end
    )
    done = (last is not None and last >= target) or span.start > target
    status: SearchStatus = "checked" if done else "partial"
    pending = 0 if done else (span.end - last).days if last else (span.end - span.start).days + 1
    skipped_days = [day for day in run if day in reading.failed]

    wanted = GazetteFilter(search.words, search.section, search.department_code)
    matches: list[ScannedEntry] = []
    for day in reversed(run):
        found = reading.summaries.get(day)
        if found is None:
            continue
        matches.extend(
            scanned_entry(day, item, several_issues=len(found.issues) > 1)
            for item in found.items
            if wanted.matches(item)
        )
    shown = matches[:limit]
    notes: list[str] = []
    if span.skipped:
        notes.append(
            f"Only the last {DEFAULT_CHECK_DAYS} days were checked; {span.skipped} earlier "
            f"{'day was' if span.skipped == 1 else 'days were'} skipped. "
            f"Pass since to read up to {MAX_SINCE_DAYS} days back."
        )
    if run and len(matches) > len(shown):
        notes.append(
            f"Showing {len(shown)} of {len(matches)}; scan_gazette with the same filters from "
            f"{run[0].isoformat()} to {run[-1].isoformat()} lists them all."
        )
    if skipped_days:
        notes.append(
            f"{len(skipped_days)} day{'s' if len(skipped_days) != 1 else ''} could not be read "
            "and "
            f"{'were' if len(skipped_days) != 1 else 'was'} skipped."
        )
    check = SearchCheck(
        name=search.name,
        kind="gazette",
        status=status,
        from_date=run[0] if run else None,
        to_date=last,
        days_pending=pending,
        days_failed=len(skipped_days),
        matched=len(matches),
        items=shown,
        has_more=len(matches) > len(shown),
        note=" ".join(notes) or None,
    )
    if last is None and not done:
        # Nothing was read (budget or a temporary error): there is no new bookmark to store.
        return check, None
    through = search.checked_through
    if last is not None and (through is None or last > through):
        # Never earlier than it was; ``since`` ranges can end before the bookmark.
        through = last
    return check, replace(search, checked_through=through, last_checked_at=moment)
