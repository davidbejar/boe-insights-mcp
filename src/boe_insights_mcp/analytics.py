"""Counts over time, rankings and spike explanations over a catalogue of ``LawSummary`` records.

This module is pure: no I/O, no clock, no network and no validation of API data. The MCP tools
of the analytics increment are thin wrappers over these functions, so every number a tool reports
is computed here and nowhere else. It consumes the ``LawSummary`` and ``CodedValue`` models of
``models`` (usually the locally cached catalogue) and returns frozen dataclasses, which the tool
layer converts into its own ``schemas``.

Everything is deterministic: equal inputs give equal outputs, ties included. Every ordering rule
therefore ends in a total order (count, then label, then code, or bucket key).
"""

from __future__ import annotations

import calendar
import re
import statistics
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from boe_insights_mcp.models import CodedValue, LawSummary

__all__ = [
    "MAX_BUCKETS",
    "Bucket",
    "BucketExplanation",
    "Contribution",
    "DateField",
    "Dimension",
    "GroupCount",
    "GroupCounts",
    "GroupSeries",
    "Period",
    "SeriesPoint",
    "Spike",
    "TimeSeries",
    "TopicSeries",
    "bucket_for",
    "bucket_range",
    "compare_topics",
    "count_by",
    "count_over_time",
    "explain_bucket",
    "filter_laws",
    "find_spikes",
    "highest_official_numbers",
    "series_by",
    "title_matcher",
]

Period = Literal["year", "quarter", "month"]
DateField = Literal["published_on", "enacted_on"]
Dimension = Literal["rank", "department", "scope"]

# 600 buckets allow 50 years by month; beyond that a chart is unreadable and the series is
# needlessly large for a model to read.
MAX_BUCKETS = 600
# A spike needs enough history to be called unusual (see ``find_spikes``).
_MIN_COMPLETE_BUCKETS = 4
# Label of the aggregated row of ``series_by``.
_OTHER_LABEL = "other"
# Two autonomous communities changed their department code when their statute or name changed, so
# one institution appears under two codes in the catalogue (api-notes, third probe, "Departments
# that changed code"). Maps the old code to the newer one, which is the code and label reported.
_RECODED_DEPARTMENTS = {
    "8161": "8162",  # Comunidad Valenciana until 2006, Comunitat Valenciana since
    "8120": "8121",  # Islas Baleares until 1998, Illes Balears since
}


@dataclass(frozen=True)
class Bucket:
    """One calendar period: its sortable key and its first and last day (inclusive)."""

    key: str
    start: date
    end: date


@dataclass(frozen=True)
class SeriesPoint:
    """The number of laws in one bucket.

    ``partial`` means the count is not comparable with the others: the bucket is cut by the
    requested range, or it has not finished yet.
    """

    bucket: Bucket
    count: int
    partial: bool


@dataclass(frozen=True)
class TimeSeries:
    """Counts per bucket plus the bookkeeping that explains the laws that were not counted."""

    period: Period
    points: tuple[SeriesPoint, ...]
    total: int
    """Sum of the point counts."""
    undated: int
    """Input laws whose date field is ``None``."""
    outside: int
    """Input laws dated outside the requested range."""


@dataclass(frozen=True)
class GroupCount:
    """One group of ``count_by``: a code, its label, its size and its share."""

    code: str
    label: str
    count: int
    share: float
    """``count`` over the laws that have a value, unrounded, in ``0.0..1.0``."""


@dataclass(frozen=True)
class GroupCounts:
    """The result of ``count_by``; ``sum(groups) + other_count + missing == total``."""

    dimension: Dimension
    groups: tuple[GroupCount, ...]
    other_count: int
    """Laws in the groups beyond ``top``."""
    missing: int
    """Laws whose value for the dimension is ``None``."""
    total: int
    distinct: int
    """Number of distinct codes seen."""


