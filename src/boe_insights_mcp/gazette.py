"""Use cases of the two gazette tools: one day's summary and a scan over a range of days.

Both tools read the BOE's daily summaries (everything printed that day, not just consolidated
laws), which ``client`` fetches and ``summaries`` parses into ``DailySummary`` models. This
module decides which items match the filters, how a day or a range is chosen, how much work one
call may do, and how results are presented as ``schemas``. Title word matching is the trend
tools' (``analytics.title_matcher``).

Layering: ``server`` calls these functions (one per tool) -> ``client`` for data -> ``schemas``
for the results. Like the other use-case modules, ``now`` and ``today`` exist so that tests can
fix the clock, and the scan's time budget takes an injected monotonic clock.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from boe_insights_mcp.analytics import title_matcher
from boe_insights_mcp.catalogue import ProgressCallback
from boe_insights_mcp.client import FIRST_SUMMARY_DAY
from boe_insights_mcp.clock import madrid_today, utc_now
from boe_insights_mcp.errors import BoeError
from boe_insights_mcp.models import DailySummary, GazetteItem
from boe_insights_mcp.schemas import (
    GAZETTE_NOTICE,
    DailySummaryResult,
    DayMatches,
    GazetteCount,
    GazetteEntry,
    GazetteIssueInfo,
    Provenance,
    ScanGazetteResult,
    ScannedEntry,
)

__all__ = [
    "DEFAULT_LIMIT",
    "LOOKBACK_DAYS",
    "MAX_LIMIT",
    "MAX_SCAN_DAYS",
    "SCAN_TIME_LIMIT",
    "SummaryClient",
    "daily_summary",
    "scan_gazette",
]

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_DEPARTMENTS = 15
# Titles of announcements can run to a thousand characters; a page of items would swamp the
# model, and the first 200 say what the item is.
MAX_TITLE = 200
# Without a date the tool shows the latest issue found within this many days before today.
LOOKBACK_DAYS = 7
MAX_SCAN_DAYS = 31
DEFAULT_SCAN_DAYS = 7
# The whole call is bounded like ``most_amended_laws`` (``trends.ANALYSIS_TIME_LIMIT``): the loop
# starts no fresh request when fewer than ``_TIME_MARGIN`` seconds remain, because a request
# that starts late can still take several seconds to answer.
SCAN_TIME_LIMIT = 40.0
_TIME_MARGIN = 5.0


class SummaryClient(Protocol):
    """The two client methods the gazette tools need; ``BoeClient`` satisfies it."""

    async def get_daily_summary(self, day: date, *, today: date) -> DailySummary | None:
        """The summary of one day, or ``None`` when there was no issue."""
        ...

    async def has_cached_summary(self, day: date) -> bool:
        """Whether the day's summary (or remembered absence) is cached (no request)."""
        ...


# --- shared helpers ----------------------------------------------------------------------------


def _blank_to_none(value: str | None) -> str | None:
    """Strip a filter value; blank means "not given"."""
    stripped = (value or "").strip()
    return stripped or None


