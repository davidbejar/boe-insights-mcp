"""Use cases of the trend tools: counts over time, rankings, topic comparison, spike
explanation and the most amended laws.

Every number comes from :mod:`boe_insights_mcp.analytics`; this module only decides which laws
go in (the filters), which dates apply (the range and its default), and how the result is
presented (coverage, caveats, provenance, compact points). The laws come from the
:class:`~boe_insights_mcp.catalogue.CatalogueStore`; ``most_amended_laws`` also needs the
client, because amendments are not part of a search hit and cost one request per law.

Layering: ``server`` calls these functions (one per tool) -> ``catalogue`` / ``client`` for
data -> ``analytics`` for counting -> ``schemas`` for the results. Like the other use-case
modules, the ``now`` and ``today`` arguments exist so that tests can fix the clock.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Protocol, get_args

from boe_insights_mcp import analytics
from boe_insights_mcp.analytics import DateField, Dimension, Period, TimeSeries
from boe_insights_mcp.catalogue import Catalogue, CatalogueStore, ProgressCallback
from boe_insights_mcp.clock import madrid_today, utc_now
from boe_insights_mcp.errors import BoeError
from boe_insights_mcp.legislation import (
    RankFilter,
    ScopeFilter,
    brief,
    rank_code,
    relation_group,
    scope_code,
)
from boe_insights_mcp.models import LawAnalysis, LawSummary
from boe_insights_mcp.schemas import (
    AmendedLaw,
    CompareTopicsResult,
    Coverage,
    DepartmentCount,
    ExplainSpikeResult,
    GroupSeries,
    LegislationOverTimeResult,
    MostAmendedLawsResult,
    PeriodCount,
    PeriodSpike,
    Provenance,
    SpikeContribution,
    TopDepartmentsResult,
    TopicSpec,
    TopicTrend,
)

__all__ = [
    "ANALYSIS_TIME_LIMIT",
    "DEFAULT_YEARS_BACK",
    "ENACTED_SETTLING_DAYS",
    "MAX_POINTS",
    "AnalysisClient",
    "DateBasis",
    "GroupBy",
    "TrendFilters",
    "compare_topics",
    "explain_spike",
    "legislation_over_time",
    "most_amended_laws",
    "top_departments",
]

DateBasis = Literal["published", "enacted"]
GroupBy = Literal["none", "rank", "department", "scope"]

# The default range starts on 1 January this many years before the current year, so with the
# current year it spans 20 calendar years.
DEFAULT_YEARS_BACK = 19
# Seconds ``most_amended_laws`` may spend on fresh analysis requests in one call. A tool call
# that runs much longer risks the client giving up on it; the work done so far stays cached, so
# calling again continues.
ANALYSIS_TIME_LIMIT = 40.0
# The loop stops this many seconds before the limit, so that a request in flight and the
# assembly of the result still finish inside it.
_TIME_MARGIN = 5.0

# The most points (series x periods) a result may hold. A 50-year monthly split into five groups
# measured 225 890 characters, far more than a model can use; 1 500 points stay near 50 000.
MAX_POINTS = 1500
MAX_TOP_GROUPS = 10
MAX_DEPARTMENTS = 50
MIN_BASELINE_PERIODS, MAX_BASELINE_PERIODS = 2, 20
MAX_EXPLAINED_LAWS = 50
MAX_AMENDED = 25
MAX_EXAMINED = 60
MIN_TOPICS, MAX_TOPICS = 2, 5

_DATE_FIELDS: dict[str, DateField] = {"published": "published_on", "enacted": "enacted_on"}
_CODE = re.compile(r"[A-Za-z0-9_-]+")
# The relation groups of ``get_amendment_history`` that count as amending a law. Corrections,
# citations, approvals and court proceedings fall in "corrections" and "other" and are excluded.
_AMENDMENT_GROUPS = ("modifies", "adds", "repeals")
# ``period_key`` formats and the period each one implies.
_PERIOD_KEYS: tuple[tuple[re.Pattern[str], Period], ...] = (
    (re.compile(r"(\d{4})"), "year"),
    (re.compile(r"(\d{4})-Q([1-4])"), "quarter"),
    (re.compile(r"(\d{4})-(0[1-9]|1[0-2])"), "month"),
)
_EARLIEST_YEAR = 1800
# Laws are published weeks after they are enacted (api-notes, third probe, "Publication lag after
# enactment"), so under ``date_basis="enacted"`` a period is still filling up for this long.
ENACTED_SETTLING_DAYS = 90

# Wordings are model-facing. The evidence behind the coverage sentence is in api-notes, third
# probe, "Coverage of the collection by year".
_CAVEAT_BASE = (
    "Counts cover the BOE's consolidated legislation collection, which holds only part of each "
    "year's laws and a varying part from year to year (thinner before about 2010 and for the "
    "most recent year or two). A change in the count is not by itself a change in how much was "
    "legislated."
)
_CAVEAT_DEPARTMENTS = (
    "Ministries are renamed and recoded when governments reorganise, so a department can "
    "appear as new without being so."
)
_CAVEAT_RATIO = (
    "Compare count with highest_number: a low ratio means the collection holds few of that "
    "year's laws."
)
_CAVEAT_REPRINT = (
    "Regional laws are dated by their BOE reprint, which can be weeks or years after regional "
    "publication; use date_basis='enacted' to avoid this; its last three months are incomplete."
)
_CAVEAT_PARTIAL = "Periods marked partial are incomplete and not comparable."
_CAVEAT_TEXT = "Body-text matching is the BOE search engine's; it matches exact words."


@dataclass(frozen=True)
class TrendFilters:
    """The filters shared by the trend tools; every field is optional and they combine with AND."""

    title: str | None = None
    text: str | None = None
    subject_code: str | None = None
    rank: RankFilter | None = None
    scope: ScopeFilter = "any"
    department_code: str | None = None


class AnalysisClient(Protocol):
    """The two client methods ``most_amended_laws`` needs; ``BoeClient`` satisfies it."""

    async def has_cached_analysis(self, law_id: str) -> bool:
        """Whether the analysis is cached (no request)."""
        ...

    async def get_law_analysis(self, law_id: str) -> LawAnalysis:
        """The analysis of one law."""
        ...


class _Monotonic:
    """Wraps a progress callback so that the values it receives strictly increase.

    One tool call can report several phases (the catalogue download, then a search, then a loop
    over analyses), each counting from its own start, while MCP requires the progress of one
    request to increase. Each report is raised to one above the previous value when needed; the
    message still says what is really being counted.
    """

    def __init__(self, callback: ProgressCallback) -> None:
        """Wrap ``callback``."""
        self._callback = callback
        self._last = 0

    async def __call__(self, done: int, total: int | None, message: str) -> None:
        """Forward the report with a value above every earlier one."""
        self._last = max(done, self._last + 1)
        await self._callback(self._last, total, message)


@dataclass(frozen=True)
class _Prepared:
    """Everything the use cases share once the filters and the range have been resolved."""

    catalogue: Catalogue
    matched: list[LawSummary]
    """The laws that pass the filters, before the date range."""
    start: date
    end: date
    field: DateField
    moment: datetime
    today: date
    progress: ProgressCallback | None
    settling_days: int
    """Days after a period's end in which it is still partial (``ENACTED_SETTLING_DAYS``)."""