@dataclass(frozen=True)
class GroupSeries:
    """The time series of one group; ``code`` is ``None`` for the aggregated "other" row."""

    code: str | None
    label: str
    series: TimeSeries


@dataclass(frozen=True)
class Spike:
    """A complete bucket that stands out from the other complete buckets."""

    bucket: Bucket
    count: int
    baseline: float
    """Median count of the other complete buckets."""
    ratio: float | None
    """``count / baseline``; ``None`` when the baseline is zero."""
    excess: float
    """``count - baseline``."""


@dataclass(frozen=True)
class Contribution:
    """How much one group of one dimension grew in the target bucket against its baseline."""

    dimension: Dimension
    code: str
    label: str
    count: int
    """Laws of this group in the target bucket."""
    expected: float
    """The group's mean count per baseline bucket."""
    excess: float
    """``count - expected``."""


@dataclass(frozen=True)
class BucketExplanation:
    """What was different about one bucket, as the groups that grew most against a baseline."""

    bucket: Bucket
    count: int
    baseline: float
    """Mean count of the baseline buckets."""
    baseline_buckets: int
    contributions: tuple[Contribution, ...]
    laws: tuple[LawSummary, ...]
    """Every law of the bucket, by date and then identifier."""


@dataclass(frozen=True)
class TopicSeries:
    """The time series of one named topic with the summary facts the tools report."""

    name: str
    series: TimeSeries
    peak: Bucket | None
    """The complete bucket with the highest count (earliest on ties); ``None`` if all are zero."""
    first: Bucket | None
    """The earliest bucket with a non-zero count, complete or not."""
    share_of_all: float | None
    """``series.total`` over the reference total in range, when a reference is given."""


# --- periods and buckets ------------------------------------------------------------------------


def bucket_for(day: date, period: Period) -> Bucket:
    """The bucket of the given period that contains ``day``."""
    if period == "year":
        return Bucket(str(day.year), date(day.year, 1, 1), date(day.year, 12, 31))
    if period == "quarter":
        quarter = (day.month - 1) // 3
        first_month = quarter * 3 + 1
        last_month = first_month + 2
        return Bucket(
            f"{day.year}-Q{quarter + 1}",
            date(day.year, first_month, 1),
            date(day.year, last_month, calendar.monthrange(day.year, last_month)[1]),
        )
    return Bucket(
        f"{day.year}-{day.month:02d}",
        date(day.year, day.month, 1),
        date(day.year, day.month, calendar.monthrange(day.year, day.month)[1]),
    )


def bucket_range(start: date, end: date, period: Period) -> list[Bucket]:
    """Every bucket overlapping ``[start, end]``, contiguous and ascending.

    Raises ``ValueError`` when ``start > end`` or when the range needs more than ``MAX_BUCKETS``
    buckets.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    buckets = [bucket_for(start, period)]
    # Stopping on ``end`` (and never computing the day after it) keeps ``date.max`` safe, and
    # raising inside the loop bounds the work for an absurd range.
    while buckets[-1].end < end:
        if len(buckets) == MAX_BUCKETS:
            raise ValueError(
                f"the range needs more than {MAX_BUCKETS} buckets; use a coarser period"
            )
        buckets.append(bucket_for(buckets[-1].end + timedelta(days=1), period))
    return buckets


# --- filtering ----------------------------------------------------------------------------------


def _law_date(law: LawSummary, date_field: DateField) -> date | None:
    """The law's date in the chosen field, ``None`` when it has none."""
    return law.published_on if date_field == "published_on" else law.enacted_on


def _raw_coded(law: LawSummary, dimension: Dimension) -> CodedValue | None:
    """The coded value exactly as the catalogue holds it, ``None`` when the law has none."""
    if dimension == "rank":
        return law.rank
    if dimension == "department":
        return law.department
    return law.scope


