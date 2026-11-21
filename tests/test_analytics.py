"""Contract tests for ``boe_insights_mcp.analytics``: buckets, filtering, counting, spikes.

Everything under test is pure logic over ``LawSummary`` records, so the tests build those records
directly with the small factories below. No network, no clock (``today`` is always passed in) and
no unseeded randomness. Tests that rest on a reading of an ambiguous rule carry a comment
``AMBIGUOUS RULE`` that names the function, so they are easy to find and revisit.
"""

from __future__ import annotations

import dataclasses
import itertools
import random
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import pytest

from boe_insights_mcp.analytics import (
    MAX_BUCKETS,
    Bucket,
    BucketExplanation,
    Contribution,
    Dimension,
    GroupCount,
    GroupCounts,
    GroupSeries,
    Period,
    SeriesPoint,
    Spike,
    TimeSeries,
    TopicSeries,
    bucket_for,
    bucket_range,
    compare_topics,
    count_by,
    count_over_time,
    explain_bucket,
    filter_laws,
    find_spikes,
    highest_official_numbers,
    series_by,
)
from boe_insights_mcp.models import CodedValue, LawSummary

# --- factories ----------------------------------------------------------------------------------

Coded = CodedValue | str | None


def coded(value: Coded) -> CodedValue | None:
    """A bare string becomes a ``CodedValue`` whose text is ``Label <code>``."""
    if isinstance(value, str):
        return CodedValue(code=value, text=f"Label {value}")
    return value


def make_law(
    identifier: str = "BOE-A-0001",
    *,
    title: str = "Ley de prueba",
    published: date | None = None,
    enacted: date | None = None,
    rank: Coded = None,
    department: Coded = None,
    scope: Coded = None,
    number: str | None = None,
) -> LawSummary:
    return LawSummary(
        identifier=identifier,
        title=title,
        published_on=published,
        enacted_on=enacted,
        rank=coded(rank),
        department=coded(department),
        scope=coded(scope),
        official_number=number,
    )


def laws_on(*days: date | None, rank: Coded = None, prefix: str = "L") -> list[LawSummary]:
    """One law per day (``None`` gives an undated law), with sequential identifiers."""
    return [make_law(f"{prefix}{i:03d}", published=day, rank=rank) for i, day in enumerate(days)]


def month(m: int, year: int = 2020) -> Bucket:
    return bucket_for(date(year, m, 1), "month")


def counts_of(series: TimeSeries) -> tuple[int, ...]:
    return tuple(p.count for p in series.points)


def partials_of(series: TimeSeries) -> tuple[bool, ...]:
    return tuple(p.partial for p in series.points)


def keys_of(series: TimeSeries) -> tuple[str, ...]:
    return tuple(p.bucket.key for p in series.points)


def make_series(
    counts: Iterable[int], partial: Iterable[int] = (), period: Period = "month"
) -> TimeSeries:
    """A monthly series starting 2000-01 with the given counts; ``partial`` lists point indexes."""
    values = list(counts)
    partial_at = set(partial)
    points = tuple(
        SeriesPoint(
            bucket=bucket_for(date(2000 + i // 12, i % 12 + 1, 1), "month"),
            count=c,
            partial=i in partial_at,
        )
        for i, c in enumerate(values)
    )
    return TimeSeries(period=period, points=points, total=sum(values), undated=0, outside=0)


def ids_of(laws: Iterable[LawSummary]) -> list[str]:
    return [law.identifier for law in laws]


# --- bucket_for ---------------------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2020, 1, 1), date(2020, 6, 15), date(2020, 12, 31)])
def test_bucket_for_year_spans_the_calendar_year(day: date) -> None:
    assert bucket_for(day, "year") == Bucket("2020", date(2020, 1, 1), date(2020, 12, 31))


@pytest.mark.parametrize(
    ("day", "key", "start", "end"),
    [
        (date(2020, 1, 1), "2020-Q1", date(2020, 1, 1), date(2020, 3, 31)),
        (date(2020, 3, 31), "2020-Q1", date(2020, 1, 1), date(2020, 3, 31)),
        (date(2020, 4, 1), "2020-Q2", date(2020, 4, 1), date(2020, 6, 30)),
        (date(2020, 6, 30), "2020-Q2", date(2020, 4, 1), date(2020, 6, 30)),
        (date(2020, 7, 1), "2020-Q3", date(2020, 7, 1), date(2020, 9, 30)),
        (date(2020, 9, 30), "2020-Q3", date(2020, 7, 1), date(2020, 9, 30)),
        (date(2020, 10, 1), "2020-Q4", date(2020, 10, 1), date(2020, 12, 31)),
        (date(2020, 12, 31), "2020-Q4", date(2020, 10, 1), date(2020, 12, 31)),
        (date(2021, 2, 14), "2021-Q1", date(2021, 1, 1), date(2021, 3, 31)),
    ],
)
def test_bucket_for_quarter_boundaries(day: date, key: str, start: date, end: date) -> None:
    assert bucket_for(day, "quarter") == Bucket(key, start, end)


@pytest.mark.parametrize(
    ("day", "key", "last_day"),
    [
        (date(2020, 1, 15), "2020-01", date(2020, 1, 31)),
        (date(2020, 2, 10), "2020-02", date(2020, 2, 29)),  # leap year
        (date(2021, 2, 10), "2021-02", date(2021, 2, 28)),
        (date(1900, 2, 10), "1900-02", date(1900, 2, 28)),  # century, not a leap year
        (date(2000, 2, 10), "2000-02", date(2000, 2, 29)),  # century divisible by 400
        (date(2020, 4, 30), "2020-04", date(2020, 4, 30)),
        (date(2020, 9, 1), "2020-09", date(2020, 9, 30)),
        (date(2020, 12, 31), "2020-12", date(2020, 12, 31)),
    ],
)
def test_bucket_for_month_keys_and_last_day(day: date, key: str, last_day: date) -> None:
    bucket = bucket_for(day, "month")
    assert bucket.key == key
    assert bucket.start == day.replace(day=1)
    assert bucket.end == last_day


@pytest.mark.parametrize("period", ["year", "quarter", "month"])
def test_bucket_for_contains_its_day_and_is_stable_over_two_years(period: Period) -> None:
    day = date(2020, 1, 1)
    while day <= date(2021, 12, 31):
        bucket = bucket_for(day, period)
        assert bucket.start <= day <= bucket.end
        assert bucket_for(bucket.start, period) == bucket
        assert bucket_for(bucket.end, period) == bucket
        day += timedelta(days=1)


@pytest.mark.parametrize("period", ["year", "quarter", "month"])
def test_bucket_for_next_period_starts_the_day_after_the_end(period: Period) -> None:
    bucket = bucket_for(date(2020, 5, 5), period)
    following = bucket_for(bucket.end + timedelta(days=1), period)
    assert following.start == bucket.end + timedelta(days=1)
    assert following.key > bucket.key


def test_bucket_is_frozen_and_hashable() -> None:
    bucket = bucket_for(date(2020, 5, 5), "month")
    with pytest.raises(dataclasses.FrozenInstanceError):
        bucket.key = "x"  # type: ignore[misc]
    assert {bucket: 1}[bucket_for(date(2020, 5, 31), "month")] == 1


# --- bucket_range -------------------------------------------------------------------------------


def test_bucket_range_single_day_is_one_whole_bucket() -> None:
    result = bucket_range(date(2020, 3, 10), date(2020, 3, 10), "month")
    assert result == [Bucket("2020-03", date(2020, 3, 1), date(2020, 3, 31))]


def test_bucket_range_first_and_last_buckets_are_whole_periods() -> None:
    result = bucket_range(date(2020, 2, 15), date(2020, 5, 10), "month")
    assert [b.key for b in result] == ["2020-02", "2020-03", "2020-04", "2020-05"]
    assert result[0].start == date(2020, 2, 1)
    assert result[-1].end == date(2020, 5, 31)


def test_bucket_range_end_on_first_day_of_a_period_includes_that_period() -> None:
    result = bucket_range(date(2020, 1, 1), date(2020, 4, 1), "quarter")
    assert [b.key for b in result] == ["2020-Q1", "2020-Q2"]


def test_bucket_range_end_on_last_day_of_a_period_does_not_add_the_next() -> None:
    result = bucket_range(date(2020, 1, 1), date(2020, 3, 31), "quarter")
    assert [b.key for b in result] == ["2020-Q1"]


def test_bucket_range_quarters_across_a_year_end() -> None:
    result = bucket_range(date(2019, 11, 20), date(2020, 2, 3), "quarter")
    assert [b.key for b in result] == ["2019-Q4", "2020-Q1"]


def test_bucket_range_years() -> None:
    result = bucket_range(date(2018, 12, 31), date(2021, 1, 1), "year")
    assert [b.key for b in result] == ["2018", "2019", "2020", "2021"]


def test_bucket_range_months_across_leap_february() -> None:
    result = bucket_range(date(2020, 1, 31), date(2020, 3, 1), "month")
    assert [b.key for b in result] == ["2020-01", "2020-02", "2020-03"]
    assert result[1].end == date(2020, 2, 29)


@pytest.mark.parametrize("period", ["year", "quarter", "month"])
def test_bucket_range_is_contiguous_ascending_and_sorted_by_key(period: Period) -> None:
    result = bucket_range(date(2017, 8, 9), date(2023, 2, 14), period)
    for previous, current in itertools.pairwise(result):
        assert current.start == previous.end + timedelta(days=1)
        assert current.key > previous.key
    assert [b.key for b in result] == sorted(b.key for b in result)
    assert result[0].start <= date(2017, 8, 9) <= result[0].end
    assert result[-1].start <= date(2023, 2, 14) <= result[-1].end


def test_bucket_range_start_after_end_raises() -> None:
    with pytest.raises(ValueError):
        bucket_range(date(2020, 1, 2), date(2020, 1, 1), "month")


def test_max_buckets_constant_is_600() -> None:
    assert MAX_BUCKETS == 600


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("month", date(2000, 1, 1), date(2049, 12, 31)),  # exactly 600 months
        ("quarter", date(1900, 1, 1), date(2049, 12, 31)),  # exactly 600 quarters
        ("year", date(1500, 1, 1), date(2099, 12, 31)),  # exactly 600 years
    ],
)
def test_bucket_range_allows_exactly_max_buckets(period: Period, start: date, end: date) -> None:
    assert len(bucket_range(start, end, period)) == 600


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("month", date(2000, 1, 1), date(2050, 1, 1)),  # 601 months
        ("quarter", date(1900, 1, 1), date(2050, 1, 1)),  # 601 quarters
        ("year", date(1500, 1, 1), date(2100, 1, 1)),  # 601 years
    ],
)
def test_bucket_range_over_max_buckets_asks_for_a_coarser_period(
    period: Period, start: date, end: date
) -> None:
    with pytest.raises(ValueError, match="coarser"):
        bucket_range(start, end, period)