def _resolve_range(
    from_date: date | None, to_date: date | None, today: date, *, open_start: bool = False
) -> tuple[date, date]:
    """Apply the default range and check the order of the two dates.

    With ``open_start`` a missing ``from_date`` means no lower bound instead of the default one.
    """
    end = to_date if to_date is not None else today
    # The default start hangs on the end date, not on today, so that giving only ``to_date``
    # in the past still yields a 20-year window instead of an inverted one.
    if from_date is not None:
        start = from_date
    elif open_start:
        start = date.min
    else:
        start = date(end.year - DEFAULT_YEARS_BACK, 1, 1)
    if start > end:
        raise ValueError("from_date must not be after to_date")
    return start, end


def _check_codes(*codes: tuple[str, str | None]) -> None:
    """Raise ``ValueError`` for a code that is not letters, digits, ``-`` or ``_``."""
    # The department code is only compared locally, but it is checked like the others so that a
    # typo such as a space gives an error instead of silently matching nothing.
    for name, value in codes:
        if value is not None and not _CODE.fullmatch(value):
            raise ValueError(f"invalid {name}: {value!r} (letters, digits, '-' and '_' only)")


def _blank_to_none(value: str | None) -> str | None:
    """Treat a blank string as absent."""
    return value if value is not None and value.strip() else None