def _canonical_code(dimension: Dimension, code: str) -> str:
    """The code a value is grouped and filtered under (recoded departments share one)."""
    return _RECODED_DEPARTMENTS.get(code, code) if dimension == "department" else code


def _coded(law: LawSummary, dimension: Dimension) -> CodedValue | None:
    """The law's coded value for the dimension, under its canonical code; ``None`` if absent."""
    value = _raw_coded(law, dimension)
    if value is None:
        return None
    code = _canonical_code(dimension, value.code)
    return value if code == value.code else CodedValue(code=code, text=value.text)


def _fold(word: str) -> str:
    """Lower-case a word and strip its accents, keeping ``ñ`` distinct from ``n``."""
    # Spanish readers type "proteccion" for "Protección", but "ano" and "año" (year versus
    # anus) and "pingüino" versus "pinguino" must not be confused where it matters: only the
    # tilde on an n is a different letter, every other accent is dropped. Casefold first so
    # that "Ñ" reaches the NFD step as lower case.
    kept: list[str] = []
    for char in unicodedata.normalize("NFD", word.casefold()):
        if unicodedata.combining(char) and not (char == "̃" and kept and kept[-1] == "n"):
            continue
        kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept))


def _words(text: str) -> list[str]:
    """The folded words of a text: runs of letters and digits, everything else separates."""
    # NFC first so that a decomposed "n" + combining tilde is the single letter "ñ" (and any
    # other decomposed accented letter is one character that ``isalnum`` accepts).
    words: list[str] = []
    current: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        if char.isalnum():
            current.append(char)
        elif current:
            words.append(_fold("".join(current)))
            current = []
    if current:
        words.append(_fold("".join(current)))
    return words


_VOWELS = frozenset("aeiou")


def _singular(word: str) -> str:
    """Reduce a folded word to a form shared by its singular and its plural.

    Removes one plural ending: a final "es" when the word is longer than 4 letters and the
    letter before it is a consonant ("leyes" -> "ley", "ciudades" -> "ciudad"), otherwise a
    final "s" when the word is longer than 3 letters ("viviendas" -> "vivienda"). Short words
    are left alone so that "mes" and "mesa", or "ley" and "legal", never meet. Applied to both
    the query and the title, so the reverse direction ("viviendas" finds "vivienda") works too.
    """
    # The letter before "es" must be a consonant so that "es" reads as a plural ending rather
    # than the tail of a stem after a vowel ("pie" -> "pies" only loses its "s"). Accents are
    # already folded away, so the five plain vowels are the whole set; a digit is not a
    # consonant. The rule is a heuristic: the plural of a word in -e after a consonant ("base",
    # "bases") loses its whole "es" and no longer meets its singular.
    if len(word) > 4 and word.endswith("es") and word[-3].isalpha() and word[-3] not in _VOWELS:
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _stems(text: str) -> set[str]:
    """The reduced forms of the folded words of a text (see ``_singular``)."""
    return {_singular(word) for word in _words(text)}


def title_matcher(words: str | None) -> Callable[[str], bool] | None:
    """A predicate telling whether a title has all of ``words`` as whole words.

    Matching is the one ``filter_laws`` applies to titles: ignoring case, accents (but not ``ñ``)
    and punctuation, whole words, with a word's singular and plural forms matching each other.
    Returns ``None`` when ``words`` is ``None`` or holds no word at all, which means "no
    constraint", so callers can tell an absent filter from one that matches nothing.
    """
    wanted = _stems(words) if words is not None else set()
    if not wanted:
        return None
    return lambda title: wanted <= _stems(title)


