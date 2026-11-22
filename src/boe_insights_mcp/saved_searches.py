"""Use cases of the five saved-search tools: save, list, check, edit and delete.

A saved search is a stored filter plus what it remembers about what it already delivered. The
server cannot alert anyone: nothing happens until the model calls ``check_saved_searches``. Two
kinds exist: ``gazette`` (title words, section and department over the daily summaries, reusing
``gazette.GazetteFilter``) and ``law`` (new amendments to one consolidated law, reusing
``chains.relation_kind``).

A gazette search remembers ``checked_through`` (every day up to it is finished) and, for the days
after it, the identifiers already delivered (``seen_items``). A day is too coarse a unit for
that alone: today's issue can grow after it was read, a ``limit`` can cut inside a day, and a
recent "no issue" may be a late publication. A law search remembers the references it reported
(``seen``). Delivery is at most once: what a check shows is marked as shown before the model has
it, so a result lost in transit is recovered only with ``since``, which re-reads and marks
nothing.

Layering: ``server`` calls these functions with the owner from ``owner`` -> ``searchstore`` for
the rows, ``client`` for BOE data -> ``schemas`` for the results. Like the other use-case
modules, ``now`` and ``today`` exist so that tests can fix the clock, and the time budget of a
check takes an injected monotonic clock. Errors a caller can fix are ``ValueError``, which the
server turns into an "Invalid argument" tool error.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Protocol

from boe_insights_mcp.catalogue import ProgressCallback
from boe_insights_mcp.chains import AMENDING_KINDS, RelationKind, relation_kind
from boe_insights_mcp.client import SUMMARY_ABSENCE_SETTLED_DAYS
from boe_insights_mcp.clock import madrid_today, midnight_utc, utc_now
from boe_insights_mcp.errors import BoeError, BoeNotFoundError
from boe_insights_mcp.gazette import (
    MAX_SCAN_DAYS,
    TODAY_MISSING_NOTE,
    GazetteFilter,
    scanned_entry,
)
from boe_insights_mcp.identifiers import normalise_identifier
from boe_insights_mcp.models import (
    DailySummary,
    GazetteItem,
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
    "MAX_TOTAL_ITEMS",
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
MAX_TOTAL_ITEMS = 60
"""Items and changes delivered per call over all searches, so that a result stays readable."""
MAX_WORDS = 200
REPORTED_KINDS: frozenset[RelationKind] = AMENDING_KINDS | frozenset[RelationKind](["corrects"])
"""The relation kinds reported as changes; court rulings and citations are only counted."""

_MAX_TEXT = 200
_NO_SEARCHES = "No saved searches. Create one with save_search."
_NO_FILTER = "give at least one of words, section or department_code"
_NO_LAW_FILTER = "a law search can only be renamed; save a new search for another law"
_NOT_REACHED_NOTE = "Not reached in this call; call again."
_CHANGED_NOTE = "Changed during the check; nothing was marked as seen."
_LAW_SINCE_NOTE = (
    "since applies to gazette searches; get_amendment_history lists a law's past amendments."
)
_BOTH_NOTICE = (
    "Gazette items are summaries of the official gazette; only the texts published in the BOE "
    "itself are authentic. Consolidated texts are provided for information only and have no "
    "official legal value."
)
_SUNDAY = 6
_NAME_ATTEMPTS = 5


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

    async def has_cached_summary(self, day: date, *, today: date) -> bool:
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
    words = _checked_words(strip_or_none(words))
    section = strip_or_none(section)
    return words, section.upper() if section else None, strip_or_none(department)


def _checked_words(words: str | None) -> str | None:
    """Return ``words`` unchanged; ``ValueError`` when longer than ``MAX_WORDS`` characters."""
    if words is not None and len(words) > MAX_WORDS:
        raise ValueError(f"words must be at most {MAX_WORDS} characters")
    return words


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
        # The baseline: what today's issue already has is not news. Everything in it is
        # remembered, not only what matches now, so that editing the filter later does not bring
        # it back. The bookmark is yesterday, so that an issue published after saving, even
        # later the same day, is reported by the first check.
        seen_items: dict[date, frozenset[str]] = {}
        try:
            todays = await client.get_daily_summary(current, today=current)
        except BoeError:
            todays = None
            unread_today = (
                "Today's issue could not be read; the first check may include items "
                "published earlier today."
            )
        else:
            unread_today = None
        if todays is not None:
            seen_items[current] = frozenset(item.identifier for item in todays.items)
        search = SavedSearch(
            owner=owner,
            name="",
            kind="gazette",
            words=words,
            section=section,
            department_code=department_code,
            checked_through=current - timedelta(days=1),
            seen_items=seen_items,
            created_at=moment,
        )
        first_check = "The first check reports what is published from now on."
        if unread_today:
            first_check = f"{first_check} {unread_today}"
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
        first_check = (
            "The first check reports changes recorded from now on "
            "(the BOE data read can be up to 6 hours old)."
        )

    proposed = slug is None
    final = slug if slug is not None else propose_name(base, taken)
    for attempt in range(_NAME_ATTEMPTS):
        try:
            await store.add(replace(search, name=final))
            break
        except DuplicateNameError:
            # A name the caller chose is theirs to change. A proposed one may have been taken a
            # moment ago by a save that ran at the same time: propose again from what is stored.
            if not proposed or attempt == _NAME_ATTEMPTS - 1:
                raise _duplicate(final) from None
            taken = await store.names(owner)
            final = propose_name(base, taken)
        except SearchLimitError:
            raise _limit_reached() from None
    notes = []
    if proposed:
        notes.append(
            f"Saved as '{final}'; rename it with edit_saved_search if you prefer another name."
        )
    notes.append(first_check)
    if store.in_memory:
        notes.append("Stored in memory only: it is lost when the server stops.")
    return SaveSearchResult(
        search=_info(replace(search, name=final)),
        name_was_proposed=proposed,
        total=len(await store.names(owner)),
        note=" ".join(notes),
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
            words=_checked_words(_edited(words, current.words)),
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
    """Days that answered "no issue" (today included: it may just not be out yet)."""
    failed: set[date] = field(default_factory=set)
    """Days skipped after an error that retrying cannot fix."""

    def read(self, day: date) -> bool:
        """Whether the BOE answered for ``day`` (an issue, no issue, or a permanent error)."""
        return day in self.summaries or day in self.empty or day in self.failed

    def settled(self, day: date, today: date) -> bool:
        """Whether what was read for ``day`` can be trusted to be final.

        Today never is: its issue may still grow (docs/api-notes.md records a day with two
        issues). A past day that had an issue is, and so is one skipped for a permanent error.
        A past "no issue" is final only when it is a Sunday or old enough that the client
        itself remembers it (``SUMMARY_ABSENCE_SETTLED_DAYS``): a recent weekday's 404 can be
        a late publication or a transient error, and trusting it would lose that issue.
        """
        if day >= today:
            return False
        if day in self.summaries or day in self.failed:
            return True
        return day.weekday() == _SUNDAY or (today - day).days > SUMMARY_ABSENCE_SETTLED_DAYS


@dataclass(frozen=True)
class _Progress:
    """The progress columns a check wants to store for one search."""

    checked_through: date | None
    seen: frozenset[str]
    seen_items: Mapping[date, frozenset[str]]
    last_checked_at: datetime


@dataclass(frozen=True)
class _Outcome:
    """The result for one search, the progress to store (if any) and the items it used up."""

    check: SearchCheck
    progress: _Progress | None = None
    used: int = 0


@dataclass(frozen=True)
class _Unread:
    """A law that could not be read this time, and why."""

    status: SearchStatus
    note: str


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
    """Raise ``ValueError`` for a ``since`` in the future or more than 90 days back."""
    if since is None:
        return
    if since > today:
        raise ValueError("since is in the future")
    if (today - since).days > MAX_SINCE_DAYS:
        raise ValueError(f"since may be at most {MAX_SINCE_DAYS} days back")


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
    """Report what is new for the owner's saved searches and remember what was delivered.

    Law searches are read first (one analysis each), then the gazette days of all gazette
    searches once, oldest first. No uncached request starts once ``time_limit`` seconds minus a
    5 s margin have passed; a search that could not be finished is ``partial`` and a later call
    continues from what it remembers. At most ``limit`` items per search and ``MAX_TOTAL_ITEMS``
    in all are delivered per call, searches being served in list order. With ``since`` gazette
    searches are re-read from that day and nothing stored changes. Raises ``ValueError`` for a
    limit outside 1..``MAX_ITEMS``, an unknown ``name`` or a ``since`` out of range.
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
    # ``since`` does not read laws: their references carry no date (see ``_law_outcome``).
    laws = [] if since is not None else [search for search in searches if search.kind == "law"]
    tally = _Tally(len(laws) + len(union), progress, clock, deadline)
    analyses = {search.name: await _read_law(client, search, tally) for search in laws}
    reading = await _read_days(client, union, current, tally)

    # The items are shared out only now, in list order, whatever the order the BOE was read in:
    # the search that comes first in the list is the one served first when the total runs out.
    remaining = MAX_TOTAL_ITEMS
    results: list[SearchCheck] = []
    for search in searches:
        allowance = min(limit, remaining)
        if search.kind == "law":
            outcome = _law_outcome(
                search,
                analyses.get(search.name),
                since=since,
                allowance=allowance,
                moment=moment,
            )
        else:
            outcome = _gazette_outcome(
                search,
                ranges[search.name],
                reading,
                since=since,
                allowance=allowance,
                moment=moment,
                today=current,
            )
        remaining -= outcome.used
        results.append(await _stored(store, owner, search, outcome))

    notes: list[str] = []
    failed = sum(result.status == "failed" for result in results)
    partial = sum(result.status == "partial" for result in results)
    checked = sum(result.status == "checked" for result in results)
    if failed:
        notes.append(
            f"{failed} saved {'search' if failed == 1 else 'searches'} could not be checked; "
            f"see {'its' if failed == 1 else 'their'} note{'' if failed == 1 else 's'}."
        )
    if partial:
        notes.append(
            f"{checked} of {len(results)} saved searches fully checked. Call again to continue."
        )
    if current in reading.empty:
        notes.append(TODAY_MISSING_NOTE)
    kinds = {search.kind for search in searches}
    return CheckSearchesResult(
        checked=checked,
        total=len(results),
        complete=partial == 0,
        results=results,
        note=" ".join(notes) or None,
        provenance=Provenance(
            generated_at=moment,
            data_updated_at=midnight_utc(max(reading.summaries, default=None)),
            **_notice(kinds),
        ),
    )