def _local_filter(
    laws: Sequence[LawSummary],
    *,
    rank: RankFilter | None,
    scope: ScopeFilter,
    department_code: str | None,
    title: str | None = None,
) -> list[LawSummary]:
    """Apply the criteria that are evaluated on the search hit itself."""
    scope_value = scope_code(scope)
    return analytics.filter_laws(
        laws,
        rank_codes={rank_code(rank)} if rank is not None else None,
        department_codes={department_code} if department_code is not None else None,
        scope_codes={scope_value} if scope_value is not None else None,
        title_words=title,
    )


async def _prepare(
    store: CatalogueStore,
    filters: TrendFilters,
    *,
    from_date: date | None,
    to_date: date | None,
    date_basis: DateBasis,
    period: Period | None,
    progress: ProgressCallback | None,
    now: datetime | None,
    today: date | None,
    open_start: bool = False,
) -> _Prepared:
    """Validate the arguments, resolve the range, then load the catalogue and select the laws.

    ``open_start`` is for the tools that count laws rather than periods: no default lower bound.
    """
    if date_basis not in _DATE_FIELDS:
        raise ValueError(f"date_basis must be one of {', '.join(_DATE_FIELDS)}")
    _check_codes(
        ("subject_code", filters.subject_code), ("department_code", filters.department_code)
    )
    moment = utc_now(now)
    day = madrid_today(moment, today)
    start, end = _resolve_range(from_date, to_date, day, open_start=open_start)
    if period is not None:
        # Fails early (too many buckets) instead of after a possible 30-second download.
        analytics.bucket_range(start, end, period)
    wrapped: ProgressCallback | None = _Monotonic(progress) if progress is not None else None
    catalogue = await store.get(progress=wrapped)
    # The API evaluates body text and subject; everything else is checked locally on the hits.
    selected = await store.select(
        text=_blank_to_none(filters.text),
        subject_code=filters.subject_code,
        progress=wrapped,
    )
    matched = _local_filter(
        selected,
        rank=filters.rank,
        scope=filters.scope,
        department_code=filters.department_code,
        title=filters.title,
    )
    settling = ENACTED_SETTLING_DAYS if date_basis == "enacted" else 0
    return _Prepared(
        catalogue, matched, start, end, _DATE_FIELDS[date_basis], moment, day, wrapped, settling
    )


def _coverage(catalogue: Catalogue, matched: int) -> Coverage:
    """The coverage block of a result."""
    return Coverage(
        catalogue_size=len(catalogue.laws),
        catalogue_fetched_at=catalogue.fetched_at,
        matched=matched,
    )


def _provenance(prepared: _Prepared) -> Provenance:
    """The provenance block: stamped at assembly time, with the catalogue's newest update."""
    return Provenance(
        generated_at=prepared.moment, data_updated_at=prepared.catalogue.newest_update
    )


def _caveats(
    *,
    scope: ScopeFilter,
    date_basis: DateBasis,
    partial: bool,
    text_used: bool,
    departments: bool = False,
    ratio: bool = False,
) -> list[str]:
    """The fixed caveat sentences that apply, in a fixed order."""
    caveats = [_CAVEAT_BASE]
    # State laws are published in the BOE itself, so only regional ones suffer the reprint delay.
    if scope != "state" and date_basis == "published":
        caveats.append(_CAVEAT_REPRINT)
    if partial:
        caveats.append(_CAVEAT_PARTIAL)
    if text_used:
        caveats.append(_CAVEAT_TEXT)
    if departments:
        caveats.append(_CAVEAT_DEPARTMENTS)
    if ratio:
        caveats.append(_CAVEAT_RATIO)
    return caveats


def _points(series: TimeSeries, highest: dict[int, int] | None = None) -> list[PeriodCount]:
    """The compact points of a series; ``highest`` maps a year to its highest official number."""
    return [
        PeriodCount(
            period=p.bucket.key,
            count=p.count,
            partial=p.partial,
            highest_number=highest.get(p.bucket.start.year) if highest else None,
        )
        for p in series.points
    ]


# The ranks whose laws are numbered afresh every year ("N/YYYY"), so that the highest number of a
# year bounds how many were enacted (api-notes, third probe, "Coverage of the collection by year").
_NUMBERED_RANKS = ("ley", "ley_organica", "real_decreto_ley", "real_decreto_legislativo")