def filter_laws(
    laws: Iterable[LawSummary],
    *,
    rank_codes: Collection[str] | None = None,
    department_codes: Collection[str] | None = None,
    scope_codes: Collection[str] | None = None,
    identifiers: Collection[str] | None = None,
    title_words: str | None = None,
    start: date | None = None,
    end: date | None = None,
    date_field: DateField = "published_on",
) -> list[LawSummary]:
    """The laws that satisfy every given constraint, in input order.

    ``None`` means no constraint and an empty collection matches nothing. A law lacking a
    rank, department, scope or date fails a constraint on that field. ``title_words`` must all
    occur as whole words, ignoring case, accents (but not ``ñ``) and punctuation; singular and
    plural forms match each other.
    """
    wanted_words = _stems(title_words) if title_words is not None else set()
    # A department filter names the institution, whichever of its codes the caller knows.
    wanted_departments = (
        {_canonical_code("department", code) for code in department_codes}
        if department_codes is not None
        else None
    )
    result: list[LawSummary] = []
    for law in laws:
        if not _in_codes(law.rank, rank_codes):
            continue
        if not _in_codes(_coded(law, "department"), wanted_departments):
            continue
        if not _in_codes(law.scope, scope_codes):
            continue
        if identifiers is not None and law.identifier not in identifiers:
            continue
        if wanted_words and not wanted_words <= _stems(law.title):
            continue
        if start is not None or end is not None:
            day = _law_date(law, date_field)
            if (
                day is None
                or (start is not None and day < start)
                or (end is not None and day > end)
            ):
                continue
        result.append(law)
    return result


def _in_codes(value: CodedValue | None, codes: Collection[str] | None) -> bool:
    """Whether a coded value satisfies a code constraint (``None`` is no constraint)."""
    if codes is None:
        return True
    return value is not None and value.code in codes


# --- counting over time -------------------------------------------------------------------------


def count_over_time(
    laws: Iterable[LawSummary],
    *,
    start: date,
    end: date,
    period: Period,
    date_field: DateField = "published_on",
    today: date | None = None,
    settling_days: int = 0,
) -> TimeSeries:
    """Count the laws dated in ``[start, end]`` per bucket, zeros included.

    A bucket that ended fewer than ``settling_days`` days before ``today`` is partial too, for
    dates that keep being filled in after the fact. Raises whatever ``bucket_range`` raises for
    the same range.
    """
    buckets = bucket_range(start, end, period)
    per_key: Counter[str] = Counter()
    undated = outside = 0
    for law in laws:
        day = _law_date(law, date_field)
        if day is None:
            undated += 1
        elif start <= day <= end:
            per_key[bucket_for(day, period).key] += 1
        else:
            outside += 1
    # A bucket is not comparable with the others when the range cuts it (only part of it was
    # counted) or when it has not finished (``end >= today``: the day is still going, so the
    # bucket containing today and every later one is incomplete). ``max(.., 1)`` makes a
    # settling time of zero mean exactly that.
    wait = timedelta(days=max(settling_days, 1))
    points = tuple(
        SeriesPoint(
            bucket=b,
            count=per_key[b.key],
            partial=b.start < start or b.end > end or (today is not None and today - b.end < wait),
        )
        for b in buckets
    )
    return TimeSeries(
        period=period,
        points=points,
        total=sum(per_key.values()),
        undated=undated,
        outside=outside,
    )


# --- grouping -----------------------------------------------------------------------------------


def _count_codes(laws: Iterable[LawSummary], dimension: Dimension) -> Counter[str]:
    """Number of laws per code of the dimension; laws without a value are not counted."""
    # Keyed by code, never by text: a department's name varies slightly over the years.
    counts: Counter[str] = Counter()
    for law in laws:
        value = _coded(law, dimension)
        if value is not None:
            counts[value.code] += 1
    return counts


