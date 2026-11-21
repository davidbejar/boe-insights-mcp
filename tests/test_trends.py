"""Tests of ``trends``: the use cases behind the five trend tools.

The catalogue store runs over a fake search client, so every number is computed from laws built
in the test. Dates are fixed through ``now``/``today`` and the time limit of ``most_amended_laws``
through an injected clock; nothing sleeps.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from boe_insights_mcp import trends
from boe_insights_mcp.catalogue import CatalogueStore
from boe_insights_mcp.errors import BoeNotFoundError, BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import CodedValue, LawAnalysis, LawReference, LawSummary
from boe_insights_mcp.schemas import TopicSpec
from boe_insights_mcp.trends import TrendFilters
from tests.laws import make_law

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = date(2026, 11, 21)
NEWEST = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
CAVEAT_BASE = "Counts cover the BOE's consolidated legislation collection, not every item"
CAVEAT_REPRINT = "Regional laws are dated by their BOE reprint"
CAVEAT_PARTIAL = "Periods marked partial are incomplete and not comparable."
CAVEAT_TEXT = "Body-text matching is the BOE search engine's"

LEY = ("1300", "Ley")
RDL = ("1320", "Real Decreto-ley")
ORDEN = ("1350", "Orden")
STATE = ("1", "Estatal")
REGIONAL = ("2", "Autonómico")
JEFATURA = ("7723", "Jefatura del Estado")
HACIENDA = ("6000", "Ministerio de Hacienda")
CATALONIA = ("8070", "Comunidad Autónoma de Cataluña")


class FakeSearch:
    """Serves the catalogue for a plain query and a fixed hit list per search term."""

    def __init__(self, laws: list[LawSummary], selections: dict[str, list[LawSummary]]) -> None:
        self.laws = laws
        self.selections = selections
        self.queries: list[str | None] = []

    async def search_laws(
        self,
        query: str | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        ttl: float | None = None,
    ) -> list[LawSummary]:
        self.queries.append(query)
        document = json.loads(query or "{}")
        expression = document.get("query", {}).get("query_string", {}).get("query")
        hits = self.laws
        if expression is not None:
            matching = [key for key in self.selections if key in expression]
            assert matching, f"unexpected query {expression}"
            hits = self.selections[matching[0]]
        return hits[offset : offset + limit]


def store_over(
    laws: list[LawSummary], selections: dict[str, list[LawSummary]] | None = None
) -> tuple[CatalogueStore, FakeSearch]:
    fake = FakeSearch(laws, selections or {})
    return CatalogueStore(fake, clock=lambda: NOW), fake


def law(
    identifier: str,
    day: date | None,
    *,
    title: str = "Ley de prueba",
    rank: tuple[str, str] | None = LEY,
    department: tuple[str, str] | None = JEFATURA,
    scope: tuple[str, str] | None = STATE,
    enacted: date | None = None,
) -> LawSummary:
    return make_law(
        identifier,
        title=title,
        published=day,
        enacted=enacted,
        rank=rank,
        department=department,
        scope=scope,
        updated_at=NEWEST if identifier.endswith("-newest") else None,
    )


def per_year(years: dict[int, int], **kwargs: object) -> list[LawSummary]:
    """``years`` maps a year to how many laws are published in it (all on 15 June)."""
    laws: list[LawSummary] = []
    for year, count in years.items():
        for n in range(count):
            laws.append(law(f"L{year}-{n:03d}", date(year, 6, 15), **kwargs))  # type: ignore[arg-type]
    return laws


async def over_time(laws: list[LawSummary], **kwargs: object):
    store, _ = store_over(laws)
    options: dict[str, object] = {"now": NOW, "today": TODAY}
    options.update(kwargs)
    filters = options.pop("filters", TrendFilters())
    return await trends.legislation_over_time(store, filters, **options)  # type: ignore[arg-type]


# --- default range and validation --------------------------------------------------------------


async def test_the_default_range_is_the_last_twenty_calendar_years_to_today() -> None:
    result = await over_time(per_year({2010: 1}))

    assert (result.from_date, result.to_date) == (date(2007, 1, 1), TODAY)
    assert result.series[0].period == "2007"
    assert result.series[-1].period == "2026"
    assert len(result.series) == 20


async def test_only_from_date_given_runs_to_today() -> None:
    result = await over_time([], from_date=date(2024, 3, 1))

    assert (result.from_date, result.to_date) == (date(2024, 3, 1), TODAY)


async def test_only_to_date_given_starts_nineteen_years_before_its_year() -> None:
    result = await over_time([], to_date=date(2005, 5, 5))

    assert (result.from_date, result.to_date) == (date(1986, 1, 1), date(2005, 5, 5))


async def test_an_inverted_range_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="from_date"):
        await over_time([], from_date=date(2020, 1, 1), to_date=date(2019, 1, 1))


async def test_too_many_buckets_is_a_value_error_naming_the_period() -> None:
    with pytest.raises(ValueError, match="coarser"):
        await over_time([], from_date=date(1900, 1, 1), period="month")


@pytest.mark.parametrize(
    "filters", [TrendFilters(subject_code="1 OR 2"), TrendFilters(department_code="a b")]
)
async def test_malformed_codes_are_value_errors(filters: TrendFilters) -> None:
    with pytest.raises(ValueError, match="code"):
        await over_time([], filters=filters)


# --- legislation_over_time ---------------------------------------------------------------------


async def test_counts_per_year_with_the_current_year_partial() -> None:
    laws = per_year({2022: 2, 2023: 3, 2026: 1})

    result = await over_time(laws, from_date=date(2022, 1, 1))

    assert [(p.period, p.count, p.partial) for p in result.series] == [
        ("2022", 2, False),
        ("2023", 3, False),
        ("2024", 0, False),
        ("2025", 0, False),
        ("2026", 1, True),
    ]
    assert result.total == 6
    assert result.undated == 0


async def test_undated_laws_are_reported_not_counted() -> None:
    laws = [*per_year({2023: 2}), law("U1", None)]

    result = await over_time(laws, from_date=date(2023, 1, 1), to_date=date(2023, 12, 31))

    assert (result.total, result.undated) == (2, 1)


async def test_the_date_basis_selects_the_date_that_is_counted() -> None:
    laws = [law("A", date(2024, 3, 1), enacted=date(2023, 12, 20))]

    published = await over_time(laws, from_date=date(2023, 1, 1), to_date=date(2024, 12, 31))
    enacted = await over_time(
        laws, from_date=date(2023, 1, 1), to_date=date(2024, 12, 31), date_basis="enacted"
    )

    assert [p.count for p in published.series] == [0, 1]
    assert [p.count for p in enacted.series] == [1, 0]


async def test_quarters_and_months_use_their_key_formats() -> None:
    laws = [law("A", date(2024, 5, 2))]

    quarterly = await over_time(
        laws, from_date=date(2024, 1, 1), to_date=date(2024, 12, 31), period="quarter"
    )
    monthly = await over_time(
        laws, from_date=date(2024, 4, 1), to_date=date(2024, 6, 30), period="month"
    )

    assert [p.period for p in quarterly.series] == ["2024-Q1", "2024-Q2", "2024-Q3", "2024-Q4"]
    assert [(p.period, p.count) for p in monthly.series] == [
        ("2024-04", 0),
        ("2024-05", 1),
        ("2024-06", 0),
    ]


async def test_filters_combine_with_and() -> None:
    laws = [
        law("A", date(2020, 1, 1), title="Ley de protección de datos", rank=LEY, scope=STATE),
        law(
            "B",
            date(2020, 1, 1),
            title="Real Decreto sobre protección de datos",
            rank=ORDEN,
            scope=STATE,
        ),
        law(
            "C",
            date(2020, 1, 1),
            title="Ley de protección de datos",
            rank=LEY,
            scope=REGIONAL,
            department=CATALONIA,
        ),
        law("D", date(2020, 1, 1), title="Ley del juego", rank=LEY, scope=STATE),
    ]
    window = {"from_date": date(2020, 1, 1), "to_date": date(2020, 12, 31)}

    by_title = await over_time(laws, filters=TrendFilters(title="PROTECCION datos"), **window)
    by_title_and_rank = await over_time(
        laws, filters=TrendFilters(title="proteccion datos", rank="ley"), **window
    )
    regional = await over_time(laws, filters=TrendFilters(scope="regional"), **window)
    by_department = await over_time(laws, filters=TrendFilters(department_code="8070"), **window)

    assert by_title.total == 3
    assert by_title_and_rank.total == 2
    assert (regional.total, by_department.total) == (1, 1)


async def test_a_rank_filter_is_exact_on_the_code() -> None:
    ley_foral = law("F", date(2020, 1, 1), rank=("1450", "Ley Foral"))

    result = await over_time(
        [ley_foral, law("A", date(2020, 1, 1))],
        filters=TrendFilters(rank="ley"),
        from_date=date(2020, 1, 1),
        to_date=date(2020, 12, 31),
    )

    assert result.total == 1


async def test_text_and_subject_use_the_api_selection_then_the_local_filters() -> None:
    laws = [
        law("A", date(2020, 1, 1)),
        law("B", date(2020, 1, 1), rank=ORDEN),
        law("C", date(2020, 1, 1)),
    ]
    store, _ = store_over(laws, {"texto:teletrabajo": [laws[1], laws[0]]})

    result = await trends.legislation_over_time(
        store,
        TrendFilters(text="teletrabajo", rank="ley"),
        from_date=date(2020, 1, 1),
        to_date=date(2020, 12, 31),
        now=NOW,
        today=TODAY,
    )

    assert result.total == 1  # B is selected by the API but is not a Ley; C is not selected
    assert result.coverage.matched == 1
    assert result.coverage.catalogue_size == 3


async def test_coverage_counts_the_catalogue_and_the_laws_before_the_date_range() -> None:
    laws = [*per_year({2010: 2, 2022: 3}), law("X-newest", date(2022, 1, 1))]

    result = await over_time(laws, from_date=date(2022, 1, 1), to_date=date(2022, 12, 31))

    assert result.coverage.catalogue_size == 6
    assert result.coverage.matched == 6  # 2010 laws still match the filters
    assert result.total == 4
    assert result.coverage.catalogue_fetched_at == NOW


async def test_provenance_carries_the_catalogue_update_stamp_and_the_assembly_time() -> None:
    result = await over_time([law("X-newest", date(2022, 1, 1))])

    assert result.provenance.generated_at == NOW
    assert result.provenance.data_updated_at == NEWEST


async def test_caveats_are_selected_by_what_applies() -> None:
    inside_2020 = {"from_date": date(2020, 1, 1), "to_date": date(2020, 12, 31)}

    plain = await over_time([], **inside_2020)
    state = await over_time([], filters=TrendFilters(scope="state"), **inside_2020)
    enacted = await over_time([], date_basis="enacted", **inside_2020)
    to_today = await over_time([], from_date=date(2020, 1, 1))

    assert [c[:40] for c in plain.caveats] == [CAVEAT_BASE[:40], CAVEAT_REPRINT[:40]]
    assert [c[:40] for c in state.caveats] == [CAVEAT_BASE[:40]]
    assert [c[:40] for c in enacted.caveats] == [CAVEAT_BASE[:40]]
    assert CAVEAT_PARTIAL in to_today.caveats
    assert plain.caveats[0].startswith(CAVEAT_BASE)


async def test_the_text_caveat_comes_last_and_only_with_text() -> None:
    laws = [law("A", date(2020, 1, 1))]
    store, _ = store_over(laws, {"texto:x": laws})

    result = await trends.legislation_over_time(
        store,
        TrendFilters(text="x", scope="state"),
        from_date=date(2020, 1, 1),
        now=NOW,
        today=TODAY,
    )

    assert result.caveats[0].startswith(CAVEAT_BASE)
    assert result.caveats[1] == CAVEAT_PARTIAL
    assert result.caveats[2].startswith(CAVEAT_TEXT)
    assert len(result.caveats) == 3


async def test_grouping_by_rank_gives_top_groups_and_an_other_row() -> None:
    laws = [
        *per_year({2020: 3}, rank=LEY),
        *[law(f"R{n}", date(2020, 3, 1), rank=RDL) for n in range(2)],
        law("O1", date(2020, 3, 1), rank=ORDEN),
        law("O2", date(2020, 3, 1), rank=("1370", "Resolución")),
    ]

    result = await over_time(
        laws,
        from_date=date(2020, 1, 1),
        to_date=date(2020, 12, 31),
        group_by="rank",
        top_groups=2,
    )

    assert [(g.code, g.label, g.total) for g in result.groups] == [
        ("1300", "Ley", 3),
        ("1320", "Real Decreto-ley", 2),
        (None, "other", 2),
    ]
    assert sum(g.series[0].count for g in result.groups) == result.series[0].count == 7


async def test_ungrouped_results_have_no_groups() -> None:
    assert (await over_time([])).groups == []


async def test_spikes_report_the_period_count_baseline_and_ratio() -> None:
    laws = per_year({2016: 2, 2017: 2, 2018: 2, 2019: 2, 2020: 12, 2021: 2})

    result = await over_time(laws, from_date=date(2016, 1, 1), to_date=date(2021, 12, 31))

    assert [(s.period, s.count, s.baseline, s.ratio) for s in result.spikes] == [
        ("2020", 12, 2.0, 6.0)
    ]


async def test_the_series_is_ungrouped_even_when_groups_are_asked_for() -> None:
    laws = per_year({2016: 1, 2017: 1, 2018: 1, 2019: 1, 2020: 9}, rank=LEY)

    result = await over_time(
        laws, from_date=date(2016, 1, 1), to_date=date(2020, 12, 31), group_by="department"
    )

    assert result.spikes and result.spikes[0].period == "2020"
    assert result.total == 13


# --- top_departments ---------------------------------------------------------------------------


async def test_top_departments_ranks_by_count_inside_the_range() -> None:
    laws = [
        *[law(f"J{n}", date(2021, 5, 1), department=JEFATURA) for n in range(3)],
        *[law(f"H{n}", date(2021, 5, 1), department=HACIENDA) for n in range(2)],
        law("C1", date(2021, 5, 1), department=CATALONIA, scope=REGIONAL),
        law("Old", date(2010, 5, 1), department=HACIENDA),
    ]
    store, _ = store_over(laws)

    result = await trends.top_departments(
        store,
        TrendFilters(),
        from_date=date(2020, 1, 1),
        to_date=date(2022, 12, 31),
        limit=2,
        now=NOW,
        today=TODAY,
    )

    assert [(d.code, d.label, d.count) for d in result.departments] == [
        ("7723", "Jefatura del Estado", 3),
        ("6000", "Ministerio de Hacienda", 2),
    ]
    assert result.departments[0].share == pytest.approx(0.5)
    assert (result.other_count, result.total, result.distinct_departments) == (1, 6, 3)
    assert (result.from_date, result.to_date) == (date(2020, 1, 1), date(2022, 12, 31))
    assert result.coverage.matched == 7


async def test_top_departments_default_range_and_caveats() -> None:
    store, _ = store_over([law("A", date(2020, 1, 1))])

    result = await trends.top_departments(store, TrendFilters(), now=NOW, today=TODAY)

    assert (result.from_date, result.to_date) == (date(2007, 1, 1), TODAY)
    assert result.caveats[0].startswith(CAVEAT_BASE)
    assert result.caveats[1].startswith(CAVEAT_REPRINT)
    assert CAVEAT_PARTIAL not in result.caveats  # no series, so nothing can be partial


async def test_top_departments_limit_is_validated() -> None:
    store, _ = store_over([])

    for bad in (0, 51):
        with pytest.raises(ValueError, match="limit"):
            await trends.top_departments(store, TrendFilters(), limit=bad, now=NOW, today=TODAY)


async def test_top_departments_refuses_a_department_filter() -> None:
    store, _ = store_over([])

    with pytest.raises(ValueError, match="department"):
        await trends.top_departments(
            store, TrendFilters(department_code="7723"), now=NOW, today=TODAY
        )


# --- compare_topics ----------------------------------------------------------------------------


def topics_fixture() -> tuple[CatalogueStore, FakeSearch]:
    laws = [
        law("T1", date(2020, 4, 1), title="Ley del teletrabajo", rank=LEY),
        law("T2", date(2020, 9, 1), title="Real Decreto-ley de teletrabajo y datos", rank=RDL),
        law("D1", date(2021, 4, 1), title="Ley de protección de datos", rank=LEY),
        law("D2", date(2021, 5, 1), title="Orden de datos", rank=ORDEN),
        law("X1", date(2021, 6, 1), title="Ley del juego", rank=LEY),
    ]
    return store_over(laws, {"texto:privacidad": [laws[2], laws[4]]})


async def test_compare_topics_counts_each_topic_independently_in_input_order() -> None:
    store, _ = topics_fixture()

    result = await trends.compare_topics(
        store,
        [
            TopicSpec(name="datos", title="datos"),
            TopicSpec(name="teletrabajo", title="teletrabajo"),
        ],
        from_date=date(2020, 1, 1),
        to_date=date(2021, 12, 31),
        now=NOW,
        today=TODAY,
    )

    assert [t.name for t in result.topics] == ["datos", "teletrabajo"]
    datos, tele = result.topics
    assert (datos.total, tele.total) == (3, 2)  # T2 belongs to both topics
    assert [p.count for p in tele.series] == [2, 0]
    assert (tele.peak, tele.first) == ("2020", "2020")
    assert (datos.peak, datos.first) == ("2021", "2020")
    assert tele.share_of_all == pytest.approx(2 / 5, abs=1e-4)
    assert (result.from_date, result.to_date, result.period) == (
        date(2020, 1, 1),
        date(2021, 12, 31),
        "year",
    )


async def test_the_reference_applies_rank_scope_and_department_but_no_topic_criterion() -> None:
    store, _ = topics_fixture()

    result = await trends.compare_topics(
        store,
        [TopicSpec(name="datos", title="datos"), TopicSpec(name="juego", title="juego")],
        rank="ley",
        from_date=date(2020, 1, 1),
        to_date=date(2021, 12, 31),
        now=NOW,
        today=TODAY,
    )

    assert result.coverage.matched == 3  # T1, D1, X1 are the Leyes
    assert result.coverage.catalogue_size == 5
    assert [t.total for t in result.topics] == [1, 1]
    assert result.topics[0].share_of_all == pytest.approx(1 / 3, abs=1e-4)


async def test_a_topic_may_use_body_text_and_subject() -> None:
    store, fake = topics_fixture()

    result = await trends.compare_topics(
        store,
        [TopicSpec(name="privacidad", text="privacidad"), TopicSpec(name="juego", title="juego")],
        from_date=date(2020, 1, 1),
        to_date=date(2021, 12, 31),
        now=NOW,
        today=TODAY,
    )

    assert [t.total for t in result.topics] == [2, 1]
    assert any("texto:privacidad" in (q or "") for q in fake.queries)
    assert result.caveats[-1].startswith(CAVEAT_TEXT)


async def test_a_topic_combines_its_own_criteria_with_and() -> None:
    store, _ = topics_fixture()

    result = await trends.compare_topics(
        store,
        [
            TopicSpec(name="a", text="privacidad", title="juego"),
            TopicSpec(name="b", title="datos"),
        ],
        from_date=date(2020, 1, 1),
        to_date=date(2021, 12, 31),
        now=NOW,
        today=TODAY,
    )

    assert [t.total for t in result.topics] == [1, 3]


@pytest.mark.parametrize(
    ("topics", "message"),
    [
        ([TopicSpec(name="a", title="x")], "between 2 and 5"),
        ([TopicSpec(name=str(n), title="x") for n in range(6)], "between 2 and 5"),
        ([TopicSpec(name="a", title="x"), TopicSpec(name="b")], "criterion"),
        ([TopicSpec(name="a", title="x"), TopicSpec(name="b", title="  ")], "criterion"),
        ([TopicSpec(name="a", title="x"), TopicSpec(name="a", title="y")], "different"),
    ],
)
async def test_invalid_topic_lists_are_value_errors(topics: list[TopicSpec], message: str) -> None:
    store, _ = store_over([])

    with pytest.raises(ValueError, match=message):
        await trends.compare_topics(store, topics, now=NOW, today=TODAY)


# --- explain_spike -----------------------------------------------------------------------------


def spike_laws() -> list[LawSummary]:
    baseline = per_year(
        {2015: 1, 2016: 1, 2017: 1, 2018: 1, 2019: 1}, rank=LEY, department=JEFATURA
    )
    spike = [
        *[law(f"S{n}", date(2020, 3, n + 1), rank=RDL, department=HACIENDA) for n in range(5)],
        law("S9", date(2020, 4, 1), rank=LEY, department=JEFATURA),
    ]
    return [*baseline, *spike, *per_year({2021: 1})]


async def explain(laws: list[LawSummary], **kwargs: object):
    store, _ = store_over(laws)
    options: dict[str, object] = {"now": NOW, "today": TODAY}
    options.update(kwargs)
    filters = options.pop("filters", TrendFilters())
    return await trends.explain_spike(store, filters, **options)  # type: ignore[arg-type]


async def test_a_given_period_is_compared_with_the_periods_just_before_it() -> None:
    result = await explain(spike_laws(), period_key="2020")

    assert result.found is True
    assert (result.period, result.count, result.baseline_periods) == ("2020", 6, 5)
    assert result.baseline == pytest.approx(1.0)
    rank_rows = {(c.dimension, c.code): c for c in result.contributions}
    assert rank_rows[("rank", "1320")].count == 5
    assert rank_rows[("rank", "1320")].expected == 0.0
    assert rank_rows[("department", "6000")].excess == pytest.approx(5.0)
    assert result.laws_total == 6
    assert [item.identifier for item in result.laws][:2] == ["S0", "S1"]


async def test_laws_are_limited_to_max_laws_but_counted_in_full() -> None:
    result = await explain(spike_laws(), period_key="2020", max_laws=2)

    assert len(result.laws) == 2
    assert result.laws_total == 6


@pytest.mark.parametrize(
    ("period_key", "expected_count"),
    [("2020", 6), ("2020-Q2", 1), ("2020-03", 5)],
)
async def test_the_period_is_taken_from_the_period_key(
    period_key: str, expected_count: int
) -> None:
    result = await explain(spike_laws(), period_key=period_key)

    assert result.period == period_key
    assert result.count == expected_count


async def test_an_explicit_period_that_agrees_with_the_key_is_accepted() -> None:
    result = await explain(spike_laws(), period_key="2020-Q1", period="quarter")

    assert (result.period, result.count) == ("2020-Q1", 5)


async def test_a_period_that_contradicts_the_key_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="period"):
        await explain(spike_laws(), period_key="2020-Q2", period="year")


@pytest.mark.parametrize(
    "key", ["20", "2020-13", "2020-Q5", "2020-00", "abcd", "2020/03", "2020-3", ""]
)
async def test_a_malformed_period_key_is_a_value_error(key: str) -> None:
    with pytest.raises(ValueError, match="period_key"):
        await explain(spike_laws(), period_key=key)


async def test_baseline_periods_and_max_laws_are_validated() -> None:
    for kwargs in (
        {"baseline_periods": 1},
        {"baseline_periods": 21},
        {"max_laws": 0},
        {"max_laws": 51},
    ):
        with pytest.raises(ValueError):
            await explain(spike_laws(), period_key="2020", **kwargs)


async def test_without_a_period_key_the_strongest_spike_is_explained() -> None:
    result = await explain(spike_laws(), from_date=date(2015, 1, 1), to_date=date(2021, 12, 31))

    assert result.found is True
    assert (result.period, result.count) == ("2020", 6)
    assert result.other_spikes == []


async def test_other_spikes_lists_the_remaining_ones() -> None:
    laws = per_year(
        {2012: 1, 2013: 1, 2014: 1, 2015: 1, 2016: 1, 2017: 1, 2018: 20, 2019: 1, 2020: 10, 2021: 1}
    )

    result = await explain(laws, from_date=date(2012, 1, 1), to_date=date(2021, 12, 31))

    assert result.period == "2018"
    assert [s.period for s in result.other_spikes] == ["2020"]


async def test_no_spike_in_the_range_is_a_found_false_result_with_a_note() -> None:
    laws = per_year({year: 3 for year in range(2012, 2022)})

    result = await explain(laws, from_date=date(2012, 1, 1), to_date=date(2021, 12, 31))

    assert result.found is False
    assert result.period is None
    assert result.contributions == []
    assert result.laws == []
    assert result.note
    assert result.coverage.matched == 30


async def test_an_empty_period_is_explained_as_such() -> None:
    result = await explain(spike_laws(), period_key="2022")

    assert result.found is True
    assert result.count == 0
    assert result.note


async def test_the_unfinished_current_period_gets_the_partial_caveat() -> None:
    result = await explain(spike_laws(), period_key="2026")

    assert CAVEAT_PARTIAL in result.caveats
    assert CAVEAT_PARTIAL not in (await explain(spike_laws(), period_key="2020")).caveats


async def test_explain_filters_apply_to_the_explained_laws() -> None:
    result = await explain(
        spike_laws(), period_key="2020", filters=TrendFilters(rank="real_decreto_ley")
    )

    assert result.count == 5
    assert result.baseline == 0.0


# --- most_amended_laws -------------------------------------------------------------------------


def relation(code: str, text: str) -> CodedValue:
    return CodedValue(code=code, text=text)


def analysis(**kinds: int) -> LawAnalysis:
    """An analysis whose later references have the given relation texts (keyword = text key)."""
    texts = {
        "modifica": ("270", "SE MODIFICA"),
        "anade": ("407", "SE AÑADE"),
        "deroga": ("210", "SE DEROGA"),
        "corrige": ("203", "SE CORRIGEN errores"),
        "cita": ("330", "SE CITA"),
        "inconstitucional": ("470", "SE DECLARA"),
    }
    later = [
        LawReference(law_id=f"BOE-A-2000-{key}{n}", relation=relation(*texts[key]), text="t")
        for key, count in kinds.items()
        for n in range(count)
    ]
    return LawAnalysis(later=later)


class FakeAnalyses:
    """Stands in for the two analysis methods of ``BoeClient`` with a controllable clock."""

    def __init__(
        self, analyses: dict[str, LawAnalysis | Exception], *, seconds_each: float = 0.0
    ) -> None:
        self.analyses = analyses
        self.cached: set[str] = set()
        self.requested: list[str] = []
        self.now = 1000.0
        self.seconds_each = seconds_each

    def clock(self) -> float:
        return self.now

    async def has_cached_analysis(self, law_id: str) -> bool:
        return law_id in self.cached

    async def get_law_analysis(self, law_id: str) -> LawAnalysis:
        if law_id not in self.cached:
            self.requested.append(law_id)
            self.now += self.seconds_each
        outcome = self.analyses[law_id]
        if isinstance(outcome, Exception):
            raise outcome
        self.cached.add(law_id)
        return outcome


def lopd_laws(count: int) -> list[LawSummary]:
    """``count`` laws about data protection, one per year from 2001, ids ``D01``..."""
    return [
        law(f"D{n:02d}", date(2001 + n, 1, 1), title="Ley de protección de datos")
        for n in range(count)
    ]


async def most_amended(laws: list[LawSummary], fake: FakeAnalyses, **kwargs: object):
    store, _ = store_over(laws)
    options: dict[str, object] = {
        "now": NOW,
        "today": TODAY,
        "clock": fake.clock,
        "filters": TrendFilters(title="protección datos"),
    }
    options.update(kwargs)
    filters = options.pop("filters")
    return await trends.most_amended_laws(store, fake, filters, **options)  # type: ignore[arg-type]


async def test_a_filter_other_than_scope_is_required() -> None:
    fake = FakeAnalyses({})

    for filters in (TrendFilters(), TrendFilters(scope="state")):
        with pytest.raises(ValueError, match="filter"):
            await most_amended([], fake, filters=filters)


@pytest.mark.parametrize(
    "filters",
    [
        TrendFilters(title="x"),
        TrendFilters(text="x"),
        TrendFilters(subject_code="1"),
        TrendFilters(rank="ley"),
        TrendFilters(department_code="7723"),
    ],
)
async def test_each_of_the_five_filters_satisfies_the_requirement(filters: TrendFilters) -> None:
    store, _ = store_over([], {"texto:x": [], "materia@codigo:1": []})

    result = await trends.most_amended_laws(store, FakeAnalyses({}), filters, now=NOW, today=TODAY)

    assert result.candidates == 0


async def test_laws_are_ranked_by_amendments_then_identifier() -> None:
    laws = lopd_laws(4)
    fake = FakeAnalyses(
        {
            "D00": analysis(modifica=2),
            "D01": analysis(modifica=3, anade=1),
            "D02": analysis(modifica=4),
            "D03": analysis(),
        }
    )

    result = await most_amended(laws, fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31))

    assert [(item.law.identifier, item.amendments) for item in result.laws] == [
        ("D01", 4),
        ("D02", 4),
        ("D00", 2),
    ]
    assert result.laws[0].by_kind == {"modifies": 3, "adds": 1}
    assert result.laws[0].law.title == "Ley de protección de datos"
    assert (result.candidates, result.examined, result.failed, result.complete) == (4, 4, 0, True)
    assert result.note is None


async def test_only_amendments_are_counted_not_corrections_citations_or_rulings() -> None:
    fake = FakeAnalyses(
        {"D00": analysis(modifica=1, anade=1, deroga=1, corrige=4, cita=3, inconstitucional=2)}
    )

    result = await most_amended(
        lopd_laws(1), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert result.laws[0].amendments == 3
    assert result.laws[0].by_kind == {"modifies": 1, "adds": 1, "repeals": 1}


async def test_the_classification_is_the_one_of_get_amendment_history() -> None:
    from boe_insights_mcp.legislation import relation_group

    # Guards against a second classifier drifting from the amendment-history groups.
    amending = {g for g in ("repeals", "modifies", "adds")}
    for text, counted in [
        ("SE MODIFICA", True),
        ("SE AÑADE", True),
        ("SE DEROGA", True),
        ("SE ANULA", True),
        ("SE SUSTITUYE", True),
        ("SE CORRIGEN errores", False),
        ("SE DICTA DE CONFORMIDAD", False),
        ("Recurso", False),
    ]:
        assert (relation_group(text) in amending) is counted


async def test_laws_without_amendments_are_left_out_of_the_ranking() -> None:
    fake = FakeAnalyses({"D00": analysis(), "D01": analysis(modifica=1)})

    result = await most_amended(
        lopd_laws(2), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert [item.law.identifier for item in result.laws] == ["D01"]
    assert result.examined == 2


async def test_the_limit_keeps_the_top_laws() -> None:
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=n + 1) for n in range(5)})

    result = await most_amended(
        lopd_laws(5), fake, limit=2, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert [item.law.identifier for item in result.laws] == ["D04", "D03"]


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 26}, {"max_laws": 0}, {"max_laws": 61}])
async def test_limit_and_max_laws_are_validated(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        await most_amended([], FakeAnalyses({}), **kwargs)


async def test_candidates_are_examined_oldest_publication_first() -> None:
    laws = list(reversed(lopd_laws(4)))  # the catalogue lists newest first
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=1) for n in range(4)})

    await most_amended(laws, fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31))

    assert fake.requested == ["D00", "D01", "D02", "D03"]


async def test_only_laws_inside_the_date_range_and_basis_are_candidates() -> None:
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=1) for n in range(10)})

    result = await most_amended(
        lopd_laws(10), fake, from_date=date(2003, 1, 1), to_date=date(2005, 12, 31)
    )

    assert fake.requested == ["D02", "D03", "D04"]
    assert result.candidates == 3
    assert result.coverage.matched == 10


async def test_max_laws_caps_the_fresh_analyses_and_the_note_says_how_to_continue() -> None:
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=1) for n in range(5)})

    result = await most_amended(
        lopd_laws(5), fake, max_laws=2, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert fake.requested == ["D00", "D01"]
    assert (result.candidates, result.examined, result.complete) == (5, 2, False)
    assert result.note == (
        "Examined 2 of 5 matching laws. Call again with the same arguments to continue "
        "(results are cached), or narrow the filters."
    )


async def test_cached_analyses_are_free_and_a_second_call_continues_where_the_first_stopped() -> (
    None
):
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=n + 1) for n in range(5)})
    window = {"from_date": date(2001, 1, 1), "to_date": date(2010, 12, 31), "max_laws": 2}

    await most_amended(lopd_laws(5), fake, **window)
    second = await most_amended(lopd_laws(5), fake, **window)

    assert fake.requested == ["D00", "D01", "D02", "D03"]
    assert second.examined == 4  # two cached (free) plus two fresh
    third = await most_amended(lopd_laws(5), fake, **window)
    assert third.complete is True
    assert third.examined == 5
    assert third.laws[0].law.identifier == "D04"


async def test_the_time_limit_stops_the_loop_between_requests() -> None:
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=1) for n in range(8)}, seconds_each=15.0)

    result = await most_amended(
        lopd_laws(8), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    # The limit is 40 s: checks at 0, 15 and 30 s pass, the one at 45 s stops the loop.
    assert fake.requested == ["D00", "D01", "D02"]
    assert (result.examined, result.complete) == (3, False)
    assert result.note and result.note.startswith("Examined 3 of 8")


async def test_cached_analyses_do_not_count_against_the_time_limit() -> None:
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=1) for n in range(8)}, seconds_each=100.0)
    fake.cached.update({"D00", "D01", "D02"})

    result = await most_amended(
        lopd_laws(8), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert fake.requested == ["D03"]  # the one fresh request used up the whole limit
    assert result.examined == 4


async def test_a_retryable_failure_is_skipped_and_counted() -> None:
    fake = FakeAnalyses(
        {
            "D00": analysis(modifica=1),
            "D01": BoeUnavailableError("down"),
            "D02": analysis(modifica=2),
        }
    )

    result = await most_amended(
        lopd_laws(3), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert (result.examined, result.failed, result.complete) == (2, 1, True)
    assert [item.law.identifier for item in result.laws] == ["D02", "D00"]


async def test_a_failed_law_is_not_complete_when_candidates_remain() -> None:
    fake = FakeAnalyses({"D00": BoeUnavailableError("down"), "D01": analysis(modifica=1)})

    result = await most_amended(
        lopd_laws(2), fake, max_laws=1, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert (result.examined, result.failed, result.complete) == (0, 1, False)


async def test_a_non_retryable_failure_is_skipped_and_counted_too() -> None:
    # One law with a permanently malformed (or vanished) analysis must not sink the ranking.
    fake = FakeAnalyses(
        {
            "D00": BoeResponseError("malformed"),
            "D01": BoeNotFoundError("gone"),
            "D02": analysis(modifica=2),
        }
    )

    result = await most_amended(
        lopd_laws(3), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    assert (result.examined, result.failed, result.complete) == (1, 2, True)
    assert [item.law.identifier for item in result.laws] == ["D02"]


async def test_a_programming_error_still_propagates() -> None:
    fake = FakeAnalyses({"D00": RuntimeError("bug")})

    with pytest.raises(RuntimeError):
        await most_amended(
            lopd_laws(1), fake, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
        )


async def test_progress_counts_up_after_the_catalogue_download() -> None:
    fake = FakeAnalyses({f"D{n:02d}": analysis(modifica=1) for n in range(3)})
    reports: list[tuple[float, str]] = []

    async def progress(done: int, total: int | None, message: str) -> None:
        reports.append((done, message))

    laws = lopd_laws(3)
    await most_amended(
        laws, fake, progress=progress, from_date=date(2001, 1, 1), to_date=date(2010, 12, 31)
    )

    values = [value for value, _ in reports]
    assert values == sorted(set(values))  # strictly increasing, as MCP progress must be
    assert len(values) >= 3 + 1
    assert any("Examined 3 of 3" in message for _, message in reports)


async def test_most_amended_caveats_and_provenance() -> None:
    fake = FakeAnalyses({"D00": analysis(modifica=1)})

    result = await most_amended(
        [*lopd_laws(1), law("X-newest", date(2002, 1, 1))],
        fake,
        from_date=date(2001, 1, 1),
        to_date=date(2010, 12, 31),
    )

    assert result.caveats[0].startswith(CAVEAT_BASE)
    assert result.caveats[1].startswith(CAVEAT_REPRINT)
    assert result.provenance.data_updated_at == NEWEST
    assert (result.from_date, result.to_date) == (date(2001, 1, 1), date(2010, 12, 31))


async def test_dimensions_fixed_by_a_filter_are_not_reported_as_contributions() -> None:
    # Every law of a "rank = real_decreto_ley" set has that rank, so a rank row would only
    # repeat the total; the same goes for department_code and for a state or regional scope.
    ranked = await explain(
        spike_laws(), period_key="2020", filters=TrendFilters(rank="real_decreto_ley")
    )
    pinned = await explain(
        spike_laws(),
        period_key="2020",
        filters=TrendFilters(rank="ley", department_code="7723", scope="state"),
    )
    free = await explain(spike_laws(), period_key="2020")

    assert {c.dimension for c in ranked.contributions} == {"department", "scope"}
    assert pinned.contributions == []
    assert {c.dimension for c in free.contributions} == {"rank", "department", "scope"}