def _coverage_numbers(
    catalogue: Catalogue, filters: TrendFilters, *, period: Period, group_by: GroupBy
) -> dict[int, int]:
    """Highest official number per year, or ``{}`` when this request is not a plain yearly count.

    Only a yearly, ungrouped count of one numbered rank (and nothing else but the state scope)
    can be set against the rank's own numbering; any other filter would make the count a subset
    of the numbered laws and the comparison meaningless.
    """
    plain = (
        filters.title is None
        and filters.text is None
        and filters.subject_code is None
        and filters.department_code is None
        and filters.scope in ("state", "any")
    )
    rank = filters.rank
    if period != "year" or group_by != "none" or not plain or rank not in _NUMBERED_RANKS:
        return {}
    # The other numbered ranks exist only at state level, but regional laws also have the rank
    # "Ley": with scope "any" the count would include them (155 against a highest number of 48
    # for 2015) and the comparison would mislead.
    if rank == "ley" and filters.scope != "state":
        return {}
    assert rank is not None  # implied by the membership test; narrows the type
    # The numbering is the state's: regional laws have their own series of numbers.
    return analytics.highest_official_numbers(
        catalogue.laws, rank_code=rank_code(rank), scope_code=scope_code("state") or ""
    )


def _has_partial(series: TimeSeries) -> bool:
    """Whether any point of the series is partial."""
    return any(p.partial for p in series.points)


def _check_points(series: int, periods: int) -> None:
    """Raise ``ValueError`` when ``series`` series of ``periods`` periods exceed ``MAX_POINTS``."""
    points = series * periods
    if points > MAX_POINTS:
        raise ValueError(
            f"the result would have {points:,} points (limit {MAX_POINTS:,}): "
            "use a coarser period, a shorter range or fewer groups"
        )


def _spikes(series: TimeSeries) -> tuple[analytics.Spike, ...]:
    """The spikes of a series, strongest first."""
    return analytics.find_spikes(series)


def _spike_info(spike: analytics.Spike) -> PeriodSpike:
    """Convert a spike, rounding the numbers to what is worth reading."""
    return PeriodSpike(
        period=spike.bucket.key,
        count=spike.count,
        baseline=round(spike.baseline, 1),
        ratio=round(spike.ratio, 2) if spike.ratio is not None else None,
    )


# --- legislation_over_time ----------------------------------------------------------------------