def _label_codes(laws: Iterable[LawSummary], dimension: Dimension) -> dict[str, str]:
    """The label of each code: its most frequent text; ties go to the text seen first."""
    # A recoded department is labelled with the text of its newer code; the old code's text is
    # used only when no law carries the newer one.
    native: dict[str, Counter[str]] = {}
    recoded: dict[str, Counter[str]] = {}
    for law in laws:
        raw = _raw_coded(law, dimension)
        if raw is not None:
            code = _canonical_code(dimension, raw.code)
            (native if code == raw.code else recoded).setdefault(code, Counter())[raw.text] += 1
    # ``Counter`` keeps insertion order and ``max`` returns the first maximum, which is the
    # first-seen rule.
    return {
        code: max(counter, key=counter.__getitem__)
        for code, counter in {**recoded, **native}.items()
    }


def _ranked_codes(counts: Counter[str], labels: Mapping[str, str]) -> list[str]:
    """Codes ordered by count descending, then label, then code (a total order)."""
    return sorted(counts, key=lambda code: (-counts[code], labels[code], code))


def _require_top(top: int) -> None:
    """Raise ``ValueError`` unless ``top`` is at least 1."""
    if top < 1:
        raise ValueError(f"top must be at least 1, got {top}")


def count_by(laws: Iterable[LawSummary], dimension: Dimension, *, top: int = 10) -> GroupCounts:
    """Count the laws per code of the dimension and keep the ``top`` largest groups.

    Raises ``ValueError`` when ``top < 1``.
    """
    _require_top(top)
    materialised = list(laws)
    counts = _count_codes(materialised, dimension)
    labels = _label_codes(materialised, dimension)
    ordered = _ranked_codes(counts, labels)
    counted = sum(counts.values())
    # The share is relative to the laws that have a value: a law with no rank is not a member
    # of any group, so counting it would make the shares add up to less than one.
    groups = tuple(
        GroupCount(code, labels[code], counts[code], counts[code] / counted)
        for code in ordered[:top]
    )
    return GroupCounts(
        dimension=dimension,
        groups=groups,
        other_count=sum(counts[code] for code in ordered[top:]),
        missing=len(materialised) - counted,
        total=len(materialised),
        distinct=len(counts),
    )


def series_by(
    laws: Iterable[LawSummary],
    dimension: Dimension,
    *,
    start: date,
    end: date,
    period: Period,
    top: int = 5,
    date_field: DateField = "published_on",
    today: date | None = None,
    settling_days: int = 0,
) -> tuple[GroupSeries, ...]:
    """One time series per top group, then an "other" row when it would not be empty.

    The top groups are those with the most laws inside ``[start, end]``, ordered as in
    ``count_by``. The result is ``()`` when no law with a value falls in the range. Raises
    ``ValueError`` when ``top < 1`` or the range is invalid.
    """
    _require_top(top)
    bucket_range(start, end, period)  # validate even when there is nothing to count
    materialised = list(laws)
    in_range = [
        law
        for law in materialised
        if (day := _law_date(law, date_field)) is not None and start <= day <= end
    ]
    # The ranking looks only at the range: a group that was big years ago must not take a row
    # from one that is big in the period being analysed.
    counts = _count_codes(in_range, dimension)
    labels = _label_codes(in_range, dimension)
    top_codes = _ranked_codes(counts, labels)[:top]
    kept = set(top_codes)

    def series_of(members: Iterable[LawSummary]) -> TimeSeries:
        """The time series of a subset of laws over the shared range."""
        return count_over_time(
            members,
            start=start,
            end=end,
            period=period,
            date_field=date_field,
            today=today,
            settling_days=settling_days,
        )

    rows = [
        GroupSeries(
            code=code,
            label=labels[code],
            series=series_of(
                law
                for law in materialised
                if (value := _coded(law, dimension)) is not None and value.code == code
            ),
        )
        for code in top_codes
    ]
    # The "other" row takes every law that is not in a top group (also the undated and
    # out-of-range ones, which only show up in its ``undated``/``outside``), so that the rows
    # always add up to ``count_over_time`` of the whole input. It is shown only when it
    # holds something inside the range.
    others = [
        law
        for law in materialised
        if (value := _coded(law, dimension)) is None or value.code not in kept
    ]
    other_series = series_of(others)
    if other_series.total:
        rows.append(GroupSeries(code=None, label=_OTHER_LABEL, series=other_series))
    return tuple(rows)