# --- filter_laws --------------------------------------------------------------------------------


@pytest.fixture
def catalogue() -> list[LawSummary]:
    """Six laws that differ in every field, in a deliberate (non-sorted) order."""
    return [
        make_law(
            "C",
            title="Ley de Protección de Datos",
            published=date(2018, 12, 6),
            enacted=date(2018, 12, 5),
            rank="1300",
            department="9564",
            scope="1",
        ),
        make_law(
            "A",
            title="Real Decreto sobre obras",
            published=date(2020, 3, 1),
            enacted=date(2020, 2, 27),
            rank="1200",
            department="9564",
            scope="2",
        ),
        make_law(
            "F",
            title="Orden de la enseñanza",
            published=date(2020, 6, 30),
            enacted=None,
            rank="1400",
            department="7723",
            scope="1",
        ),
        make_law("B", title="Ley de aguas", published=None, enacted=date(2019, 1, 1)),
        make_law(
            "E",
            title="Ley de leyes",
            published=date(2021, 1, 1),
            enacted=date(2020, 12, 30),
            rank="1300",
            department="7723",
            scope="3",
        ),
        make_law(
            "D",
            title="Resolución de aguas",
            published=date(2019, 5, 5),
            enacted=date(2019, 5, 1),
            rank="1200",
            scope="2",
        ),
    ]


def test_filter_without_constraints_returns_everything_in_input_order(
    catalogue: list[LawSummary],
) -> None:
    result = filter_laws(catalogue)
    assert ids_of(result) == ["C", "A", "F", "B", "E", "D"]
    assert result is not catalogue


def test_filter_accepts_any_iterable_once() -> None:
    laws = laws_on(date(2020, 1, 1), date(2020, 1, 2))
    assert ids_of(filter_laws(iter(laws))) == ["L000", "L001"]
    assert ids_of(filter_laws(law for law in laws)) == ["L000", "L001"]


def test_filter_rank_codes(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, rank_codes={"1300"})) == ["C", "E"]


def test_filter_department_codes(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, department_codes=["7723"])) == ["F", "E"]


def test_filter_scope_codes(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, scope_codes=("2",))) == ["A", "D"]


def test_filter_several_codes_in_one_constraint(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, rank_codes=frozenset({"1200", "1400"}))) == ["A", "F", "D"]


def test_filter_identifiers_keep_input_order_and_ignore_unknown_ids(
    catalogue: list[LawSummary],
) -> None:
    result = filter_laws(catalogue, identifiers=["D", "C", "NOPE"])
    assert ids_of(result) == ["C", "D"]


@pytest.mark.parametrize("field", ["rank_codes", "department_codes", "scope_codes", "identifiers"])
@pytest.mark.parametrize("empty", [[], (), set(), frozenset()])
def test_filter_empty_collection_matches_nothing(
    catalogue: list[LawSummary], field: str, empty: object
) -> None:
    constraint: dict[str, Any] = {field: empty}
    assert filter_laws(catalogue, **constraint) == []


@pytest.mark.parametrize("field", ["rank_codes", "department_codes", "scope_codes", "identifiers"])
def test_filter_none_collection_is_no_constraint(catalogue: list[LawSummary], field: str) -> None:
    constraint: dict[str, Any] = {field: None}
    assert len(filter_laws(catalogue, **constraint)) == 6


@pytest.mark.parametrize(
    ("field", "missing_in"),
    [
        ("rank_codes", {"B"}),
        ("department_codes", {"B", "D"}),
        ("scope_codes", {"B"}),
    ],
)
def test_filter_law_without_the_field_fails_the_constraint(
    catalogue: list[LawSummary], field: str, missing_in: set[str]
) -> None:
    # Every code present in the catalogue is listed, so only laws lacking the field are dropped.
    every_code = {"1200", "1300", "1400", "9564", "7723", "1", "2", "3"}
    constraint: dict[str, Any] = {field: every_code}
    result = filter_laws(catalogue, **constraint)
    assert set(ids_of(result)) == {"A", "B", "C", "D", "E", "F"} - missing_in


def test_filter_constraints_are_combined_with_and(catalogue: list[LawSummary]) -> None:
    result = filter_laws(
        catalogue,
        rank_codes={"1300", "1200"},
        department_codes={"9564"},
        scope_codes={"1", "2"},
        start=date(2019, 1, 1),
        end=date(2020, 12, 31),
        title_words="real",
    )
    assert ids_of(result) == ["A"]


def test_filter_empty_collection_wins_over_other_matching_constraints(
    catalogue: list[LawSummary],
) -> None:
    assert filter_laws(catalogue, rank_codes={"1300"}, scope_codes=[]) == []


# Title matching: (title, query, should_match)
TITLE_CASES = [
    ("Ley de Protección de Datos", "proteccion", True),
    ("Ley de Protección de Datos", "PROTECCIÓN", True),
    ("Ley de Protección de Datos", "protección", True),
    ("Ley de proteccion de datos", "Protección", True),
    ("Ley de Protección de Datos", "datos ley", True),
    ("Ley de Protección de Datos", "ley proteccion datos", True),
    ("Ley de Protección de Datos", "proteccion cultura", False),
    ("Ley de Protección de Datos", "  ley   DATOS ", True),
    ("Texto de las leyes", "ley", False),
    ("Texto de la ley", "leyes", False),
    ("Ley de Educación", "educ", False),
    ("Ley de Educación", "cacion", False),
    ("Ley (Orgánica) de «Educación».", "organica educacion", True),
    ("Ley, de Aguas; texto: refundido", "ley aguas refundido", True),
    ("Ley del pingüino", "pinguino", True),
    ("Ley del pinguino", "pingüino", True),
    ("Ley del Año", "año", True),
    ("Ley del año", "AÑO", True),
    ("Ley del AÑO", "año", True),
    ("Ley del Año", "ano", False),
    ("Ley del ano", "año", False),
    ("Ley del ANO", "ano", True),
    ("Ley de la enseñanza", "ensenanza", False),
    ("Ley de la ensenanza", "enseñanza", False),
    ("Ley de la enseñanza", "ENSEÑANZA", True),
    ("Ley Orgánica 1/2004", "2004", True),
    ("Ley", "", True),
    ("Ley", "   ", True),
]


@pytest.mark.parametrize(("title", "query", "expected"), TITLE_CASES)
def test_filter_title_words(title: str, query: str, expected: bool) -> None:
    result = filter_laws([make_law(title=title)], title_words=query)
    assert (len(result) == 1) is expected


def test_filter_blank_title_words_is_the_same_as_none(catalogue: list[LawSummary]) -> None:
    assert filter_laws(catalogue, title_words="  ") == filter_laws(catalogue, title_words=None)
    assert filter_laws(catalogue, title_words="") == filter_laws(catalogue)


def test_filter_title_words_in_a_catalogue(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, title_words="AGUAS")) == ["B", "D"]
    assert ids_of(filter_laws(catalogue, title_words="ley")) == ["C", "B", "E"]
    assert ids_of(filter_laws(catalogue, title_words="ensenanza")) == []
    assert ids_of(filter_laws(catalogue, title_words="enseñanza")) == ["F"]


def test_filter_start_and_end_are_inclusive(catalogue: list[LawSummary]) -> None:
    result = filter_laws(catalogue, start=date(2019, 5, 5), end=date(2020, 6, 30))
    assert ids_of(result) == ["A", "F", "D"]


def test_filter_start_only(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, start=date(2020, 6, 30))) == ["F", "E"]
    assert ids_of(filter_laws(catalogue, start=date(2020, 7, 1))) == ["E"]


def test_filter_end_only(catalogue: list[LawSummary]) -> None:
    assert ids_of(filter_laws(catalogue, end=date(2019, 5, 5))) == ["C", "D"]
    assert ids_of(filter_laws(catalogue, end=date(2019, 5, 4))) == ["C"]


@pytest.mark.parametrize("bound", ["start", "end"])
def test_filter_undated_law_fails_when_either_bound_is_given(bound: str) -> None:
    undated = make_law("U", published=None, enacted=None)
    far: dict[str, Any] = {"start": date(1, 1, 1), "end": date(9999, 12, 31)}
    assert filter_laws([undated], **{bound: far[bound]}) == []


def test_filter_undated_law_passes_without_bounds() -> None:
    assert ids_of(filter_laws([make_law("U")])) == ["U"]


def test_filter_date_field_selects_which_date_is_used(catalogue: list[LawSummary]) -> None:
    by_published = filter_laws(catalogue, start=date(2019, 1, 1), end=date(2019, 12, 31))
    by_enacted = filter_laws(
        catalogue, start=date(2019, 1, 1), end=date(2019, 12, 31), date_field="enacted_on"
    )
    assert ids_of(by_published) == ["D"]
    assert ids_of(by_enacted) == ["B", "D"]


def test_filter_enacted_date_none_fails_bounds(catalogue: list[LawSummary]) -> None:
    result = filter_laws(catalogue, start=date(2020, 1, 1), date_field="enacted_on")
    assert "F" not in ids_of(result)


def test_filter_published_none_fails_bounds_but_enacted_is_ignored(
    catalogue: list[LawSummary],
) -> None:
    # B has an enacted date of 2019-01-01 but no publication date.
    assert "B" not in ids_of(filter_laws(catalogue, start=date(2000, 1, 1)))


# --- count_over_time ----------------------------------------------------------------------------