def _notice(kinds: set[str]) -> dict[str, str]:
    """The ``notice`` override for a check of these kinds of search (none for laws alone)."""
    if kinds == {"law"}:
        return {}
    return {"notice": _BOTH_NOTICE if "law" in kinds else GAZETTE_NOTICE}


async def _stored(
    store: SavedSearchStore, owner: str, search: SavedSearch, outcome: _Outcome
) -> SearchCheck:
    """Store the progress of one outcome and return its check, as delivered.

    The check is returned as it is only if the progress was stored. If the row changed or went
    away while the check was reading (another check, a rename, a delete) nothing is marked as
    seen, so the items are not shown either: they would be shown again by the next call.
    """
    update = outcome.progress
    if update is None:
        return outcome.check
    stored = await store.update_progress(
        owner,
        search.name,
        expected=search,
        checked_through=update.checked_through,
        seen=update.seen,
        seen_items=update.seen_items,
        last_checked_at=update.last_checked_at,
    )
    if stored:
        return outcome.check
    check = outcome.check
    changes: dict[str, object] = {"status": "partial", "note": _CHANGED_NOTE}
    if check.items is not None:
        changes |= {"items": [], "has_more": bool(check.matched)}
    if check.changes is not None:
        changes |= {"changes": [], "other_new": None}
    return check.model_copy(update=changes)