# --- coverage ----------------------------------------------------------------------------------

_OFFICIAL_NUMBER = re.compile(r"(\d+)/(\d{4})")


def highest_official_numbers(
    laws: Iterable[LawSummary], *, rank_code: str, scope_code: str
) -> dict[int, int]:
    """The highest official number "N/YYYY" seen per year among the laws of one rank and scope.

    The year is the one inside the number, not the publication year. The result is a lower
    bound of how many laws of that rank were enacted that year, so a caller can compare it with
    how many the catalogue holds. Laws without a number of that form are ignored.
    """
    highest: dict[int, int] = {}
    for law in laws:
        if law.rank is None or law.rank.code != rank_code:
            continue
        if law.scope is None or law.scope.code != scope_code or law.official_number is None:
            continue
        found = _OFFICIAL_NUMBER.fullmatch(law.official_number.strip())
        if found is None:
            continue
        year, number = int(found.group(2)), int(found.group(1))
        highest[year] = max(highest.get(year, 0), number)
    return highest


# --- spikes -------------------------------------------------------------------------------------


def find_spikes(
    series: TimeSeries, *, min_ratio: float = 1.5, min_excess: int = 5, limit: int = 5
) -> tuple[Spike, ...]:
    """The complete buckets that stand well above the other complete buckets.

    Raises ``ValueError`` when ``limit < 1``. Fewer than four complete buckets give no spikes.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    # Partial buckets are neither candidates nor baseline: their counts are cut or unfinished,
    # so they would look like dips (or hide a real spike) rather than activity.
    complete = [p for p in series.points if not p.partial]
    if len(complete) < _MIN_COMPLETE_BUCKETS:
        return ()
    spikes: list[Spike] = []
    for index, point in enumerate(complete):
        # Leave-one-out: the candidate is left out of its own baseline, otherwise a huge bucket
        # would drag the median up and hide itself.
        others = [p.count for i, p in enumerate(complete) if i != index]
        baseline = float(statistics.median(others))
        excess = point.count - baseline
        ratio = None if baseline == 0 else point.count / baseline
        # With a zero baseline there is no ratio to compare, so the excess alone decides.
        if excess >= min_excess and (ratio is None or ratio >= min_ratio):
            spikes.append(Spike(point.bucket, point.count, baseline, ratio, excess))
    spikes.sort(key=lambda s: (-s.excess, s.bucket.key))
    return tuple(spikes[:limit])


def _merge_buckets(buckets: Iterable[Bucket]) -> tuple[list[date], list[date]]:
    """The starts and ends of the buckets merged into disjoint, ascending intervals."""
    starts: list[date] = []
    ends: list[date] = []
    for bucket in sorted(buckets, key=lambda b: (b.start, b.end)):
        if ends and bucket.start <= ends[-1] + timedelta(days=1):
            ends[-1] = max(ends[-1], bucket.end)
        else:
            starts.append(bucket.start)
            ends.append(bucket.end)
    return starts, ends


def explain_bucket(
    laws: Iterable[LawSummary],
    *,
    target: Bucket,
    baseline: Sequence[Bucket],
    dimensions: Sequence[Dimension] = ("rank", "department", "scope"),
    date_field: DateField = "published_on",
    top: int = 5,
) -> BucketExplanation:
    """Which groups grew most in ``target`` compared with their mean over ``baseline``.

    ``target`` is ignored inside ``baseline`` and duplicate baseline buckets count once. Only
    groups with a positive excess are reported, ``top`` per dimension. Raises ``ValueError``
    when ``top < 1`` or the effective baseline is empty.
    """
    _require_top(top)
    baseline_buckets = list(dict.fromkeys(b for b in baseline if b != target))
    if not baseline_buckets:
        raise ValueError("the baseline has no bucket other than the target")
    starts, ends = _merge_buckets(baseline_buckets)

    def in_baseline(day: date) -> bool:
        """Whether the day falls in any baseline bucket (binary search over merged intervals)."""
        i = bisect_right(starts, day) - 1
        return i >= 0 and day <= ends[i]

    in_target: list[LawSummary] = []
    in_base: list[LawSummary] = []
    relevant: list[LawSummary] = []  # both sets, in input order, for the labels
    for law in laws:
        day = _law_date(law, date_field)
        if day is None:
            continue
        # ``elif``: a law of the target is never also a baseline law, even if a baseline bucket
        # overlapped the target, so the target is not compared with itself.
        if target.start <= day <= target.end:
            in_target.append(law)
        elif in_baseline(day):
            in_base.append(law)
        else:
            continue
        relevant.append(law)
    candidates: list[tuple[float, int, str, str, Contribution]] = []
    for order, dimension in enumerate(dimensions):
        target_counts = _count_codes(in_target, dimension)
        base_counts = _count_codes(in_base, dimension)
        # Labels come from the target and the baseline together, so one renamed group has one
        # label whichever side saw more of it.
        labels = _label_codes(relevant, dimension)
        grown: list[Contribution] = []
        for code, count in target_counts.items():
            expected = base_counts[code] / len(baseline_buckets)
            if count - expected > 0:
                grown.append(
                    Contribution(dimension, code, labels[code], count, expected, count - expected)
                )
        grown.sort(key=lambda c: (-c.excess, c.label, c.code))
        candidates.extend((c.excess, order, c.label, c.code, c) for c in grown[:top])
    # Across dimensions: excess descending, then the dimension order as given, then label and code.
    candidates.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    return BucketExplanation(
        bucket=target,
        count=len(in_target),
        baseline=len(in_base) / len(baseline_buckets),
        baseline_buckets=len(baseline_buckets),
        contributions=tuple(t[4] for t in candidates),
        # Every target law is dated, so ``date.min`` only satisfies the type checker.
        laws=tuple(
            sorted(
                in_target,
                key=lambda law: (_law_date(law, date_field) or date.min, law.identifier),
            )
        ),
    )


# --- comparing topics ---------------------------------------------------------------------------


def compare_topics(
    topics: Mapping[str, Iterable[LawSummary]],
    *,
    start: date,
    end: date,
    period: Period,
    reference: Iterable[LawSummary] | None = None,
    date_field: DateField = "published_on",
    today: date | None = None,
    settling_days: int = 0,
) -> tuple[TopicSeries, ...]:
    """One ``TopicSeries`` per topic, in the mapping's order; topics are counted independently.

    ``share_of_all`` is the topic's total over the reference's total in the same range, ``None``
    without a reference or when that total is zero.
    """
    reference_total: int | None = None
    if reference is not None:
        reference_total = count_over_time(
            reference, start=start, end=end, period=period, date_field=date_field
        ).total
    result: list[TopicSeries] = []
    for name, members in topics.items():
        series = count_over_time(
            members,
            start=start,
            end=end,
            period=period,
            date_field=date_field,
            today=today,
            settling_days=settling_days,
        )
        # ``peak`` looks only at complete buckets, because a cut or unfinished bucket is low
        # for a reason that is not interest in the topic; ``first`` is about when the topic
        # appeared at all, so a partial bucket counts.
        complete = [p for p in series.points if not p.partial]
        best = max(complete, key=lambda p: p.count, default=None)  # first maximum: earliest
        first = next((p for p in series.points if p.count > 0), None)
        result.append(
            TopicSeries(
                name=name,
                series=series,
                peak=best.bucket if best is not None and best.count > 0 else None,
                first=first.bucket if first is not None else None,
                share_of_all=series.total / reference_total if reference_total else None,
            )
        )
    return tuple(result)