def test_count_over_time_counts_per_month_with_zeros() -> None:
    laws = laws_on(date(2020, 1, 31), date(2020, 2, 1), date(2020, 2, 29), date(2020, 4, 1))
    series = count_over_time(laws, start=date(2020, 1, 1), end=date(2020, 4, 30), period="month")
    assert series.period == "month"
    assert keys_of(series) == ("2020-01", "2020-02", "2020-03", "2020-04")
    assert counts_of(series) == (1, 2, 0, 1)
    assert series.total == 4
    assert series.undated == 0
    assert series.outside == 0


def test_count_over_time_points_hold_whole_buckets() -> None:
    series = count_over_time([], start=date(2020, 2, 10), end=date(2020, 3, 5), period="month")
    assert [p.bucket for p in series.points] == [month(2), month(3)]


@pytest.mark.parametrize(
    ("period", "expected_keys"),
    [
        ("year", ("2019", "2020", "2021")),
        ("quarter", ("2019-Q4", "2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4", "2021-Q1")),
    ],
)
def test_count_over_time_other_periods(period: Period, expected_keys: tuple[str, ...]) -> None:
    series = count_over_time([], start=date(2019, 12, 1), end=date(2021, 1, 31), period=period)
    assert keys_of(series) == expected_keys
    assert series.period == period


def test_count_over_time_quarter_assignment() -> None:
    laws = laws_on(date(2020, 3, 31), date(2020, 4, 1), date(2020, 6, 30), date(2020, 12, 31))
    series = count_over_time(laws, start=date(2020, 1, 1), end=date(2020, 12, 31), period="quarter")
    assert counts_of(series) == (1, 2, 0, 1)


def test_count_over_time_range_bounds_are_inclusive_and_the_rest_is_outside() -> None:
    laws = laws_on(
        date(2020, 2, 14),  # day before start
        date(2020, 2, 15),  # start
        date(2020, 4, 10),  # end
        date(2020, 4, 11),  # day after end
    )
    series = count_over_time(laws, start=date(2020, 2, 15), end=date(2020, 4, 10), period="month")
    assert counts_of(series) == (1, 0, 1)
    assert series.total == 2
    assert series.outside == 2


def test_count_over_time_cut_buckets_are_partial() -> None:
    series = count_over_time([], start=date(2020, 2, 15), end=date(2020, 4, 10), period="month")
    assert partials_of(series) == (True, False, True)


def test_count_over_time_only_the_cut_side_is_partial() -> None:
    only_start_cut = count_over_time(
        [], start=date(2020, 2, 15), end=date(2020, 4, 30), period="month"
    )
    only_end_cut = count_over_time(
        [], start=date(2020, 2, 1), end=date(2020, 4, 29), period="month"
    )
    assert partials_of(only_start_cut) == (True, False, False)
    assert partials_of(only_end_cut) == (False, False, True)


def test_count_over_time_aligned_range_has_no_partial_bucket() -> None:
    series = count_over_time([], start=date(2020, 1, 1), end=date(2020, 12, 31), period="quarter")
    assert partials_of(series) == (False, False, False, False)


def test_count_over_time_range_inside_one_bucket_is_one_partial_bucket() -> None:
    series = count_over_time(
        laws_on(date(2020, 3, 10)), start=date(2020, 3, 5), end=date(2020, 3, 20), period="month"
    )
    assert partials_of(series) == (True,)
    assert counts_of(series) == (1,)


def test_count_over_time_year_cut_in_the_middle() -> None:
    series = count_over_time([], start=date(2019, 6, 1), end=date(2021, 12, 31), period="year")
    assert partials_of(series) == (True, False, False)


def test_count_over_time_today_flags_its_bucket_and_every_later_one() -> None:
    series = count_over_time(
        [],
        start=date(2020, 1, 1),
        end=date(2020, 12, 31),
        period="month",
        today=date(2020, 6, 15),
    )
    assert partials_of(series) == (False,) * 5 + (True,) * 7


@pytest.mark.parametrize(
    ("today", "first_partial_index"),
    [
        (date(2020, 5, 31), 4),  # last day of May: May is not over, so it is partial
        (date(2020, 6, 1), 5),  # first day of June
        (date(2020, 6, 30), 5),
        (date(2020, 7, 1), 6),
    ],
)
def test_count_over_time_today_on_period_edges(today: date, first_partial_index: int) -> None:
    series = count_over_time(
        [], start=date(2020, 1, 1), end=date(2020, 12, 31), period="month", today=today
    )
    assert partials_of(series) == (False,) * first_partial_index + (True,) * (
        12 - first_partial_index
    )


def test_count_over_time_today_before_the_range_makes_everything_partial() -> None:
    series = count_over_time(
        [], start=date(2020, 1, 1), end=date(2020, 12, 31), period="quarter", today=date(2019, 7, 1)
    )
    assert partials_of(series) == (True,) * 4


def test_count_over_time_today_after_the_range_changes_nothing() -> None:
    series = count_over_time(
        [], start=date(2020, 1, 1), end=date(2020, 12, 31), period="quarter", today=date(2021, 1, 1)
    )
    assert partials_of(series) == (False,) * 4


def test_count_over_time_today_does_not_change_the_counts() -> None:
    laws = laws_on(date(2020, 1, 5), date(2020, 6, 5), date(2020, 12, 5))
    kwargs: dict[str, Any] = {"start": date(2020, 1, 1), "end": date(2020, 12, 31)}
    plain = count_over_time(laws, period="month", **kwargs)
    flagged = count_over_time(laws, period="month", today=date(2020, 3, 3), **kwargs)
    assert counts_of(plain) == counts_of(flagged)
    assert flagged.total == 3


def test_count_over_time_today_and_cut_range_combine() -> None:
    series = count_over_time(
        [],
        start=date(2020, 1, 15),
        end=date(2020, 5, 31),
        period="month",
        today=date(2020, 3, 10),
    )
    assert partials_of(series) == (True, False, True, True, True)


def test_count_over_time_undated_laws_are_reported_not_counted() -> None:
    laws = laws_on(None, date(2020, 1, 10), None)
    series = count_over_time(laws, start=date(2020, 1, 1), end=date(2020, 1, 31), period="month")
    assert (series.total, series.undated, series.outside) == (1, 2, 0)


def test_count_over_time_date_field_enacted_on() -> None:
    laws = [
        make_law("a", published=date(2020, 1, 10), enacted=date(2019, 12, 20)),
        make_law("b", published=date(2020, 1, 11), enacted=None),
        make_law("c", published=None, enacted=date(2020, 1, 12)),
    ]
    kwargs = {"start": date(2020, 1, 1), "end": date(2020, 1, 31), "period": "month"}
    published = count_over_time(laws, **kwargs)  # type: ignore[arg-type]
    enacted = count_over_time(laws, date_field="enacted_on", **kwargs)  # type: ignore[arg-type]
    assert (published.total, published.undated, published.outside) == (2, 1, 0)
    assert (enacted.total, enacted.undated, enacted.outside) == (1, 1, 1)


def test_count_over_time_accepts_a_generator() -> None:
    series = count_over_time(
        (law for law in laws_on(date(2020, 1, 1), date(2020, 1, 2))),
        start=date(2020, 1, 1),
        end=date(2020, 1, 31),
        period="month",
    )
    assert series.total == 2


def test_count_over_time_empty_input_gives_all_zero_points() -> None:
    series = count_over_time([], start=date(2020, 1, 1), end=date(2020, 3, 31), period="month")
    assert counts_of(series) == (0, 0, 0)
    assert (series.total, series.undated, series.outside) == (0, 0, 0)


def test_count_over_time_start_after_end_raises() -> None:
    with pytest.raises(ValueError):
        count_over_time([], start=date(2020, 2, 1), end=date(2020, 1, 1), period="month")


def test_count_over_time_too_many_buckets_raises() -> None:
    with pytest.raises(ValueError, match="coarser"):
        count_over_time([], start=date(1900, 1, 1), end=date(2000, 1, 1), period="month")


def test_time_series_is_frozen() -> None:
    series = count_over_time([], start=date(2020, 1, 1), end=date(2020, 1, 31), period="month")
    with pytest.raises(dataclasses.FrozenInstanceError):
        series.total = 5  # type: ignore[misc]


# --- count_by -----------------------------------------------------------------------------------


def ranked_laws(spec: dict[str, int], *, text: str | None = None) -> list[LawSummary]:
    """Laws whose rank code is each key, repeated ``value`` times."""
    result: list[LawSummary] = []
    for code, n in spec.items():
        for _ in range(n):
            rank = CodedValue(code=code, text=text or f"Label {code}")
            result.append(make_law(f"{code}-{len(result)}", rank=rank))
    return result


def test_count_by_counts_shares_and_totals() -> None:
    laws = [*ranked_laws({"A": 3, "B": 1}), make_law("none-1"), make_law("none-2")]
    result = count_by(laws, "rank")
    assert isinstance(result, GroupCounts)
    assert result.dimension == "rank"
    assert [(g.code, g.label, g.count) for g in result.groups] == [
        ("A", "Label A", 3),
        ("B", "Label B", 1),
    ]
    assert (result.other_count, result.missing, result.total, result.distinct) == (0, 2, 6, 2)


def test_count_by_share_uses_laws_with_a_value_as_denominator() -> None:
    laws = [*ranked_laws({"A": 2, "B": 1}), make_law("none")]
    groups = count_by(laws, "rank").groups
    assert groups[0].share == pytest.approx(2 / 3)
    assert groups[1].share == pytest.approx(1 / 3)


def test_count_by_share_is_unrounded() -> None:
    groups = count_by(ranked_laws({"A": 1, "B": 2}), "rank").groups
    assert groups[1].share == pytest.approx(1 / 3, rel=1e-12)
    assert groups[0].share == pytest.approx(2 / 3, rel=1e-12)


def test_count_by_shares_of_all_groups_add_up_to_one() -> None:
    groups = count_by([*ranked_laws({"A": 5, "B": 3, "C": 1}), make_law("x")], "rank").groups
    assert sum(g.share for g in groups) == pytest.approx(1.0)


def test_count_by_group_count_is_a_frozen_dataclass() -> None:
    group = count_by(ranked_laws({"A": 1}), "rank").groups[0]
    assert isinstance(group, GroupCount)
    with pytest.raises(dataclasses.FrozenInstanceError):
        group.count = 9  # type: ignore[misc]