def _check_paging(offset: int, limit: int) -> None:
    """Raise ``ValueError`` for a negative offset or a limit outside 1..``MAX_LIMIT``."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")


class _Filter:
    """The three item filters of both tools; every one that is given must match (AND)."""

    def __init__(self, words: str | None, section: str | None, department_code: str | None) -> None:
        """Normalise the raw arguments: blank values and words without letters mean "absent"."""
        self._words = title_matcher(words)
        section = _blank_to_none(section)
        # Section codes are letters and digits whose case carries no meaning ("2a" is "2A").
        self._section = section.upper() if section else None
        self._department = _blank_to_none(department_code)

    @property
    def is_empty(self) -> bool:
        """True when no filter was given."""
        return self._words is None and self._section is None and self._department is None

    def matches(self, item: GazetteItem) -> bool:
        """Whether ``item`` satisfies every given filter."""
        if self._section is not None and item.section_code.upper() != self._section:
            return False
        if self._department is not None and item.department_code != self._department:
            return False
        return self._words is None or self._words(item.title)


def _shorten(title: str) -> str:
    """Cut a title longer than ``MAX_TITLE`` at a word boundary and add "…" (not counted)."""
    if len(title) <= MAX_TITLE:
        return title
    # Titles have single spaces (``GazetteItem.title``), so a space is the word boundary. When
    # the character just after the window is a space the window ends exactly between words and
    # is kept whole; otherwise the last word is cut, so the window goes back to the last space.
    # A title with no space in the window (one very long token) is cut hard instead.
    cut = MAX_TITLE if title[MAX_TITLE] == " " else title.rfind(" ", 0, MAX_TITLE)
    if cut <= 0:
        cut = MAX_TITLE
    return title[:cut].rstrip() + "…"


def _compact_fields(item: GazetteItem, *, several_issues: bool) -> dict[str, Any]:
    """The fields shared by the compact entries of both tools.

    ``several_issues`` says whether the item's day has more than one issue; only then is the
    issue number worth giving (it is the one thing that tells an extraordinary issue apart).
    """
    return {
        "identifier": item.identifier,
        "title": _shorten(item.title),
        "section": item.section_code,
        "department": item.department_name,
        "heading": item.heading,
        "issue": item.issue_number if several_issues else None,
        # The page of the text on boe.es is the better link; the PDF is the fallback for the
        # announcements that have no HTML version (api-notes, fourth probe).
        "url": item.html_url or item.pdf_url,
    }


def _entry(item: GazetteItem, *, several_issues: bool) -> GazetteEntry:
    """The compact entry of an item."""
    return GazetteEntry(**_compact_fields(item, several_issues=several_issues))


def _page(total: int, offset: int, limit: int) -> tuple[int, bool, int | None]:
    """The end of a page and the paging flags: ``(end, has_more, next_offset)``."""
    end = min(offset + limit, total)
    has_more = end < total
    return end, has_more, end if has_more else None


def _day_count(count: int) -> str:
    """``"1 day"`` or ``"3 days"``."""
    return f"{count} day" if count == 1 else f"{count} days"


def _provenance(moment: datetime, *, notice: str = GAZETTE_NOTICE) -> Provenance:
    """The provenance block: stamped at assembly time, with no update date (summaries have none).

    The notice is the gazette's by default: a summary lists what was published, not consolidated
    texts, so the consolidated-text notice of ``Provenance`` would be wrong for it.
    """
    return Provenance(generated_at=moment, data_updated_at=None, notice=notice)


# --- get_daily_summary -------------------------------------------------------------------------


def _counts(
    items: Iterable[GazetteItem], *, by_section: bool, top: int | None
) -> list[GazetteCount]:
    """Items per section (in order of first appearance) or per department (largest first)."""
    counter: Counter[str | None] = Counter()
    names: dict[str | None, str | None] = {}
    for item in items:
        code = item.section_code if by_section else item.department_code
        counter[code] += 1
        names.setdefault(code, item.section_name if by_section else item.department_name)
    # ``Counter`` and ``dict`` keep first-appearance order and ``sorted`` is stable, so equal
    # counts stay in the order the department first appears in the day's summary.
    codes = list(counter) if by_section else sorted(counter, key=lambda code: -counter[code])
    return [GazetteCount(code=code, name=names[code], items=counter[code]) for code in codes[:top]]


async def daily_summary(
    client: SummaryClient,
    *,
    day: date | None,
    section: str | None = None,
    department_code: str | None = None,
    words: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
    today: date | None = None,
) -> DailySummaryResult:
    """Summarise one day's gazette, with the items that match the filters paged.

    Without ``day`` it gives the latest issue found within ``LOOKBACK_DAYS`` days before today
    (Madrid) and says which day was asked when it had to fall back. Raises ``ValueError`` for
    paging outside its bounds or a date outside 1960-09-01..today; client errors propagate.
    """
    _check_paging(offset, limit)
    moment = utc_now(now)
    # ``today`` is computed here, once, and handed to the client: the cache lifetime of a summary
    # depends on whether its day is today, and one call must not straddle midnight in the middle.
    current = madrid_today(moment, today)
    note: str | None = None
    if day is not None:
        shown, found = day, await client.get_daily_summary(day, today=current)
        if found is None:
            note = (
                f"No BOE issue on {day.isoformat()}: no publication that day, or not yet published."
            )
    else:
        shown, found = current, None
        # Newest first: the first day that has an issue wins. Today's 404 is not cached by the
        # client, so on a Monday morning before publication this re-asks for today every call.
        for back in range(LOOKBACK_DAYS + 1):
            candidate = current - timedelta(days=back)
            found = await client.get_daily_summary(candidate, today=current)
            if found is not None:
                shown = candidate
                break
        if found is None:
            earliest = (current - timedelta(days=LOOKBACK_DAYS)).isoformat()
            note = f"No BOE issue found from {earliest} to {current.isoformat()}."
        elif shown != current:
            note = (
                f"No BOE issue on {current.isoformat()} (no publication that day, or not yet "
                f"published): showing the latest issue, {shown.isoformat()}."
            )
    if found is None:
        return DailySummaryResult(
            date=shown,
            published=False,
            issues=[],
            total_items=0,
            sections=[],
            departments=[],
            matched=0,
            items=[],
            returned=0,
            has_more=False,
            next_offset=None,
            note=note,
            provenance=_provenance(moment),
        )
    wanted = _Filter(words, section, department_code)
    matching = [item for item in found.items if wanted.matches(item)]
    end, has_more, next_offset = _page(len(matching), offset, limit)
    page = matching[offset:end]
    return DailySummaryResult(
        date=shown,
        published=True,
        issues=[GazetteIssueInfo(number=i.number, identifier=i.identifier) for i in found.issues],
        total_items=len(found.items),
        # The sections describe the whole day, so the reader sees what else there is; the
        # departments describe the matches, so they answer "who published what I asked for".
        sections=_counts(found.items, by_section=True, top=None),
        departments=_counts(matching, by_section=False, top=MAX_DEPARTMENTS),
        matched=len(matching),
        items=[_entry(item, several_issues=len(found.issues) > 1) for item in page],
        returned=len(page),
        has_more=has_more,
        next_offset=next_offset,
        note=note,
        provenance=_provenance(moment),
    )


# --- scan_gazette ------------------------------------------------------------------------------


def _resolve_range(from_date: date | None, to_date: date | None, today: date) -> tuple[date, date]:
    """The inclusive range to scan, validated; raises ``ValueError`` when it cannot be scanned."""
    end = to_date if to_date is not None else today
    # A date after today cannot have a summary yet, and asking for "this month" is natural, so
    # the end is cut back to today instead of refused (the result shows the range really used).
    end = min(end, today)
    start = from_date if from_date is not None else end - timedelta(days=DEFAULT_SCAN_DAYS - 1)
    if from_date is not None and from_date > today:
        raise ValueError("from_date is in the future: there are no BOE issues after today")
    # Nothing exists before the first summary, so an earlier start is moved up to it.
    start = max(start, FIRST_SUMMARY_DAY)
    if start > end:
        raise ValueError("from_date must not be after to_date")
    if (end - start).days + 1 > MAX_SCAN_DAYS:
        raise ValueError(f"the range may not exceed {MAX_SCAN_DAYS} days")
    return start, end


def _scan_note(scanned: int, total: int, pending: int, failed: int, unavailable: int) -> str | None:
    """What to tell the caller about an incomplete scan or skipped days; ``None`` when nothing."""
    parts: list[str] = []
    if pending:
        parts.append(
            f"Scanned {scanned} of {total} days. Call again with the same arguments to continue "
            "(days already read are cached)."
        )
    if unavailable:
        parts.append(f"{_day_count(unavailable)} could not be read and will be retried.")
    if failed:
        verb = "was" if failed == 1 else "were"
        parts.append(f"{_day_count(failed)} could not be read and {verb} skipped.")
    return " ".join(parts) or None


async def scan_gazette(
    client: SummaryClient,
    *,
    words: str | None = None,
    section: str | None = None,
    department_code: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit: float = SCAN_TIME_LIMIT,
) -> ScanGazetteResult:
    """Find the items matching the filters over up to ``MAX_SCAN_DAYS`` days, newest day first.

    One request per day not already cached; no fresh request starts once ``time_limit`` seconds
    minus a 5 s margin have passed since the call began, which leaves the older days pending
    (calling again continues, because read days are cached). A day that fails with a retryable
    error stays pending; one that fails for good is counted in ``days_failed``. Raises
    ``ValueError`` when no filter is given or the range is invalid.
    """
    # The clock starts before anything else for the same reason as in ``most_amended_laws``:
    # the client bounds the whole call, so time spent before the loop is time not available to it.
    deadline = clock() + time_limit - _TIME_MARGIN
    _check_paging(offset, limit)
    wanted = _Filter(words, section, department_code)
    if wanted.is_empty:
        raise ValueError("give at least one of words, section or department_code")
    moment = utc_now(now)
    current = madrid_today(moment, today)
    start, end = _resolve_range(from_date, to_date, current)
    total_days = (end - start).days + 1

    matches: list[ScannedEntry] = []
    by_day: list[DayMatches] = []
    scanned = with_issue = failed = unavailable = 0
    for step in range(total_days):
        day = end - timedelta(days=step)
        cached = await client.has_cached_summary(day)
        # A cached day costs nothing, so it never counts against the budget; the first uncached
        # day after the deadline ends the loop and every older day waits for the next call.
        if not cached and clock() >= deadline:
            break
        found: DailySummary | None
        try:
            found = await client.get_daily_summary(day, today=current)
        except BoeError as error:
            # One bad day must not sink the scan. A retryable failure says nothing about the
            # day, so it stays pending and is asked again next call; a permanent one (the body
            # is unreadable, the request is rejected) would fail the same way again, so it is
            # counted as failed and no longer pending. Programming errors are not ``BoeError``
            # and still propagate.
            if error.retryable:
                unavailable += 1
            else:
                failed += 1
        else:
            scanned += 1
            if found is not None:
                with_issue += 1
                day_matches = [
                    ScannedEntry(
                        date=day, **_compact_fields(item, several_issues=len(found.issues) > 1)
                    )
                    for item in found.items
                    if wanted.matches(item)
                ]
                if day_matches:
                    by_day.append(DayMatches(date=day, matches=len(day_matches)))
                    matches.extend(day_matches)
        if progress is not None:
            # Every day gives one report, so the value strictly increases as MCP requires.
            done = step + 1
            await progress(
                done, total_days, f"Read {day.isoformat()} ({done} of {total_days} days)"
            )
    pending = total_days - scanned - failed
    end_index, has_more, next_offset = _page(len(matches), offset, limit)
    page = matches[offset:end_index]
    return ScanGazetteResult(
        from_date=start,
        to_date=end,
        days_scanned=scanned,
        days_with_issue=with_issue,
        days_pending=pending,
        days_failed=failed,
        complete=pending == 0,
        matched=len(matches),
        items=page,
        returned=len(page),
        has_more=has_more,
        next_offset=next_offset,
        by_day=by_day,
        note=_scan_note(scanned, total_days, pending, failed, unavailable),
        provenance=_provenance(moment),
    )