def _days(start: date, end: date) -> list[date]:
    """Every day from ``start`` to ``end``, inclusive, oldest first."""
    return [start + timedelta(days=step) for step in range((end - start).days + 1)]


# -- law searches --


async def _read_law(
    client: SavedSearchClient, search: SavedSearch, tally: _Tally
) -> LawAnalysis | _Unread:
    """Read one law's analysis, or say why it could not be read."""
    law_id = search.law_id or ""
    try:
        # A cached analysis is free, so it never counts against the budget; an uncached one is
        # not requested once the deadline has passed and the law waits for the next call.
        if not await client.has_cached_analysis(law_id) and tally.out_of_time():
            return _Unread("partial", "Not read yet; call again.")
        return await client.get_law_analysis(law_id)
    except BoeNotFoundError:
        return _Unread("failed", "The BOE no longer has this law.")
    except BoeError as error:
        # Never the API's own words: they are not ours to relay and mean nothing to the user.
        if error.retryable:
            return _Unread("partial", "Could not be read; try again.")
        return _Unread("failed", "Could not be read.")
    finally:
        await tally.step()


def _change(reference: LawReference) -> SearchChange:
    """The model-facing form of one reported reference."""
    return SearchChange(
        law_id=reference.law_id,
        kind=relation_kind(reference.relation.code),
        relation=reference.relation.text,
        text=ellipsize(reference.text, _MAX_TEXT),
    )