def test_count_by_every_law_without_a_value() -> None:
    result = count_by([make_law("a"), make_law("b")], "rank")
    assert result.groups == ()
    assert (result.other_count, result.missing, result.total, result.distinct) == (0, 2, 2, 0)


def test_count_by_empty_input() -> None:
    result = count_by([], "scope")
    assert result.groups == ()
    assert (result.other_count, result.missing, result.total, result.distinct) == (0, 0, 0, 0)
    assert result.dimension == "scope"


@pytest.mark.parametrize("dimension", ["rank", "department", "scope"])
def test_count_by_reads_the_requested_dimension(dimension: Dimension) -> None:
    laws = [
        make_law("1", rank="r1", department="d1", scope="s1"),
        make_law("2", rank="r1", department="d2", scope="s1"),
        make_law("3", rank="r2", department="d2", scope="s2"),
        make_law("4", rank="r1", department="d2", scope="s1"),
    ]
    result = count_by(laws, dimension)
    assert result.dimension == dimension
    expected = {
        "rank": [("r1", 3), ("r2", 1)],
        "department": [("d2", 3), ("d1", 1)],
        "scope": [("s1", 3), ("s2", 1)],
    }[dimension]
    assert [(g.code, g.count) for g in result.groups] == expected


def test_count_by_sorts_by_count_descending() -> None:
    result = count_by(ranked_laws({"A": 1, "B": 4, "C": 2}), "rank")
    assert [g.code for g in result.groups] == ["B", "C", "A"]


def test_count_by_ties_are_ordered_by_label_then_code() -> None:
    laws = [
        make_law("1", rank=CodedValue(code="9", text="Alpha")),
        make_law("2", rank=CodedValue(code="1", text="Beta")),
        make_law("3", rank=CodedValue(code="5", text="Alpha")),
    ]
    result = count_by(laws, "rank")
    assert [(g.label, g.code) for g in result.groups] == [
        ("Alpha", "5"),
        ("Alpha", "9"),
        ("Beta", "1"),
    ]


def test_count_by_tie_order_does_not_depend_on_input_order() -> None:
    laws = [
        make_law("1", rank=CodedValue(code="9", text="Alpha")),
        make_law("2", rank=CodedValue(code="1", text="Beta")),
        make_law("3", rank=CodedValue(code="5", text="Alpha")),
    ]
    forward = count_by(laws, "rank")
    backward = count_by(list(reversed(laws)), "rank")
    assert forward == backward


def test_count_by_top_limits_groups_and_reports_the_rest() -> None:
    result = count_by(
        [*ranked_laws({"A": 5, "B": 4, "C": 2, "D": 1}), make_law("n")], "rank", top=2
    )
    assert [g.code for g in result.groups] == ["A", "B"]
    assert result.other_count == 3
    assert result.distinct == 4
    assert result.missing == 1
    assert result.total == 13
    # shares stay relative to every counted law, not just the groups shown
    assert result.groups[0].share == pytest.approx(5 / 12)


def test_count_by_top_of_one() -> None:
    result = count_by(ranked_laws({"A": 2, "B": 2, "C": 1}), "rank", top=1)
    assert [g.code for g in result.groups] == ["A"]
    assert result.other_count == 3


def test_count_by_top_larger_than_the_number_of_groups() -> None:
    result = count_by(ranked_laws({"A": 2, "B": 1}), "rank", top=50)
    assert len(result.groups) == 2
    assert result.other_count == 0


def test_count_by_default_top_is_ten() -> None:
    spec = {f"{i:02d}": 20 - i for i in range(13)}
    result = count_by(ranked_laws(spec), "rank")
    assert len(result.groups) == 10
    assert result.distinct == 13
    assert result.other_count == sum(20 - i for i in (10, 11, 12))


@pytest.mark.parametrize("top", [0, -1, -10])
def test_count_by_top_below_one_raises(top: int) -> None:
    with pytest.raises(ValueError):
        count_by(ranked_laws({"A": 1}), "rank", top=top)


def test_count_by_groups_by_code_not_by_text() -> None:
    laws = [
        make_law("1", department=CodedValue(code="D1", text="Ministerio de Hacienda")),
        make_law("2", department=CodedValue(code="D1", text="Ministerio de Economía y Hacienda")),
        make_law("3", department=CodedValue(code="D2", text="Ministerio de Hacienda")),
    ]
    result = count_by(laws, "department")
    assert [(g.code, g.count) for g in result.groups] == [("D1", 2), ("D2", 1)]
    assert result.distinct == 2


def test_count_by_label_is_the_most_frequent_text_for_the_code() -> None:
    texts = ["Old name", "New name", "New name", "Old name", "New name"]
    laws = [make_law(str(i), rank=CodedValue(code="X", text=t)) for i, t in enumerate(texts)]
    assert count_by(laws, "rank").groups[0].label == "New name"


def test_count_by_label_ties_go_to_the_text_seen_first() -> None:
    texts = ["Zeta", "Alpha", "Alpha", "Zeta"]
    laws = [make_law(str(i), rank=CodedValue(code="X", text=t)) for i, t in enumerate(texts)]
    assert count_by(laws, "rank").groups[0].label == "Zeta"
    reversed_laws = [
        make_law(str(i), rank=CodedValue(code="X", text=t)) for i, t in enumerate(texts[::-1])
    ]
    assert count_by(reversed_laws, "rank").groups[0].label == "Zeta"  # still the first one seen
    first_alpha = [
        make_law(str(i), rank=CodedValue(code="X", text=t))
        for i, t in enumerate(["Alpha", "Zeta", "Zeta", "Alpha"])
    ]
    assert count_by(first_alpha, "rank").groups[0].label == "Alpha"


def test_count_by_label_decides_the_tie_order_after_the_label_choice() -> None:
    # Code "2" is labelled "Aaa" (most frequent), code "1" is labelled "Bbb": the label, not the
    # code, orders the tie.
    laws = [
        make_law("1", rank=CodedValue(code="2", text="Zzz")),
        make_law("2", rank=CodedValue(code="2", text="Aaa")),
        make_law("3", rank=CodedValue(code="2", text="Aaa")),
        make_law("4", rank=CodedValue(code="1", text="Bbb")),
        make_law("5", rank=CodedValue(code="1", text="Bbb")),
        make_law("6", rank=CodedValue(code="1", text="Bbb")),
        make_law("7", rank=CodedValue(code="1", text="Bbb")),
    ]
    result = count_by(laws, "rank")
    assert [(g.code, g.label, g.count) for g in result.groups] == [
        ("1", "Bbb", 4),
        ("2", "Aaa", 3),
    ]


def test_count_by_counts_every_input_law_whatever_its_dates() -> None:
    laws = [make_law("a", rank="A"), make_law("b", rank="A", published=date(1999, 1, 1))]
    assert count_by(laws, "rank").total == 2


def test_count_by_accepts_a_generator() -> None:
    result = count_by((law for law in ranked_laws({"A": 2, "B": 1})), "rank")
    assert result.total == 3
    assert [g.code for g in result.groups] == ["A", "B"]


@pytest.mark.parametrize("top", [1, 2, 3, 10])
def test_count_by_accounting_identity(top: int) -> None:
    laws = [*ranked_laws({"A": 7, "B": 5, "C": 5, "D": 2, "E": 1}), make_law("n1"), make_law("n2")]
    result = count_by(laws, "rank", top=top)
    assert sum(g.count for g in result.groups) + result.other_count + result.missing == result.total
    assert result.total == len(laws)


# --- series_by ----------------------------------------------------------------------------------


def dated(identifier: str, day: date, rank: Coded = None, **extra: Coded) -> LawSummary:
    return make_law(
        identifier,
        published=day,
        rank=rank,
        department=extra.get("department"),
        scope=extra.get("scope"),
    )


Q1 = {"start": date(2020, 1, 1), "end": date(2020, 3, 31), "period": "month"}