async def legislation_over_time(
    store: CatalogueStore,
    filters: TrendFilters,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    period: Period = "year",
    date_basis: DateBasis = "published",
    group_by: GroupBy = "none",
    top_groups: int = 5,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> LegislationOverTimeResult:
    """Count the matching laws per period, optionally split by rank, department or scope.

    Raises ``ValueError`` for an inverted range, too many periods or points, a malformed code or
    an argument out of range.
    """
    if group_by not in get_args(GroupBy):
        raise ValueError(f"group_by must be one of {', '.join(get_args(GroupBy))}")
    if not 1 <= top_groups <= MAX_TOP_GROUPS:
        raise ValueError(f"top_groups must be between 1 and {MAX_TOP_GROUPS}")
    p = await _prepare(
        store,
        filters,
        from_date=from_date,
        to_date=to_date,
        date_basis=date_basis,
        period=period,
        progress=progress,
        now=now,
        today=today,
    )
    series = analytics.count_over_time(
        p.matched,
        start=p.start,
        end=p.end,
        period=period,
        date_field=p.field,
        today=p.today,
        settling_days=p.settling_days,
    )
    groups: list[GroupSeries] = []
    if group_by != "none":
        dimension: Dimension = group_by
        groups = [
            GroupSeries(
                code=row.code, label=row.label, total=row.series.total, series=_points(row.series)
            )
            for row in analytics.series_by(
                p.matched,
                dimension,
                start=p.start,
                end=p.end,
                period=period,
                top=top_groups,
                date_field=p.field,
                today=p.today,
                settling_days=p.settling_days,
            )
        ]
    # The ungrouped series and one series per group all span the same periods.
    _check_points(1 + len(groups), len(series.points))
    highest = _coverage_numbers(p.catalogue, filters, period=period, group_by=group_by)
    points = _points(series, highest)
    return LegislationOverTimeResult(
        from_date=p.start,
        to_date=p.end,
        period=period,
        series=points,
        total=series.total,
        undated=series.undated,
        groups=groups,
        # Spikes are always looked for in the ungrouped series: a group's own spike says little
        # about legislation as a whole.
        spikes=[_spike_info(s) for s in _spikes(series)],
        coverage=_coverage(p.catalogue, len(p.matched)),
        caveats=_caveats(
            scope=filters.scope,
            date_basis=date_basis,
            partial=_has_partial(series),
            text_used=_blank_to_none(filters.text) is not None,
            departments=group_by == "department",
            ratio=any(point.highest_number is not None for point in points),
        ),
        provenance=_provenance(p),
    )


# --- top_departments ----------------------------------------------------------------------------


async def top_departments(
    store: CatalogueStore,
    filters: TrendFilters,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    date_basis: DateBasis = "published",
    limit: int = 10,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> TopDepartmentsResult:
    """Rank the departments (or autonomous communities) by laws in the range.

    ``filters.department_code`` must be unset: filtering by department makes no sense when
    ranking departments. Raises ``ValueError`` for invalid arguments.
    """
    if not 1 <= limit <= MAX_DEPARTMENTS:
        raise ValueError(f"limit must be between 1 and {MAX_DEPARTMENTS}")
    if filters.department_code is not None:
        raise ValueError("department_code cannot be combined with ranking departments")
    p = await _prepare(
        store,
        filters,
        from_date=from_date,
        to_date=to_date,
        date_basis=date_basis,
        period=None,
        progress=progress,
        now=now,
        today=today,
    )
    in_range = analytics.filter_laws(p.matched, start=p.start, end=p.end, date_field=p.field)
    counts = analytics.count_by(in_range, "department", top=limit)
    return TopDepartmentsResult(
        from_date=p.start,
        to_date=p.end,
        departments=[
            DepartmentCount(code=g.code, label=g.label, count=g.count, share=round(g.share, 4))
            for g in counts.groups
        ],
        other_count=counts.other_count,
        total=counts.total,
        distinct_departments=counts.distinct,
        coverage=_coverage(p.catalogue, len(p.matched)),
        # No series, so nothing can be partial.
        caveats=_caveats(
            scope=filters.scope,
            date_basis=date_basis,
            partial=False,
            text_used=_blank_to_none(filters.text) is not None,
            departments=True,
        ),
        provenance=_provenance(p),
    )


# --- compare_topics -----------------------------------------------------------------------------


def _check_topics(topics: Sequence[TopicSpec]) -> None:
    """Validate the topic list: 2 to 5 topics, distinct names, at least one criterion each."""
    if not MIN_TOPICS <= len(topics) <= MAX_TOPICS:
        raise ValueError(f"topics must have between {MIN_TOPICS} and {MAX_TOPICS} entries")
    names = [topic.name.strip() for topic in topics]
    if len(set(names)) != len(names):
        raise ValueError("topic names must be different from each other")
    for topic in topics:
        if not any(_blank_to_none(c) for c in (topic.title, topic.text, topic.subject_code)):
            raise ValueError(
                f"topic {topic.name!r} needs at least one criterion: title, text or subject_code"
            )
        _check_codes(("subject_code", topic.subject_code))


async def compare_topics(
    store: CatalogueStore,
    topics: Sequence[TopicSpec],
    *,
    rank: RankFilter | None = None,
    scope: ScopeFilter = "any",
    department_code: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    period: Period = "year",
    date_basis: DateBasis = "published",
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> CompareTopicsResult:
    """Count several topics over the same periods and relate each to all laws.

    ``share_of_all`` compares a topic with every law under the same rank, scope and department.
    Raises ``ValueError`` for an invalid topic list or arguments.
    """
    _check_topics(topics)
    # The shared filters travel in a TrendFilters so that ``_prepare`` validates them and loads
    # the catalogue; the topic criteria are applied below.
    shared = TrendFilters(rank=rank, scope=scope, department_code=department_code)
    p = await _prepare(
        store,
        shared,
        from_date=from_date,
        to_date=to_date,
        date_basis=date_basis,
        period=period,
        progress=progress,
        now=now,
        today=today,
    )
    # Checked before the per-topic searches, which can each cost API requests.
    _check_points(len(topics), len(analytics.bucket_range(p.start, p.end, period)))
    # ``p.matched`` is exactly the reference population: the whole catalogue under the shared
    # filters and no topic criterion.
    members: dict[str, list[LawSummary]] = {}
    for topic in topics:
        selected = await store.select(
            text=_blank_to_none(topic.text),
            subject_code=topic.subject_code,
            progress=p.progress,
        )
        members[topic.name.strip()] = _local_filter(
            selected,
            rank=rank,
            scope=scope,
            department_code=department_code,
            title=topic.title,
        )
    compared = analytics.compare_topics(
        members,
        start=p.start,
        end=p.end,
        period=period,
        reference=p.matched,
        date_field=p.field,
        today=p.today,
        settling_days=p.settling_days,
    )
    return CompareTopicsResult(
        from_date=p.start,
        to_date=p.end,
        period=period,
        topics=[
            TopicTrend(
                name=t.name,
                total=t.series.total,
                share_of_all=round(t.share_of_all, 4) if t.share_of_all is not None else None,
                peak=t.peak.key if t.peak else None,
                first=t.first.key if t.first else None,
                series=_points(t.series),
            )
            for t in compared
        ],
        coverage=_coverage(p.catalogue, len(p.matched)),
        caveats=_caveats(
            scope=scope,
            date_basis=date_basis,
            partial=any(_has_partial(t.series) for t in compared),
            text_used=any(_blank_to_none(topic.text) for topic in topics),
        ),
        provenance=_provenance(p),
    )


# --- explain_spike ------------------------------------------------------------------------------


def _parse_period_key(key: str, period: Period | None) -> tuple[Period, analytics.Bucket]:
    """Turn ``2020``, ``2020-Q2`` or ``2020-03`` into its period type and bucket.

    ``period``, when given, must agree with the key. Raises ``ValueError`` otherwise or for a
    malformed key.
    """
    for pattern, implied in _PERIOD_KEYS:
        found = pattern.fullmatch(key)
        if found is None:
            continue
        if period is not None and period != implied:
            raise ValueError(
                f"period_key {key!r} is a {implied} but period is {period!r}; "
                f"leave period out or set it to {implied!r}"
            )
        year = int(found.group(1))
        if year < _EARLIEST_YEAR:
            raise ValueError(f"period_key year must be {_EARLIEST_YEAR} or later")
        if implied == "year":
            day = date(year, 1, 1)
        elif implied == "quarter":
            day = date(year, (int(found.group(2)) - 1) * 3 + 1, 1)
        else:
            day = date(year, int(found.group(2)), 1)
        return implied, analytics.bucket_for(day, implied)
    raise ValueError(f"period_key {key!r} must look like '2020', '2020-Q2' or '2020-03'")


def _preceding(target: analytics.Bucket, period: Period, count: int) -> list[analytics.Bucket]:
    """The ``count`` buckets immediately before ``target``, oldest first."""
    buckets: list[analytics.Bucket] = []
    cursor = target
    for _ in range(count):
        cursor = analytics.bucket_for(cursor.start - timedelta(days=1), period)
        buckets.append(cursor)
    buckets.reverse()
    return buckets


def _sample(laws: Sequence[LawSummary], count: int) -> list[LawSummary]:
    """At most ``count`` laws spread evenly over ``laws``, the first and the last included."""
    # The first ``count`` laws of a long period are only its first days, which misleads about
    # what the period was about.
    if len(laws) <= count:
        return list(laws)
    if count == 1:
        return [laws[0]]
    last = len(laws) - 1
    return [laws[round(i * last / (count - 1))] for i in range(count)]


async def explain_spike(
    store: CatalogueStore,
    filters: TrendFilters,
    *,
    period_key: str | None = None,
    period: Period | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    date_basis: DateBasis = "published",
    baseline_periods: int = 5,
    max_laws: int = 15,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> ExplainSpikeResult:
    """Explain a period with many laws: which groups grew against the periods before it.

    With ``period_key`` that period is explained; without it, the strongest spike of the range
    is. ``period`` defaults to what the key implies (or ``year``). Raises ``ValueError`` for a
    malformed or contradictory key, or arguments out of range.
    """
    if not MIN_BASELINE_PERIODS <= baseline_periods <= MAX_BASELINE_PERIODS:
        raise ValueError(
            f"baseline_periods must be between {MIN_BASELINE_PERIODS} and {MAX_BASELINE_PERIODS}"
        )
    if not 1 <= max_laws <= MAX_EXPLAINED_LAWS:
        raise ValueError(f"max_laws must be between 1 and {MAX_EXPLAINED_LAWS}")
    target: analytics.Bucket | None = None
    if period_key is not None:
        resolved, target = _parse_period_key(period_key, period)
    else:
        resolved = period if period is not None else "year"
    p = await _prepare(
        store,
        filters,
        from_date=from_date,
        to_date=to_date,
        date_basis=date_basis,
        period=resolved,
        progress=progress,
        now=now,
        today=today,
    )
    series = analytics.count_over_time(
        p.matched,
        start=p.start,
        end=p.end,
        period=resolved,
        date_field=p.field,
        today=p.today,
        settling_days=p.settling_days,
    )
    spikes = list(_spikes(series))
    if target is None:
        if not spikes:
            return _no_spike(p, filters, date_basis)
        target = spikes[0].bucket
    others = [_spike_info(s) for s in spikes if s.bucket != target]
    baseline = _preceding(target, resolved, baseline_periods)
    # A dimension the filters already fix says nothing: with rank='ley' every law has that
    # rank, so its row would only repeat the total. The same holds for one that happens to have
    # a single value among the laws compared (the target and its baseline), which a filter did
    # not fix. Leave both out.
    fixed: dict[Dimension, bool] = {
        "rank": filters.rank is not None,
        "department": filters.department_code is not None,
        "scope": filters.scope != "any",
    }
    # The baseline buckets are the ones just before the target, so one range covers them all.
    compared = analytics.filter_laws(
        p.matched, start=baseline[0].start, end=target.end, date_field=p.field
    )
    dimensions: list[Dimension] = [
        dimension
        for dimension, pinned in fixed.items()
        if not pinned and analytics.count_by(compared, dimension, top=1).distinct > 1
    ]
    explanation = analytics.explain_bucket(
        p.matched,
        target=target,
        baseline=baseline,
        dimensions=dimensions,
        date_field=p.field,
    )
    return ExplainSpikeResult(
        found=True,
        period=target.key,
        count=explanation.count,
        baseline=round(explanation.baseline, 1),
        baseline_periods=explanation.baseline_buckets,
        contributions=[
            SpikeContribution(
                dimension=c.dimension,
                code=c.code,
                label=c.label,
                count=c.count,
                expected=round(c.expected, 1),
                excess=round(c.excess, 1),
            )
            for c in explanation.contributions
        ],
        laws=[brief(law) for law in _sample(explanation.laws, max_laws)],
        laws_total=len(explanation.laws),
        other_spikes=others,
        note="No laws match in this period." if explanation.count == 0 else None,
        coverage=_coverage(p.catalogue, len(p.matched)),
        caveats=_caveats(
            scope=filters.scope,
            date_basis=date_basis,
            # A spike found in the series is a complete period by construction; a period named
            # by the caller may still be running.
            partial=p.today - target.end < timedelta(days=max(p.settling_days, 1)),
            text_used=_blank_to_none(filters.text) is not None,
            departments="department" in dimensions,
        ),
        provenance=_provenance(p),
    )


def _no_spike(p: _Prepared, filters: TrendFilters, date_basis: DateBasis) -> ExplainSpikeResult:
    """The ``found: false`` result for a range in which no period stands out."""
    return ExplainSpikeResult(
        found=False,
        period=None,
        count=None,
        baseline=None,
        baseline_periods=None,
        contributions=[],
        laws=[],
        laws_total=0,
        other_spikes=[],
        note="No period in the range stands out from the others. Try a longer range, a finer "
        "period or fewer filters, or name a period with period_key.",
        coverage=_coverage(p.catalogue, len(p.matched)),
        caveats=_caveats(
            scope=filters.scope,
            date_basis=date_basis,
            partial=False,
            text_used=_blank_to_none(filters.text) is not None,
        ),
        provenance=_provenance(p),
    )


# --- most_amended_laws --------------------------------------------------------------------------


def _amendments_of(analysis: LawAnalysis) -> dict[str, int]:
    """Count the later references that amend the law, per kind (modifies, adds, repeals)."""
    # The kinds come from ``legislation.relation_group``, the classifier that
    # ``get_amendment_history`` groups its items with, so both tools agree on what an
    # amendment is.
    counts = Counter(relation_group(ref.relation.text) for ref in analysis.later)
    return {kind: counts[kind] for kind in _AMENDMENT_GROUPS if counts[kind]}


def _incomplete_note(examined: int, failed: int, candidates: int) -> str:
    """What to tell the caller when not every candidate was examined."""
    if examined + failed == candidates:  # every law was tried; only failures are missing
        return (
            f"{failed} of {candidates} matching laws could not be read. Call again with the "
            "same arguments to retry them."
        )
    note = (
        f"Examined {examined} of {candidates} matching laws. Call again with the "
        "same arguments to continue (results are cached), or narrow the filters."
    )
    return note + (f" {failed} could not be read and will be retried." if failed else "")


async def most_amended_laws(
    store: CatalogueStore,
    client: AnalysisClient,
    filters: TrendFilters,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    date_basis: DateBasis = "published",
    limit: int = 10,
    max_laws: int = 30,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    today: date | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit: float = ANALYSIS_TIME_LIMIT,
) -> MostAmendedLawsResult:
    """Rank the matching laws by how many later laws amended them.

    Examines at most ``max_laws`` laws that are not yet cached, oldest first, within
    ``time_limit`` seconds counted from the start of the call (the loop stops 5 seconds
    earlier); calling again continues, because the analyses are cached. Without ``from_date``
    there is no lower bound, unlike the other trend tools. Raises
    ``ValueError`` for invalid arguments or when no filter narrows the set.
    """
    if not 1 <= limit <= MAX_AMENDED:
        raise ValueError(f"limit must be between 1 and {MAX_AMENDED}")
    if not 1 <= max_laws <= MAX_EXAMINED:
        raise ValueError(f"max_laws must be between 1 and {MAX_EXAMINED}")
    if not any(
        (
            _blank_to_none(filters.title),
            _blank_to_none(filters.text),
            filters.subject_code,
            filters.rank,
            filters.department_code,
        )
    ):
        raise ValueError(
            "give at least one filter (title, text, subject_code, rank or department_code): "
            "ranking the whole collection is not possible"
        )
    # The clock starts here, not after the catalogue is ready: a cold download can take 30 of the
    # 40 seconds, and the client gives up on the whole call, not on the loop (api-notes, third
    # probe, "Bulk download").
    deadline = clock() + time_limit - _TIME_MARGIN
    p = await _prepare(
        store,
        filters,
        from_date=from_date,
        to_date=to_date,
        date_basis=date_basis,
        period=None,
        progress=progress,
        now=now,
        today=today,
        open_start=True,
    )
    in_range = analytics.filter_laws(p.matched, start=p.start, end=p.end, date_field=p.field)
    # Oldest first: an older law has had longer to be amended, so it is the likelier winner, and
    # the order is the same on every call, which is what lets a repeated call continue.
    candidates = sorted(in_range, key=lambda law: (law.published_on or date.max, law.identifier))

    ranked: list[AmendedLaw] = []
    examined = failed = fresh = 0
    for law in candidates:
        cached = await client.has_cached_analysis(law.identifier)
        # A cached analysis costs nothing, so it never counts against the budget; the first
        # uncached law beyond the budget ends the loop (later ones would wait for the next call).
        if not cached and (fresh >= max_laws or clock() >= deadline):
            break
        try:
            analysis = await client.get_law_analysis(law.identifier)
        except BoeError:
            # Any BOE failure, retryable or not, skips just this law: a permanently malformed
            # analysis of one law must not sink the ranking of the others. It is counted in
            # ``failed`` and tried again on the next call. Programming errors are not
            # ``BoeError`` and still propagate.
            failed += 1
            fresh += 1
        else:
            examined += 1
            if not cached:
                fresh += 1
            kinds = _amendments_of(analysis)
            if kinds:
                ranked.append(
                    AmendedLaw(law=brief(law), amendments=sum(kinds.values()), by_kind=kinds)
                )
        if p.progress is not None:
            await p.progress(
                examined + failed,
                None,
                f"Examined {examined + failed} of {len(candidates)} matching laws",
            )
    ranked.sort(key=lambda item: (-item.amendments, item.law.identifier))
    # A law that could not be read is not examined, so the ranking is complete only when every
    # candidate was read.
    complete = examined == len(candidates)
    return MostAmendedLawsResult(
        from_date=from_date,
        to_date=p.end,
        laws=ranked[:limit],
        candidates=len(candidates),
        examined=examined,
        failed=failed,
        complete=complete,
        note=None if complete else _incomplete_note(examined, failed, len(candidates)),
        coverage=_coverage(p.catalogue, len(p.matched)),
        caveats=_caveats(
            scope=filters.scope,
            date_basis=date_basis,
            partial=False,
            text_used=_blank_to_none(filters.text) is not None,
        ),
        provenance=_provenance(p),
    )