def _law_check(
    search: SavedSearch,
    status: SearchStatus,
    *,
    note: str | None,
    changes: list[SearchChange] | None = None,
    total: int = 0,
    other_new: dict[str, int] | None = None,
) -> SearchCheck:
    """The check of a law search; by default nothing is reported."""
    return SearchCheck(
        name=search.name,
        kind="law",
        status=status,
        law_id=search.law_id,
        law_title=search.law_title,
        changes=changes or [],
        changes_total=total,
        other_new=other_new,
        note=note,
    )


def _other_counts(others: list[LawReference]) -> dict[str, int]:
    """Count references that are not changes by what they are; zero counts are left out."""
    kinds = Counter(
        {"judicial": "court_rulings", "cites": "citations"}.get(
            relation_kind(reference.relation.code), "other"
        )
        for reference in others
    )
    return {
        label: kinds[label] for label in ("court_rulings", "citations", "other") if kinds[label]
    }


def _law_outcome(
    search: SavedSearch,
    read: LawAnalysis | _Unread | None,
    *,
    since: date | None,
    allowance: int,
    moment: datetime,
) -> _Outcome:
    """The check of a law search: its references not seen yet, and ``seen`` grown by those shown."""
    if since is not None:
        # The references carry no date, only the acting norm's identifier, and a year taken from
        # it would pass for a date: so a law is not re-read by date at all.
        return _Outcome(_law_check(search, "checked", note=_LAW_SINCE_NOTE))
    if read is None or isinstance(read, _Unread):
        unread = read or _Unread("partial", "Not read yet; call again.")
        return _Outcome(_law_check(search, unread.status, note=unread.note))
    fresh: dict[str, LawReference] = {}
    for reference in read.later:
        key = _reference_key(reference)
        if key not in search.seen:
            fresh.setdefault(key, reference)
    reported = [
        (key, reference)
        for key, reference in fresh.items()
        if relation_kind(reference.relation.code) in REPORTED_KINDS
    ]
    others = [
        (key, reference)
        for key, reference in fresh.items()
        if relation_kind(reference.relation.code) not in REPORTED_KINDS
    ]
    if reported and allowance == 0:
        # The call's total was used up by earlier searches: nothing is marked, nothing lost.
        return _Outcome(_law_check(search, "partial", note=_NOT_REACHED_NOTE, total=len(reported)))
    shown = reported[:allowance]
    # Why only the shown changes are remembered: the rest were never put in front of the reader,
    # so marking them seen would lose them for good; they stay new for the next call.
    # Why ``seen`` only grows: a reference that vanishes from the analysis and comes back (a
    # correction of the BOE's data) must not be reported twice.
    seen = search.seen | {key for key, _ in shown} | {key for key, _ in others}
    counts = _other_counts([reference for _, reference in others])
    cut = len(reported) > len(shown)
    notes: list[str] = []
    if cut:
        notes.append(f"Showing {len(shown)} of {len(reported)} changes; call again for the rest.")
    if rulings := counts.get("court_rulings"):
        notes.append(
            f"{rulings} new court {'ruling' if rulings == 1 else 'rulings'} on this law "
            f"{'is' if rulings == 1 else 'are'} not listed; explore_amendment_chain with "
            "include='all' shows them."
        )
    return _Outcome(
        _law_check(
            search,
            "partial" if cut else "checked",
            note=" ".join(notes) or None,
            changes=[_change(reference) for _, reference in shown],
            total=len(reported),
            other_new=counts or None,
        ),
        _Progress(search.checked_through, seen, search.seen_items, moment),
        used=len(shown),
    )


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
    unbroken run of finished days, so the days nearest the bookmark must be read before any
    later one, and stopping early (budget, a temporary error) leaves nothing unread in the
    middle.
    """
    reading = _Reading()
    for day in days:
        cached = await client.has_cached_summary(day, today=today)
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
            else:
                reading.empty.add(day)
        await tally.step()
    return reading


def _gazette_outcome(
    search: SavedSearch,
    span: _Range,
    reading: _Reading,
    *,
    since: date | None,
    allowance: int,
    moment: datetime,
    today: date,
) -> _Outcome:
    """The check of a gazette search over the days read, and the progress it earns.

    New matches are the matching items of the days read that were not delivered before, shown
    oldest day first up to ``allowance``. A day is finished when it was read, all its new
    matches were delivered and it is settled; the bookmark moves over the unbroken run of
    finished days from the start, and the identifiers delivered for the days after it are kept.
    """
    if span.start > span.end:
        # Only a version-1 row that was checked through today gets here.
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
        return _Outcome(check, _Progress(through, search.seen, search.seen_items, moment))

    days = _days(span.start, span.end)
    run: list[date] = []
    for day in days:
        if not reading.read(day):
            break
        run.append(day)

    wanted = GazetteFilter(search.words, search.section, search.department_code)
    fresh: dict[date, list[GazetteItem]] = {}
    for day in run:
        found = reading.summaries.get(day)
        if found is None:
            continue
        # With ``since`` what was delivered before is shown again: it is a re-read.
        known = frozenset() if since is not None else search.seen_items.get(day, frozenset())
        matches = [
            item for item in found.items if wanted.matches(item) and item.identifier not in known
        ]
        if matches:
            fresh[day] = matches
    matched = sum(len(matches) for matches in fresh.values())

    delivered: dict[date, list[GazetteItem]] = {}
    room = allowance
    for day, matches in fresh.items():  # oldest day first: the run is in that order
        taken = matches[:room]
        room -= len(taken)
        if taken:
            delivered[day] = taken
    entries = [
        scanned_entry(day, item, several_issues=len(reading.summaries[day].issues) > 1)
        for day, taken in delivered.items()
        for item in taken
    ]
    undelivered = matched - len(entries)
    # Calling again can only help when something is stored; with ``since`` it cannot.
    not_reached = since is None and undelivered > 0 and not entries
    all_read = len(run) == len(days)
    status: SearchStatus = (
        "checked" if all_read and (undelivered == 0 or since is not None) else "partial"
    )
    skipped_days = [day for day in run if day in reading.failed]

    notes: list[str] = []
    if span.skipped:
        notes.append(
            f"Only the last {DEFAULT_CHECK_DAYS} days were checked; {span.skipped} earlier "
            f"{'day was' if span.skipped == 1 else 'days were'} skipped. "
            f"Pass since to read up to {MAX_SINCE_DAYS} days back."
        )
    if not_reached:
        notes.append(_NOT_REACHED_NOTE)
    elif undelivered:
        if since is not None:
            notes.append(
                f"Showing {len(entries)} of {matched}; scan_gazette with the same filters lists "
                f"them all (at most {MAX_SCAN_DAYS} days per call)."
            )
        else:
            notes.append(f"Showing {len(entries)} of {matched} new items; call again for the rest.")
    if skipped_days:
        notes.append(
            f"{len(skipped_days)} day{'s' if len(skipped_days) != 1 else ''} could not be read "
            f"and {'were' if len(skipped_days) != 1 else 'was'} skipped."
        )
    check = SearchCheck(
        name=search.name,
        kind="gazette",
        status=status,
        from_date=run[0] if run else None,
        to_date=run[-1] if run else None,
        days_pending=len(days) - len(run),
        days_failed=len(skipped_days),
        matched=matched,
        items=entries,
        has_more=undelivered > 0,
        note=" ".join(notes) or None,
    )
    if since is not None or not_reached or not run:
        # ``since`` stores nothing; a search that was not reached or read nothing has no news
        # for the store either.
        return _Outcome(check, used=len(entries))

    through = search.checked_through
    for day in run:
        if not (
            reading.settled(day, today) and len(delivered.get(day, ())) == len(fresh.get(day, ()))
        ):
            break
        through = day
    remembered = dict(search.seen_items)
    for day, taken in delivered.items():
        remembered[day] = remembered.get(day, frozenset()) | {item.identifier for item in taken}
    # Days up to the bookmark are finished: their identifiers are no longer needed.
    kept = {day: ids for day, ids in remembered.items() if through is None or day > through}
    return _Outcome(check, _Progress(through, search.seen, kept, moment), used=len(entries))