def test_series_by_one_series_per_group_in_rank_order() -> None:
    laws = [
        dated("1", date(2020, 1, 5), "A"),
        dated("2", date(2020, 2, 5), "B"),
        dated("3", date(2020, 2, 6), "B"),
        dated("4", date(2020, 3, 5), "B"),
        dated("5", date(2020, 3, 6), "A"),
    ]
    rows = series_by(laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month")
    assert [r.code for r in rows] == ["B", "A"]
    assert [r.label for r in rows] == ["Label B", "Label A"]
    assert counts_of(rows[0].series) == (0, 2, 1)
    assert counts_of(rows[1].series) == (1, 0, 1)
    assert all(isinstance(r, GroupSeries) for r in rows)
    assert all(r.series.period == "month" for r in rows)


def test_series_by_top_groups_are_chosen_inside_the_range() -> None:
    laws = [
        # A is the biggest overall, but only one of its laws falls inside the range
        *(dated(f"a{i}", date(2019, 1, 1 + i), "A") for i in range(3)),
        dated("a-in", date(2020, 1, 10), "A"),
        dated("b1", date(2020, 1, 11), "B"),
        dated("b2", date(2020, 2, 11), "B"),
    ]
    rows = series_by(
        laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month", top=1
    )
    assert [r.code for r in rows] == ["B", None]
    assert rows[1].code is None
    assert rows[1].label == "other"
    assert counts_of(rows[1].series) == (1, 0, 0)


def test_series_by_other_row_collects_remaining_groups_and_laws_without_a_value() -> None:
    laws = [
        dated("1", date(2020, 1, 1), "A"),
        dated("2", date(2020, 1, 2), "A"),
        dated("3", date(2020, 2, 1), "B"),
        dated("4", date(2020, 3, 1), "C"),
        dated("5", date(2020, 3, 2), None),
        dated("6", date(2020, 3, 3), None),
    ]
    rows = series_by(
        laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month", top=1
    )
    assert [r.code for r in rows] == ["A", None]
    assert rows[1].label == "other"
    assert counts_of(rows[1].series) == (0, 1, 3)
    assert rows[1].series.total == 4


def test_series_by_other_row_absent_when_nothing_would_go_in_it() -> None:
    laws = [dated("1", date(2020, 1, 1), "A"), dated("2", date(2020, 2, 1), "B")]
    rows = series_by(laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month")
    assert [r.code for r in rows] == ["A", "B"]


def test_series_by_other_row_ignores_laws_outside_the_range_and_undated_ones() -> None:
    laws = [
        dated("1", date(2020, 1, 1), "A"),
        dated("2", date(2019, 1, 1), None),  # no value, but outside the range
        dated("3", date(2021, 1, 1), "C"),  # beyond top, but outside the range
        make_law("4", published=None, rank=None),  # no value and undated
        make_law("5", published=None, rank="C"),  # undated
    ]
    rows = series_by(
        laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month", top=1
    )
    assert [r.code for r in rows] == ["A"]


def test_series_by_other_row_present_for_a_single_law_without_a_value() -> None:
    laws = [dated("1", date(2020, 1, 1), "A"), dated("2", date(2020, 2, 1), None)]
    rows = series_by(laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month")
    assert [r.code for r in rows] == ["A", None]
    assert counts_of(rows[1].series) == (0, 1, 0)


def test_series_by_no_laws_in_range_gives_no_rows() -> None:
    assert series_by([], "rank", **Q1) == ()  # type: ignore[arg-type]
    laws = [dated("1", date(2019, 1, 1), "A"), make_law("2", rank="A")]
    assert series_by(laws, "rank", **Q1) == ()  # type: ignore[arg-type]


def test_series_by_rows_share_the_same_buckets_and_partial_flags() -> None:
    laws = [
        dated("1", date(2020, 1, 20), "A"),
        dated("2", date(2020, 3, 5), "B"),
        dated("3", date(2020, 3, 6), None),
    ]
    rows = series_by(
        laws,
        "rank",
        start=date(2020, 1, 15),
        end=date(2020, 4, 20),
        period="month",
        top=1,
        today=date(2020, 3, 15),
    )
    assert len(rows) == 2
    reference = count_over_time(
        laws,
        start=date(2020, 1, 15),
        end=date(2020, 4, 20),
        period="month",
        today=date(2020, 3, 15),
    )
    for row in rows:
        assert [p.bucket for p in row.series.points] == [p.bucket for p in reference.points]
        assert partials_of(row.series) == partials_of(reference) == (True, False, True, True)


def test_series_by_per_bucket_sum_equals_count_over_time() -> None:
    laws = [
        dated("1", date(2020, 1, 1), "A"),
        dated("2", date(2020, 1, 2), "B"),
        dated("3", date(2020, 2, 2), "C"),
        dated("4", date(2020, 2, 3), "A"),
        dated("5", date(2020, 3, 4), None),
        dated("6", date(2019, 3, 4), "A"),
        make_law("7", rank="A"),
    ]
    rows = series_by(
        laws, "rank", start=date(2020, 1, 1), end=date(2020, 3, 31), period="month", top=2
    )
    overall = count_over_time(laws, start=date(2020, 1, 1), end=date(2020, 3, 31), period="month")
    summed = tuple(sum(col) for col in zip(*(counts_of(r.series) for r in rows), strict=True))
    assert summed == counts_of(overall) == (2, 2, 1)


def test_series_by_ties_for_the_last_top_place_use_the_count_by_order() -> None:
    # AMBIGUOUS RULE (series_by): the tie rule for choosing the top groups is not stated.
    # These tests assume the same order as ``count_by``: count descending, then label, then code.
    laws = [
        dated("1", date(2020, 1, 1), CodedValue(code="9", text="Alpha")),
        dated("2", date(2020, 1, 2), CodedValue(code="1", text="Beta")),
        dated("3", date(2020, 1, 3), CodedValue(code="5", text="Alpha")),
    ]
    rows = series_by(
        laws, "rank", start=date(2020, 1, 1), end=date(2020, 1, 31), period="month", top=2
    )
    assert [r.code for r in rows] == ["5", "9", None]


def test_series_by_top_default_is_five() -> None:
    laws = [dated(f"{i}", date(2020, 1, 1 + i), f"R{i}") for i in range(7)]
    rows = series_by(laws, "rank", start=date(2020, 1, 1), end=date(2020, 1, 31), period="month")
    assert len(rows) == 6
    assert rows[-1].code is None
    assert rows[-1].series.total == 2


@pytest.mark.parametrize("dimension", ["rank", "department", "scope"])
def test_series_by_reads_the_requested_dimension(dimension: Dimension) -> None:
    laws = [
        dated("1", date(2020, 1, 1), "r1", department="d1", scope="s1"),
        dated("2", date(2020, 1, 2), "r2", department="d1", scope="s2"),
        dated("3", date(2020, 1, 3), "r2", department="d2", scope="s2"),
    ]
    rows = series_by(
        laws, dimension, start=date(2020, 1, 1), end=date(2020, 1, 31), period="month", top=1
    )
    expected = {"rank": "r2", "department": "d1", "scope": "s2"}[dimension]
    assert rows[0].code == expected
    assert rows[0].series.total == 2


def test_series_by_date_field_enacted_on() -> None:
    laws = [
        make_law("1", published=date(2020, 1, 1), enacted=date(2019, 12, 1), rank="A"),
        make_law("2", published=date(2020, 1, 1), enacted=date(2020, 1, 2), rank="B"),
        make_law("3", published=date(2020, 1, 1), enacted=date(2020, 1, 3), rank="B"),
    ]
    rows = series_by(
        laws,
        "rank",
        start=date(2020, 1, 1),
        end=date(2020, 1, 31),
        period="month",
        date_field="enacted_on",
    )
    assert [(r.code, r.series.total) for r in rows] == [("B", 2)]


def test_series_by_today_flags_the_buckets() -> None:
    laws = [dated("1", date(2020, 1, 1), "A")]
    rows = series_by(laws, "rank", today=date(2020, 2, 10), **Q1)  # type: ignore[arg-type]
    assert partials_of(rows[0].series) == (False, True, True)


def test_series_by_accepts_a_generator() -> None:
    laws = (dated(f"{i}", date(2020, 1, 1 + i), "A" if i % 2 else "B") for i in range(5))
    rows = series_by(laws, "rank", start=date(2020, 1, 1), end=date(2020, 1, 31), period="month")
    assert [(r.code, r.series.total) for r in rows] == [("B", 3), ("A", 2)]


# --- find_spikes --------------------------------------------------------------------------------


def test_find_spikes_flags_a_bucket_far_above_the_rest() -> None:
    spikes = find_spikes(make_series([1, 1, 1, 20]))
    assert len(spikes) == 1
    spike = spikes[0]
    assert isinstance(spike, Spike)
    assert spike.bucket == month(4, 2000)
    assert spike.count == 20
    assert spike.baseline == pytest.approx(1.0)
    assert spike.ratio == pytest.approx(20.0)
    assert spike.excess == pytest.approx(19.0)


def test_find_spikes_flat_series_has_none() -> None:
    assert find_spikes(make_series([5, 5, 5, 5, 5, 5])) == ()


def test_find_spikes_baseline_is_leave_one_out() -> None:
    # With the candidate included the median would be 7 (ratio 1.7 < 3); without it, it is 2.
    spikes = find_spikes(make_series([2, 2, 12, 12]), min_ratio=3.0)
    assert [s.bucket.key for s in spikes] == ["2000-03", "2000-04"]
    assert all(s.baseline == pytest.approx(2.0) for s in spikes)
    assert all(s.ratio == pytest.approx(6.0) for s in spikes)


def test_find_spikes_baseline_is_a_median_not_a_mean() -> None:
    # Others of the first bucket are [2, 2, 40]: median 2, mean 14.7.
    spikes = find_spikes(make_series([10, 2, 2, 40]))
    assert [s.bucket.key for s in spikes] == ["2000-04", "2000-01"]
    assert spikes[1].baseline == pytest.approx(2.0)
    assert spikes[1].ratio == pytest.approx(5.0)
    assert spikes[1].excess == pytest.approx(8.0)


def test_find_spikes_median_of_an_even_number_of_others_is_the_middle_average() -> None:
    spikes = find_spikes(make_series([10, 1, 3, 5, 9]))
    by_key = {s.bucket.key: s for s in spikes}
    assert by_key["2000-01"].baseline == pytest.approx(4.0)  # others 1 3 5 9
    assert by_key["2000-01"].excess == pytest.approx(6.0)
    assert by_key["2000-05"].baseline == pytest.approx(4.0)  # others 1 3 5 10
    assert "2000-04" not in by_key  # others 1 3 9 10 -> 6.0, count 5 is below it


def test_find_spikes_fewer_than_four_complete_buckets_means_no_spikes() -> None:
    assert find_spikes(make_series([0, 0, 50])) == ()
    assert find_spikes(make_series([])) == ()


def test_find_spikes_exactly_four_complete_buckets_are_enough() -> None:
    assert len(find_spikes(make_series([0, 0, 0, 9]))) == 1


def test_find_spikes_partial_buckets_do_not_count_towards_the_four() -> None:
    assert find_spikes(make_series([0, 0, 0, 9, 9], partial=[3, 4])) == ()
    assert find_spikes(make_series([0, 0, 0, 9, 0], partial=[4])) != ()


def test_find_spikes_partial_buckets_are_never_candidates() -> None:
    series = make_series([1, 1, 1, 1, 1000], partial=[4])
    assert find_spikes(series) == ()


def test_find_spikes_partial_buckets_are_not_in_the_baseline() -> None:
    # If the partial 50s were in the baseline, the median for the 12 would be 25.5.
    spikes = find_spikes(make_series([1, 1, 1, 12, 50, 50, 50], partial=[4, 5, 6]))
    assert [(s.bucket.key, s.baseline) for s in spikes] == [("2000-04", pytest.approx(1.0))]


def test_find_spikes_a_partial_bucket_in_the_middle_is_skipped_cleanly() -> None:
    spikes = find_spikes(make_series([3, 3, 100, 3, 25], partial=[2]))
    assert [s.bucket.key for s in spikes] == ["2000-05"]
    assert spikes[0].baseline == pytest.approx(3.0)


def test_find_spikes_zero_baseline_ignores_min_ratio_and_has_no_ratio() -> None:
    spikes = find_spikes(make_series([0, 0, 0, 6]), min_ratio=1000.0)
    assert len(spikes) == 1
    assert spikes[0].baseline == 0.0
    assert spikes[0].ratio is None
    assert spikes[0].excess == pytest.approx(6.0)


def test_find_spikes_zero_baseline_still_needs_the_minimum_excess() -> None:
    assert find_spikes(make_series([0, 0, 0, 4]), min_excess=5) == ()
    assert len(find_spikes(make_series([0, 0, 0, 5]), min_excess=5)) == 1


def test_find_spikes_min_excess_is_inclusive() -> None:
    # Baseline 5, count 10: excess exactly 5, ratio 2.
    assert len(find_spikes(make_series([5, 5, 5, 10]), min_excess=5)) == 1
    assert find_spikes(make_series([5, 5, 5, 10]), min_excess=6) == ()


def test_find_spikes_min_ratio_is_inclusive() -> None:
    series = make_series([4, 4, 4, 6])  # ratio exactly 1.5, excess 2
    assert len(find_spikes(series, min_ratio=1.5, min_excess=1)) == 1
    assert find_spikes(series, min_ratio=1.51, min_excess=1) == ()


def test_find_spikes_both_conditions_are_required() -> None:
    # Big excess but a small ratio: baseline 100, count 110.
    assert find_spikes(make_series([100, 100, 100, 110]), min_excess=5) == ()
    # Big ratio but a small excess: baseline 1, count 4.
    assert find_spikes(make_series([1, 1, 1, 4]), min_excess=5) == ()


def test_find_spikes_sorted_by_excess_then_bucket_key() -> None:
    series = make_series([0, 0, 0, 0, 0, 8, 0, 20, 0, 8])
    assert [(s.bucket.key, s.count) for s in find_spikes(series)] == [
        ("2000-08", 20),
        ("2000-06", 8),
        ("2000-10", 8),
    ]


def test_find_spikes_limit() -> None:
    series = make_series([0, 0, 0, 0, 0, 8, 0, 20, 0, 9])
    assert [s.count for s in find_spikes(series, limit=2)] == [20, 9]
    assert len(find_spikes(series, limit=10)) == 3


def test_find_spikes_default_limit_is_five() -> None:
    series = make_series([0] * 6 + [10, 11, 12, 13, 14, 15, 16])
    assert [s.count for s in find_spikes(series)] == [16, 15, 14, 13, 12]


def test_find_spikes_ignores_total_undated_and_outside_fields() -> None:
    series = dataclasses.replace(make_series([0, 0, 0, 9]), total=999, undated=5, outside=7)
    assert len(find_spikes(series)) == 1


# --- explain_bucket -----------------------------------------------------------------------------


def scenario() -> list[LawSummary]:
    """June 2020 is the target; January to May is the baseline.

    Rank A: 4 laws in June, 1 per baseline month (expected 1.0).
    Rank B: 2 laws in June, none in the baseline (expected 0.0).
    Rank C: 1 law in June and 5 in the baseline (expected 1.0, so no excess).
    """
    laws: list[LawSummary] = []
    for i in range(4):
        laws.append(dated(f"jun-a{i}", date(2020, 6, 10 + i), "A", department="D1", scope="S1"))
    for i in range(2):
        laws.append(dated(f"jun-b{i}", date(2020, 6, 20 + i), "B", department="D2", scope="S1"))
    laws.append(dated("jun-c", date(2020, 6, 30), "C", department="D2", scope="S2"))
    for m in range(1, 6):
        laws.append(dated(f"base-a{m}", date(2020, m, 15), "A", department="D1", scope="S1"))
    for i in range(5):
        laws.append(dated(f"base-c{i}", date(2020, 1 + i, 3), "C", department="D2", scope="S2"))
    return laws


BASELINE = [month(m) for m in range(1, 6)]


def rank_only(laws: Iterable[LawSummary], **kwargs: object) -> BucketExplanation:
    return explain_bucket(
        laws,
        target=month(6),
        baseline=BASELINE,
        dimensions=("rank",),
        **kwargs,  # type: ignore[arg-type]
    )


def test_explain_bucket_headline_numbers() -> None:
    result = explain_bucket(scenario(), target=month(6), baseline=BASELINE)
    assert isinstance(result, BucketExplanation)
    assert result.bucket == month(6)
    assert result.count == 7
    assert result.baseline_buckets == 5
    assert result.baseline == pytest.approx(10 / 5)


def test_explain_bucket_contributions_for_one_dimension() -> None:
    result = rank_only(scenario())
    assert [(c.code, c.count) for c in result.contributions] == [("A", 4), ("B", 2)]
    first, second = result.contributions
    assert isinstance(first, Contribution)
    assert (first.dimension, first.label) == ("rank", "Label A")
    assert first.expected == pytest.approx(1.0)
    assert first.excess == pytest.approx(3.0)
    assert second.expected == pytest.approx(0.0)
    assert second.excess == pytest.approx(2.0)


def test_explain_bucket_group_with_no_growth_is_left_out() -> None:
    # C has one law in June and 1.0 expected, so its excess is exactly zero.
    codes = [c.code for c in rank_only(scenario()).contributions]
    assert "C" not in codes


def test_explain_bucket_group_that_shrank_is_left_out() -> None:
    laws = [dated("t", date(2020, 6, 1), "A")] + [
        dated(f"b{i}", date(2020, 1 + i % 5, 1 + i), "A") for i in range(10)
    ]
    assert rank_only(laws).contributions == ()


def test_explain_bucket_group_absent_from_the_baseline_expects_zero() -> None:
    laws = [dated("t", date(2020, 6, 1), "NEW")]
    (contribution,) = rank_only(laws).contributions
    assert contribution.expected == 0.0
    assert contribution.excess == pytest.approx(1.0)


def test_explain_bucket_expected_is_the_mean_over_the_baseline_buckets() -> None:
    laws = [
        dated("t", date(2020, 6, 1), "A"),
        dated("b0", date(2020, 1, 1), "A"),
        dated("b1", date(2020, 3, 1), "A"),
    ]
    (contribution,) = rank_only(laws).contributions
    assert contribution.expected == pytest.approx(2 / 5)
    assert contribution.excess == pytest.approx(1 - 2 / 5)


def test_explain_bucket_target_inside_the_baseline_is_ignored() -> None:
    with_target = explain_bucket(scenario(), target=month(6), baseline=[*BASELINE, month(6)])
    without = explain_bucket(scenario(), target=month(6), baseline=BASELINE)
    assert with_target == without
    assert with_target.baseline_buckets == 5


def test_explain_bucket_empty_baseline_raises() -> None:
    with pytest.raises(ValueError):
        explain_bucket(scenario(), target=month(6), baseline=[])


def test_explain_bucket_baseline_made_only_of_the_target_raises() -> None:
    with pytest.raises(ValueError):
        explain_bucket(scenario(), target=month(6), baseline=[month(6)])


def test_explain_bucket_baseline_mean_counts_only_laws_in_baseline_buckets() -> None:
    laws = [
        dated("t", date(2020, 6, 1), "A"),
        dated("in", date(2020, 2, 1), "A"),
        dated("before", date(2019, 12, 31), "A"),
        dated("between", date(2020, 7, 1), "A"),
        make_law("undated", published=None, rank="A"),
    ]
    result = explain_bucket(laws, target=month(6), baseline=[month(1), month(2)])
    assert result.baseline == pytest.approx(1 / 2)
    assert result.baseline_buckets == 2
    assert result.contributions[0].expected == pytest.approx(1 / 2)


def test_explain_bucket_non_contiguous_baseline() -> None:
    laws = [dated("t", date(2020, 6, 1), "A"), dated("x", date(2020, 3, 1), "A")]
    result = explain_bucket(laws, target=month(6), baseline=[month(1), month(3), month(9)])
    assert result.baseline_buckets == 3
    assert result.baseline == pytest.approx(1 / 3)


def test_explain_bucket_laws_are_the_target_laws_by_date_then_identifier() -> None:
    laws = [
        dated("z", date(2020, 6, 1), "A"),
        dated("b", date(2020, 6, 5), "A"),
        dated("a", date(2020, 6, 5), "A"),
        dated("first", date(2020, 6, 30), "A"),
        dated("base", date(2020, 2, 1), "A"),
        dated("next", date(2020, 7, 1), "A"),
        make_law("undated", rank="A"),
    ]
    result = explain_bucket(laws, target=month(6), baseline=BASELINE)
    assert ids_of(result.laws) == ["z", "a", "b", "first"]
    assert result.count == 4
    assert isinstance(result.laws, tuple)


def test_explain_bucket_target_bucket_edges_are_inclusive() -> None:
    laws = [
        dated("lo", date(2020, 6, 1), "A"),
        dated("hi", date(2020, 6, 30), "A"),
        dated("out-lo", date(2020, 5, 31), "A"),
        dated("out-hi", date(2020, 7, 1), "A"),
    ]
    assert ids_of(explain_bucket(laws, target=month(6), baseline=[month(1)]).laws) == ["lo", "hi"]


def test_explain_bucket_works_for_other_periods() -> None:
    quarter = bucket_for(date(2020, 5, 1), "quarter")
    laws = [
        dated("a", date(2020, 4, 1), "A"),
        dated("b", date(2020, 6, 30), "A"),
        dated("c", date(2019, 10, 1), "A"),
    ]
    result = explain_bucket(
        laws, target=quarter, baseline=[bucket_for(date(2019, 11, 1), "quarter")]
    )
    assert result.count == 2
    assert result.baseline == pytest.approx(1.0)
    assert result.contributions[0].expected == pytest.approx(1.0)
    assert result.contributions[0].excess == pytest.approx(1.0)


def test_explain_bucket_laws_without_a_value_count_but_have_no_contribution() -> None:
    laws = [dated("t1", date(2020, 6, 1), None), dated("t2", date(2020, 6, 2), None)]
    result = rank_only(laws)
    assert result.count == 2
    assert result.contributions == ()
    assert ids_of(result.laws) == ["t1", "t2"]


def test_explain_bucket_default_dimensions_are_rank_department_scope() -> None:
    laws = [
        dated("t1", date(2020, 6, 1), "R1", department="D1", scope="S1"),
        dated("t2", date(2020, 6, 2), "R1", department="D1", scope="S1"),
        dated("t3", date(2020, 6, 3), "R1", department="D1", scope="S1"),
    ]
    result = explain_bucket(laws, target=month(6), baseline=BASELINE)
    assert [c.dimension for c in result.contributions] == ["rank", "department", "scope"]


def test_explain_bucket_dimensions_restrict_what_is_reported() -> None:
    result = explain_bucket(
        scenario(), target=month(6), baseline=BASELINE, dimensions=("department",)
    )
    assert {c.dimension for c in result.contributions} == {"department"}
    assert [(c.code, c.count) for c in result.contributions] == [("D1", 4), ("D2", 3)]
    assert result.contributions[0].expected == pytest.approx(1.0)
    assert result.contributions[1].expected == pytest.approx(1.0)


def test_explain_bucket_contributions_are_ordered_by_excess_across_dimensions() -> None:
    # rank R: 3 laws (excess 3), department D: 5 laws (excess 5), scope S: 4 laws (excess 4).
    laws = [dated(f"a{i}", date(2020, 6, 1 + i), "R", department="D", scope="S") for i in range(3)]
    laws += [dated("b1", date(2020, 6, 10), None, department="D", scope="S")]
    laws += [dated("b2", date(2020, 6, 11), None, department="D")]
    result = explain_bucket(laws, target=month(6), baseline=BASELINE)
    assert [(c.dimension, c.excess) for c in result.contributions] == [
        ("department", pytest.approx(5.0)),
        ("scope", pytest.approx(4.0)),
        ("rank", pytest.approx(3.0)),
    ]


def tied_laws() -> list[LawSummary]:
    """Three laws in June, each in a rank, a department and a scope with equal excess."""
    return [
        dated(
            f"t{i}",
            date(2020, 6, 1 + i),
            CodedValue(code="R", text="Zzz rank"),
            department=CodedValue(code="D", text="Mmm department"),
            scope=CodedValue(code="S", text="Aaa scope"),
        )
        for i in range(3)
    ]


def test_explain_bucket_equal_excess_follows_the_dimension_order_as_given() -> None:
    default = explain_bucket(tied_laws(), target=month(6), baseline=BASELINE)
    reordered = explain_bucket(
        tied_laws(),
        target=month(6),
        baseline=BASELINE,
        dimensions=("scope", "department", "rank"),
    )
    assert [c.dimension for c in default.contributions] == ["rank", "department", "scope"]
    assert [c.dimension for c in reordered.contributions] == ["scope", "department", "rank"]


def test_explain_bucket_equal_excess_in_one_dimension_goes_by_label_then_code() -> None:
    laws = [
        dated("1", date(2020, 6, 1), CodedValue(code="9", text="Alpha")),
        dated("2", date(2020, 6, 2), CodedValue(code="1", text="Beta")),
        dated("3", date(2020, 6, 3), CodedValue(code="5", text="Alpha")),
    ]
    result = rank_only(laws)
    assert [(c.label, c.code) for c in result.contributions] == [
        ("Alpha", "5"),
        ("Alpha", "9"),
        ("Beta", "1"),
    ]


def test_explain_bucket_top_applies_per_dimension() -> None:
    laws = [
        dated("1", date(2020, 6, 1), "R1", department="D1"),
        dated("2", date(2020, 6, 2), "R1", department="D1"),
        dated("3", date(2020, 6, 3), "R2", department="D2"),
        dated("4", date(2020, 6, 4), "R3", department="D3"),
    ]
    result = explain_bucket(
        laws, target=month(6), baseline=BASELINE, dimensions=("rank", "department"), top=2
    )
    assert [(c.dimension, c.code) for c in result.contributions] == [
        ("rank", "R1"),
        ("department", "D1"),
        ("rank", "R2"),
        ("department", "D2"),
    ]


def test_explain_bucket_top_keeps_the_largest_excess_not_the_largest_count() -> None:
    # R1 has 3 laws but 3 of them are expected (3 baseline laws over 5 months is 0.6): excess 2.4.
    # R2 has 2 laws and no baseline: excess 2.0. R3 has 1 law: excess 1.0.
    laws = [dated(f"t{i}", date(2020, 6, 1 + i), "R1") for i in range(3)]
    laws += [dated(f"u{i}", date(2020, 6, 10 + i), "R2") for i in range(2)]
    laws += [dated("v", date(2020, 6, 20), "R3")]
    laws += [dated(f"b{i}", date(2020, 1 + i, 1), "R1") for i in range(3)]
    result = rank_only(laws, top=2)
    assert [c.code for c in result.contributions] == ["R1", "R2"]
    assert result.contributions[0].excess == pytest.approx(3 - 3 / 5)


def test_explain_bucket_top_default_is_five_per_dimension() -> None:
    laws = [dated(f"t{i}", date(2020, 6, 1 + i), f"R{i}") for i in range(7)]
    assert len(rank_only(laws).contributions) == 5


def test_explain_bucket_date_field_enacted_on() -> None:
    laws = [
        make_law("a", published=date(2020, 1, 1), enacted=date(2020, 6, 5), rank="A"),
        make_law("b", published=date(2020, 6, 5), enacted=date(2020, 1, 5), rank="A"),
        make_law("c", published=date(2020, 6, 6), enacted=None, rank="A"),
    ]
    result = explain_bucket(
        laws,
        target=month(6),
        baseline=BASELINE,
        dimensions=("rank",),
        date_field="enacted_on",
    )
    assert ids_of(result.laws) == ["a"]
    assert result.baseline == pytest.approx(1 / 5)
    assert result.contributions[0].expected == pytest.approx(1 / 5)


def test_explain_bucket_groups_by_code_and_labels_by_most_frequent_text() -> None:
    laws = [
        dated("1", date(2020, 6, 1), CodedValue(code="X", text="Old")),
        dated("2", date(2020, 6, 2), CodedValue(code="X", text="New")),
        dated("3", date(2020, 6, 3), CodedValue(code="X", text="New")),
    ]
    (contribution,) = rank_only(laws).contributions
    assert (contribution.code, contribution.label, contribution.count) == ("X", "New", 3)


def test_explain_bucket_accepts_a_generator() -> None:
    result = explain_bucket(
        (law for law in scenario()), target=month(6), baseline=BASELINE, dimensions=("rank",)
    )
    assert result.count == 7
    assert [c.code for c in result.contributions] == ["A", "B"]


# --- compare_topics -----------------------------------------------------------------------------

YEAR_2020 = {"start": date(2020, 1, 1), "end": date(2020, 12, 31), "period": "quarter"}


def test_compare_topics_one_entry_per_topic_in_mapping_order() -> None:
    topics = {
        "water": laws_on(date(2020, 1, 1), prefix="w"),
        "fire": laws_on(date(2020, 5, 1), date(2020, 5, 2), prefix="f"),
        "air": [],
    }
    result = compare_topics(topics, **YEAR_2020)  # type: ignore[arg-type]
    assert [t.name for t in result] == ["water", "fire", "air"]
    assert all(isinstance(t, TopicSeries) for t in result)
    assert [t.series.total for t in result] == [1, 2, 0]
    assert counts_of(result[1].series) == (0, 2, 0, 0)


def test_compare_topics_no_topics_gives_an_empty_tuple() -> None:
    assert compare_topics({}, **YEAR_2020) == ()  # type: ignore[arg-type]


def test_compare_topics_series_equal_count_over_time() -> None:
    laws = laws_on(date(2020, 2, 2), date(2019, 2, 2), None, date(2020, 8, 8))
    (entry,) = compare_topics(
        {"t": laws},
        today=date(2020, 7, 1),
        **YEAR_2020,  # type: ignore[arg-type]
    )
    expected = count_over_time(laws, today=date(2020, 7, 1), **YEAR_2020)  # type: ignore[arg-type]
    assert entry.series == expected


def test_compare_topics_a_law_in_two_topics_counts_in_both() -> None:
    shared = laws_on(date(2020, 1, 1))
    result = compare_topics({"a": shared, "b": shared}, **YEAR_2020)  # type: ignore[arg-type]
    assert [t.series.total for t in result] == [1, 1]


def test_compare_topics_peak_is_the_highest_complete_bucket() -> None:
    laws = laws_on(
        date(2020, 4, 1), date(2020, 5, 1), date(2020, 6, 1), date(2020, 1, 1), date(2020, 2, 1)
    )
    (entry,) = compare_topics({"t": laws}, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.peak == bucket_for(date(2020, 4, 1), "quarter")


def test_compare_topics_peak_ties_go_to_the_earliest_bucket() -> None:
    laws = laws_on(date(2020, 4, 1), date(2020, 10, 1), date(2020, 1, 1), date(2020, 7, 1))
    laws += laws_on(date(2020, 4, 2), date(2020, 7, 2), date(2020, 10, 2), prefix="M")
    (entry,) = compare_topics({"t": laws}, **YEAR_2020)  # type: ignore[arg-type]
    assert counts_of(entry.series) == (1, 2, 2, 2)
    assert entry.peak == bucket_for(date(2020, 4, 1), "quarter")


def test_compare_topics_peak_ignores_partial_buckets_from_today() -> None:
    laws = laws_on(date(2020, 1, 1), date(2020, 1, 2), date(2020, 7, 1), *[date(2020, 10, 1)] * 5)
    (entry,) = compare_topics(
        {"t": laws},
        today=date(2020, 9, 1),
        **YEAR_2020,  # type: ignore[arg-type]
    )
    assert entry.peak == bucket_for(date(2020, 1, 1), "quarter")


def test_compare_topics_peak_ignores_partial_buckets_from_a_cut_range() -> None:
    laws = laws_on(*[date(2020, 1, 20)] * 6, date(2020, 5, 1), date(2020, 5, 2))
    (entry,) = compare_topics(
        {"t": laws},
        start=date(2020, 1, 15),
        end=date(2020, 6, 30),
        period="quarter",
    )
    assert entry.peak == bucket_for(date(2020, 5, 1), "quarter")


def test_compare_topics_first_is_the_earliest_non_zero_bucket() -> None:
    laws = laws_on(date(2020, 8, 1), date(2020, 11, 1))
    (entry,) = compare_topics({"t": laws}, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.first == bucket_for(date(2020, 8, 1), "quarter")


def test_compare_topics_first_may_be_a_partial_bucket() -> None:
    # AMBIGUOUS RULE (compare_topics): "first" does not say "complete", so a partial bucket counts.
    laws = laws_on(date(2020, 1, 20), date(2020, 5, 1))
    (entry,) = compare_topics(
        {"t": laws}, start=date(2020, 1, 15), end=date(2020, 6, 30), period="quarter"
    )
    assert entry.first == bucket_for(date(2020, 1, 20), "quarter")
    assert entry.peak == bucket_for(date(2020, 5, 1), "quarter")


def test_compare_topics_no_laws_in_range_means_no_peak_and_no_first() -> None:
    laws = laws_on(date(2019, 1, 1), None)
    (entry,) = compare_topics({"t": laws}, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.peak is None
    assert entry.first is None
    assert entry.series.total == 0


def test_compare_topics_share_of_all() -> None:
    topic = laws_on(date(2020, 1, 1), date(2020, 2, 1))
    reference = laws_on(*[date(2020, 3, 1)] * 8, prefix="R")
    (entry,) = compare_topics({"t": topic}, reference=reference, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.share_of_all == pytest.approx(2 / 8)


def test_compare_topics_share_is_none_without_a_reference() -> None:
    (entry,) = compare_topics({"t": laws_on(date(2020, 1, 1))}, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.share_of_all is None


def test_compare_topics_share_is_none_when_the_reference_total_is_zero() -> None:
    reference = laws_on(date(2019, 1, 1), None, prefix="R")  # nothing in range
    (entry,) = compare_topics(
        {"t": laws_on(date(2020, 1, 1))},
        reference=reference,
        **YEAR_2020,  # type: ignore[arg-type]
    )
    assert entry.share_of_all is None
    (empty_ref,) = compare_topics(
        {"t": laws_on(date(2020, 1, 1))},
        reference=[],
        **YEAR_2020,  # type: ignore[arg-type]
    )
    assert empty_ref.share_of_all is None


def test_compare_topics_share_counts_only_the_reference_laws_in_range() -> None:
    reference = [
        *laws_on(date(2020, 1, 1), date(2020, 2, 1), prefix="in"),
        *laws_on(date(2019, 1, 1), date(2021, 1, 1), None, prefix="out"),
    ]
    topic = laws_on(date(2020, 1, 1), prefix="t")
    (entry,) = compare_topics({"t": topic}, reference=reference, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.share_of_all == pytest.approx(1 / 2)


def test_compare_topics_share_is_zero_for_an_empty_topic_with_a_reference() -> None:
    reference = laws_on(date(2020, 1, 1))
    (entry,) = compare_topics({"t": []}, reference=reference, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.share_of_all == 0.0


def test_compare_topics_share_is_per_topic() -> None:
    reference = laws_on(*[date(2020, 3, 1)] * 10, prefix="R")
    topics = {"a": laws_on(date(2020, 1, 1), prefix="a"), "b": laws_on(*[date(2020, 1, 1)] * 4)}
    result = compare_topics(topics, reference=reference, **YEAR_2020)  # type: ignore[arg-type]
    assert [t.share_of_all for t in result] == [pytest.approx(0.1), pytest.approx(0.4)]


def test_compare_topics_date_field_applies_to_topics_and_reference() -> None:
    topic = [make_law("a", published=date(2019, 1, 1), enacted=date(2020, 1, 1))]
    reference = [
        make_law("a", published=date(2019, 1, 1), enacted=date(2020, 1, 1)),
        make_law("b", published=date(2020, 1, 1), enacted=date(2019, 1, 1)),
    ]
    (entry,) = compare_topics(
        {"t": topic},
        reference=reference,
        date_field="enacted_on",
        **YEAR_2020,  # type: ignore[arg-type]
    )
    assert entry.series.total == 1
    assert entry.share_of_all == pytest.approx(1.0)


def test_compare_topics_accepts_generators_for_topics_and_reference() -> None:
    topics = {"t": (law for law in laws_on(date(2020, 1, 1), date(2020, 2, 2)))}
    reference = (law for law in laws_on(*[date(2020, 5, 5)] * 4, prefix="R"))
    (entry,) = compare_topics(topics, reference=reference, **YEAR_2020)  # type: ignore[arg-type]
    assert entry.series.total == 2
    assert entry.share_of_all == pytest.approx(0.5)


# --- cross-cutting: accounting identities on seeded random data ---------------------------------


def random_laws(seed: int, n: int) -> list[LawSummary]:
    """A reproducible mixed bag: dates in 2018-2022, some undated, some without ranks."""
    rng = random.Random(seed)
    first = date(2018, 1, 1)
    laws: list[LawSummary] = []
    for i in range(n):
        published = None if rng.random() < 0.1 else first + timedelta(days=rng.randrange(0, 1826))
        enacted = None if rng.random() < 0.2 else first + timedelta(days=rng.randrange(0, 1826))
        rank = None if rng.random() < 0.15 else f"R{rng.randrange(0, 7)}"
        laws.append(make_law(f"L{i:04d}", published=published, enacted=enacted, rank=rank))
    return laws


RANGES = [
    (date(2018, 1, 1), date(2022, 12, 31)),
    (date(2019, 2, 14), date(2021, 11, 3)),
    (date(2020, 1, 1), date(2020, 12, 31)),
    (date(2020, 6, 15), date(2020, 6, 20)),
]


@pytest.mark.parametrize("period", ["year", "quarter", "month"])
@pytest.mark.parametrize(("start", "end"), RANGES)
@pytest.mark.parametrize("date_field", ["published_on", "enacted_on"])
def test_count_over_time_accounting_identity(
    period: Period, start: date, end: date, date_field: str
) -> None:
    laws = random_laws(11, 300)
    series = count_over_time(
        laws,
        start=start,
        end=end,
        period=period,
        date_field=date_field,  # type: ignore[arg-type]
    )
    assert series.total + series.undated + series.outside == len(laws)
    assert sum(counts_of(series)) == series.total


@pytest.mark.parametrize("period", ["year", "quarter", "month"])
@pytest.mark.parametrize(("start", "end"), RANGES)
@pytest.mark.parametrize("top", [1, 3, 5])
def test_series_by_rows_always_add_up_to_count_over_time(
    period: Period, start: date, end: date, top: int
) -> None:
    laws = random_laws(5, 300)
    today = date(2020, 8, 1)
    rows = series_by(laws, "rank", start=start, end=end, period=period, top=top, today=today)
    overall = count_over_time(laws, start=start, end=end, period=period, today=today)
    if overall.total == 0:
        assert rows == ()
        return
    summed = tuple(sum(col) for col in zip(*(counts_of(r.series) for r in rows), strict=True))
    assert summed == counts_of(overall)
    assert sum(r.series.total for r in rows) == overall.total
    assert all(partials_of(r.series) == partials_of(overall) for r in rows)
    named = [r for r in rows if r.code is not None]
    assert len(named) <= top
    totals = [r.series.total for r in named]
    assert totals == sorted(totals, reverse=True)


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("top", [1, 2, 4, 20])
def test_count_by_accounting_identity_on_random_data(seed: int, top: int) -> None:
    laws = random_laws(seed, 200)
    result = count_by(laws, "rank", top=top)
    assert sum(g.count for g in result.groups) + result.other_count + result.missing == result.total
    assert result.total == 200
    assert result.missing == sum(1 for law in laws if law.rank is None)
    assert all(0.0 <= g.share <= 1.0 for g in result.groups)
    counts = [g.count for g in result.groups]
    assert counts == sorted(counts, reverse=True)


def test_every_function_is_deterministic() -> None:
    laws = random_laws(9, 150)
    start, end = date(2019, 1, 1), date(2021, 12, 31)
    assert count_by(laws, "rank", top=3) == count_by(laws, "rank", top=3)
    assert series_by(laws, "rank", start=start, end=end, period="quarter") == series_by(
        laws, "rank", start=start, end=end, period="quarter"
    )
    series = count_over_time(laws, start=start, end=end, period="month")
    assert find_spikes(series) == find_spikes(series)
    assert filter_laws(laws, rank_codes={"R1", "R2"}) == filter_laws(laws, rank_codes={"R2", "R1"})


def test_results_do_not_depend_on_input_order_when_ties_are_broken_by_the_rules() -> None:
    laws = random_laws(21, 120)
    shuffled = list(laws)
    random.Random(4).shuffle(shuffled)
    assert count_by(laws, "rank", top=4) == count_by(shuffled, "rank", top=4)
    start, end = date(2018, 1, 1), date(2022, 12, 31)
    forward = series_by(laws, "rank", start=start, end=end, period="year", top=3)
    backward = series_by(shuffled, "rank", start=start, end=end, period="year", top=3)
    assert [(r.code, r.label, counts_of(r.series)) for r in forward] == [
        (r.code, r.label, counts_of(r.series)) for r in backward
    ]


# --- highest official number per year -----------------------------------------------------------


def test_highest_official_number_per_year_of_one_state_rank() -> None:
    laws = [
        make_law("A", rank="1320", scope="1", number="2/2020"),
        make_law("B", rank="1320", scope="1", number="38/2020"),
        make_law("C", rank="1320", scope="1", number="9/2020"),
        make_law("D", rank="1320", scope="1", number="4/2019"),
    ]

    assert highest_official_numbers(laws, rank_code="1320", scope_code="1") == {2020: 38, 2019: 4}


def test_highest_official_number_ignores_other_ranks_regional_laws_and_odd_numbers() -> None:
    laws = [
        make_law("A", rank="1320", scope="1", number="5/2020"),
        make_law("B", rank="1300", scope="1", number="90/2020"),  # another rank
        make_law("C", rank="1320", scope="2", number="70/2020"),  # regional
        make_law("D", rank="1320", scope="1", number="EFD/80/2020"),  # not N/YYYY
        make_law("E", rank="1320", scope="1", number=None),
        make_law("F", rank="1320", scope="1", number="12/20"),  # not a four-digit year
    ]

    assert highest_official_numbers(laws, rank_code="1320", scope_code="1") == {2020: 5}


def test_highest_official_number_is_empty_without_numbers() -> None:
    assert highest_official_numbers([], rank_code="1320", scope_code="1") == {}
