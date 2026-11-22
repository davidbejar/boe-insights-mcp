"""Tests of ``gazette``: the use cases behind ``get_daily_summary`` and ``scan_gazette``.

The client is a fake serving summaries built in the test, with an injected clock (it advances
only when a "request" is made), so nothing here sleeps, reads the real clock or touches the
network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from boe_insights_mcp import gazette
from boe_insights_mcp.client import FIRST_SUMMARY_DAY
from boe_insights_mcp.errors import BoeNotFoundError, BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import DailySummary, GazetteIssue, GazetteItem

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = date(2026, 11, 21)  # a Saturday

JEFATURA = ("7723", "JEFATURA DEL ESTADO")
HACIENDA = ("6000", "MINISTERIO DE HACIENDA")
JUSTICIA = ("4810", "MINISTERIO DE JUSTICIA")


def item(
    identifier: str,
    title: str = "Resolución sobre algo",
    *,
    section: str = "1",
    department: tuple[str, str] | None = JEFATURA,
    heading: str | None = None,
    issue: str = "1",
    html: bool = True,
) -> GazetteItem:
    """A gazette item with a PDF link and, by default, an HTML link."""
    return GazetteItem(
        identifier=identifier,
        title=title,
        section_code=section,
        section_name=f"Sección {section}",
        department_code=department[0] if department else None,
        department_name=department[1] if department else None,
        heading=heading,
        issue_number=issue,
        first_page=1,
        last_page=2,
        pdf_url=f"https://www.boe.es/pdfs/{identifier}.pdf",
        html_url=f"https://www.boe.es/txt/{identifier}" if html else None,
    )


def summary(day: date, *items: GazetteItem, issues: tuple[str, ...] = ("1",)) -> DailySummary:
    """A summary of ``day`` with the given issues (numbers) and items."""
    return DailySummary(
        day=day,
        issues=tuple(GazetteIssue(number=n, identifier=f"BOE-S-2026-{n}") for n in issues),
        items=items,
    )


class FakeSummaries:
    """Stands in for the summary methods of ``BoeClient`` with a controllable clock."""

    def __init__(
        self, days: dict[date, DailySummary | Exception | None], *, seconds_each: float = 0.0
    ) -> None:
        self.days = days
        self.cached: set[date] = set()
        self.requested: list[date] = []
        self.now = 1000.0
        self.seconds_each = seconds_each

    def clock(self) -> float:
        return self.now

    async def has_cached_summary(self, day: date, *, today: date) -> bool:
        return day in self.cached

    async def get_daily_summary(self, day: date, *, today: date) -> DailySummary | None:
        if day < FIRST_SUMMARY_DAY or day > today:
            raise ValueError("date out of range")
        if day not in self.cached:
            self.requested.append(day)
            self.now += self.seconds_each
        outcome = self.days.get(day)
        if isinstance(outcome, Exception):
            if isinstance(outcome, BoeNotFoundError | BoeResponseError):
                self.cached.add(day)  # not important here; permanent failures are re-asked
            raise outcome
        self.cached.add(day)
        return outcome


def ago(days: int) -> date:
    return TODAY - timedelta(days=days)


async def one_day(fake: FakeSummaries, day: date | None = None, **kwargs: object):
    options: dict[str, object] = {"now": NOW, "today": TODAY}
    options.update(kwargs)
    return await gazette.daily_summary(fake, day=day, **options)  # type: ignore[arg-type]


async def scan(fake: FakeSummaries, **kwargs: object):
    options: dict[str, object] = {"now": NOW, "today": TODAY, "clock": fake.clock, "words": "ley"}
    options.update(kwargs)
    return await gazette.scan_gazette(fake, **options)  # type: ignore[arg-type]


def sample_day() -> FakeSummaries:
    """2026-11-20: five items in three sections and three departments, two issues."""
    day = date(2026, 11, 20)
    return FakeSummaries(
        {
            day: summary(
                day,
                item("A1", "Ley Orgánica de protección de datos", section="1"),
                item("A2", "Real Decreto de vivienda", section="1", department=HACIENDA),
                item("A3", "Nombramiento de un Magistrado", section="2A", department=JUSTICIA),
                item("B1", "Anuncio de licitación de VIVIENDAS", section="5A", department=HACIENDA),
                item("A4", "Orden de Hacienda", section="1", department=HACIENDA, issue="2"),
                issues=("1", "2"),
            )
        }
    )


# --- get_daily_summary -------------------------------------------------------------------------


async def test_a_day_is_summarised_by_section_and_department() -> None:
    result = await one_day(sample_day(), date(2026, 11, 20))

    assert result.date == date(2026, 11, 20)
    assert result.published is True
    assert [(i.number, i.identifier) for i in result.issues] == [
        ("1", "BOE-S-2026-1"),
        ("2", "BOE-S-2026-2"),
    ]
    assert result.total_items == 5
    # Whole day, in order of first appearance; section 1 is counted across both issues.
    assert [(s.code, s.name, s.items) for s in result.sections] == [
        ("1", "Sección 1", 3),
        ("2A", "Sección 2A", 1),
        ("5A", "Sección 5A", 1),
    ]
    assert [(d.code, d.name, d.items) for d in result.departments] == [
        ("6000", "MINISTERIO DE HACIENDA", 3),
        ("7723", "JEFATURA DEL ESTADO", 1),
        ("4810", "MINISTERIO DE JUSTICIA", 1),
    ]
    assert (result.matched, result.returned, result.has_more, result.next_offset) == (
        5,
        5,
        False,
        None,
    )
    assert result.note is None


async def test_provenance_is_stamped_and_dated_by_the_issue_read() -> None:
    result = await one_day(sample_day(), date(2026, 11, 20))

    assert result.provenance.generated_at == NOW
    # C5: summaries have no stamp of their own, so the data's date is the day of the issue.
    assert result.provenance.data_updated_at == datetime(2026, 11, 20, tzinfo=UTC)
    assert result.provenance.attribution.startswith("Basado en datos")


async def test_entries_are_compact_with_html_link_else_pdf() -> None:
    day = date(2026, 11, 20)
    fake = FakeSummaries(
        {
            day: summary(
                day,
                item("A1", heading="Medidas urgentes"),
                item("B1", section="4", department=None, html=False),
            )
        }
    )

    result = await one_day(fake, day)

    first, second = result.items
    assert first.model_dump() == {
        "identifier": "A1",
        "title": "Resolución sobre algo",
        "section": "1",
        "department": "JEFATURA DEL ESTADO",
        "heading": "Medidas urgentes",
        "url": "https://www.boe.es/txt/A1",
    }
    assert second.heading is None
    assert second.department is None
    assert second.url == "https://www.boe.es/pdfs/B1.pdf"


async def test_titles_up_to_200_characters_are_kept_whole() -> None:
    day = date(2026, 11, 20)
    exact = "x" * 200
    fake = FakeSummaries({day: summary(day, item("A1", exact))})

    result = await one_day(fake, day)

    assert result.items[0].title == exact


async def test_longer_titles_are_cut_at_a_word_boundary_within_200_characters() -> None:
    day = date(2026, 11, 20)
    words = ["palabra"] * 40  # 7 letters + 1 space each: 319 characters
    long = " ".join(words)
    fake = FakeSummaries({day: summary(day, item("A1", long))})

    result = await one_day(fake, day)

    title = result.items[0].title
    assert title.endswith("…")
    body = title[:-1]  # the ellipsis is not counted in the 200
    assert len(body) <= 200
    assert body == " ".join(["palabra"] * 25)  # 199 characters; a 26th word would pass 200
    assert long.startswith(body + " ")


async def test_a_cut_that_falls_exactly_between_words_keeps_the_whole_word_before_it() -> None:
    day = date(2026, 11, 20)
    first = " ".join(["w" * 9] * 19 + ["z" * 10])  # exactly 200 characters, a space follows
    assert len(first) == 200
    fake = FakeSummaries({day: summary(day, item("A1", first + " and the rest of it"))})

    result = await one_day(fake, day)

    assert result.items[0].title == first + "…"


async def test_a_title_that_is_one_long_word_is_cut_hard_at_200() -> None:
    day = date(2026, 11, 20)
    fake = FakeSummaries({day: summary(day, item("A1", "y" * 500))})

    result = await one_day(fake, day)

    assert result.items[0].title == "y" * 200 + "…"


async def test_the_issue_is_given_only_when_the_day_has_several_issues() -> None:
    day = date(2026, 11, 20)
    several = FakeSummaries(
        {
            day: summary(
                day,
                item("A1", issue="67", section="1"),
                item("A2", issue="66", section="2A"),
                issues=("67", "66"),
            )
        }
    )
    single = FakeSummaries({day: summary(day, item("A1", issue="66"))})

    many = await one_day(several, day)
    one = await one_day(single, day)

    assert [i.issue for i in many.items] == ["67", "66"]
    assert [e["issue"] for e in many.model_dump()["items"]] == ["67", "66"]
    assert one.items[0].issue is None
    assert "issue" not in one.model_dump()["items"][0]


async def test_a_scan_gives_the_issue_per_day() -> None:
    multi, plain = ago(1), ago(0)
    fake = FakeSummaries(
        {
            multi: summary(multi, item("M1", "Ley uno", issue="9"), issues=("9", "8")),
            plain: summary(plain, item("P1", "Ley dos", issue="10")),
        }
    )

    result = await scan(fake, from_date=multi, to_date=plain)

    by_id = {i.identifier: i.model_dump() for i in result.items}
    assert by_id["M1"]["issue"] == "9"
    assert "issue" not in by_id["P1"]


async def test_gazette_results_carry_the_gazette_notice_not_the_consolidated_one() -> None:
    day = date(2026, 11, 20)
    summary_result = await one_day(sample_day(), day)
    absent = await one_day(FakeSummaries({}), day)
    scanned = await scan(week_of_laws([ago(0)]), from_date=ago(0), to_date=ago(0))

    for result in (summary_result, absent, scanned):
        provenance = result.provenance
        assert provenance.notice == (
            "Summary of the official gazette. Only the texts published in the BOE itself are "
            "authentic."
        )
        assert "onsolidated" not in provenance.notice
        assert (
            provenance.attribution
            == "Basado en datos de la Agencia Estatal Boletín Oficial del Estado"
        )
        assert provenance.source_url == "https://www.boe.es"


async def test_the_default_page_is_25_items_and_100_is_the_maximum() -> None:
    day = date(2026, 11, 20)
    fake = FakeSummaries({day: summary(day, *[item(f"A{n}", "Ley") for n in range(40)])})

    default = await one_day(fake, day)
    scanned = await scan(fake, from_date=day, to_date=day)

    assert gazette.DEFAULT_LIMIT == 25
    assert gazette.MAX_LIMIT == 100
    assert (default.returned, default.has_more, default.next_offset) == (25, True, 25)
    assert (scanned.returned, scanned.has_more, scanned.next_offset) == (25, True, 25)
    with pytest.raises(ValueError, match="limit"):
        await one_day(fake, day, limit=101)


async def test_section_filter_ignores_case() -> None:
    result = await one_day(sample_day(), date(2026, 11, 20), section="2a")

    assert [i.identifier for i in result.items] == ["A3"]
    assert (result.matched, result.total_items) == (1, 5)
    # The sections of the whole day stay whole; the departments follow the filter.
    assert len(result.sections) == 3
    assert [d.code for d in result.departments] == ["4810"]


async def test_department_filter() -> None:
    result = await one_day(sample_day(), date(2026, 11, 20), department_code="6000")

    assert [i.identifier for i in result.items] == ["A2", "B1", "A4"]


async def test_words_filter_needs_all_whole_words_ignoring_case_accents_and_number() -> None:
    fake = sample_day()

    assert [
        i.identifier
        for i in (await one_day(fake, date(2026, 11, 20), words="PROTECCION datos")).items
    ] == ["A1"]
    # Singular and plural forms match each other: "vivienda" finds "viviendas" too.
    assert (await one_day(fake, date(2026, 11, 20), words="vivienda")).matched == 2
    assert (await one_day(fake, date(2026, 11, 20), words="viviendas")).matched == 2
    assert (await one_day(fake, date(2026, 11, 20), words="viv")).matched == 0
    assert (await one_day(fake, date(2026, 11, 20), words="vivienda datos")).matched == 0


async def test_filters_combine_and_blank_ones_are_ignored() -> None:
    fake = sample_day()

    both = await one_day(fake, date(2026, 11, 20), section="1", department_code="6000")
    blank = await one_day(fake, date(2026, 11, 20), section=" ", department_code="", words="  ")

    assert [i.identifier for i in both.items] == ["A2", "A4"]
    assert blank.matched == 5


async def test_the_departments_list_has_the_fifteen_largest_ties_in_order_of_appearance() -> None:
    day = date(2026, 11, 20)
    items = [item(f"A{n}", department=(str(n), f"D{n}")) for n in range(20)]
    items.append(item("EXTRA", department=("19", "D19")))
    fake = FakeSummaries({day: summary(day, *items)})

    result = await one_day(fake, day)

    assert len(result.departments) == 15
    assert [d.code for d in result.departments][:2] == ["19", "0"]
    assert result.departments[0].items == 2


async def test_items_without_a_department_are_grouped_under_a_null_code() -> None:
    day = date(2026, 11, 20)
    fake = FakeSummaries(
        {day: summary(day, item("A1", department=None), item("A2", department=None))}
    )

    result = await one_day(fake, day)

    assert [(d.code, d.name, d.items) for d in result.departments] == [(None, None, 2)]


async def test_paging() -> None:
    day = date(2026, 11, 20)
    fake = FakeSummaries({day: summary(day, *[item(f"A{n}") for n in range(5)])})

    first = await one_day(fake, day, limit=2)
    last = await one_day(fake, day, offset=4, limit=2)
    beyond = await one_day(fake, day, offset=9, limit=2)

    assert ([i.identifier for i in first.items], first.has_more, first.next_offset) == (
        ["A0", "A1"],
        True,
        2,
    )
    assert ([i.identifier for i in last.items], last.has_more, last.next_offset) == (
        ["A4"],
        False,
        None,
    )
    assert (beyond.items, beyond.returned, beyond.has_more, beyond.matched) == ([], 0, False, 5)


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_bad_paging_arguments_are_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        await one_day(sample_day(), date(2026, 11, 20), **kwargs)


async def test_a_day_without_an_issue_says_so() -> None:
    fake = FakeSummaries({date(2026, 11, 15): None})

    result = await one_day(fake, date(2026, 11, 15))

    assert result.published is False
    assert result.date == date(2026, 11, 15)
    assert (result.issues, result.sections, result.departments, result.items) == ([], [], [], [])
    assert (result.total_items, result.matched, result.returned, result.has_more) == (
        0,
        0,
        0,
        False,
    )
    assert result.note == (
        "No BOE issue on 2026-11-15: no publication that day, or not yet published."
    )


async def test_a_date_outside_the_archive_raises() -> None:
    with pytest.raises(ValueError):
        await one_day(FakeSummaries({}), date(1960, 8, 31))
    with pytest.raises(ValueError):
        await one_day(FakeSummaries({}), TODAY + timedelta(days=1))


async def test_without_a_date_the_latest_issue_is_given() -> None:
    fake = FakeSummaries({TODAY: summary(TODAY, item("A1"))})

    result = await one_day(fake)

    assert (result.date, result.published, result.note) == (TODAY, True, None)
    assert fake.requested == [TODAY]


async def test_without_a_date_it_falls_back_over_days_without_an_issue_and_says_so() -> None:
    saturday_before = ago(2)  # Thursday 2026-11-19
    fake = FakeSummaries({saturday_before: summary(saturday_before, item("A1"))})

    result = await one_day(fake)

    assert (result.date, result.published) == (date(2026, 11, 19), True)
    assert fake.requested == [TODAY, ago(1), ago(2)]
    assert result.note == (
        "No BOE issue on 2026-11-21 (no publication that day, or not yet published): "
        "showing the latest issue, 2026-11-19."
    )


async def test_without_a_date_it_looks_back_seven_days_at_most() -> None:
    fake = FakeSummaries({ago(8): summary(ago(8), item("A1"))})

    result = await one_day(fake)

    assert result.published is False
    assert result.date == TODAY
    assert fake.requested == [ago(n) for n in range(8)]  # today and the seven days before
    assert result.note == "No BOE issue found from 2026-11-14 to 2026-11-21."


async def test_without_a_date_today_comes_from_the_madrid_clock() -> None:
    # 23:30 UTC on the 20th is already the 21st in Madrid.
    late = datetime(2026, 11, 20, 23, 30, tzinfo=UTC)
    fake = FakeSummaries({TODAY: summary(TODAY, item("A1"))})

    result = await gazette.daily_summary(fake, day=None, now=late)

    assert result.date == TODAY


async def test_a_retryable_error_propagates() -> None:
    fake = FakeSummaries({date(2026, 11, 20): BoeUnavailableError("down")})

    with pytest.raises(BoeUnavailableError):
        await one_day(fake, date(2026, 11, 20))


# --- scan_gazette ------------------------------------------------------------------------------


def week_of_laws(dates: list[date]) -> FakeSummaries:
    """Each given day has two items; the second one is a "Ley" announcement."""
    return FakeSummaries(
        {
            d: summary(
                d,
                item(f"A-{d:%d}", "Resolución de personal"),
                item(f"B-{d:%d}", "Anuncio de la Ley de algo", section="5A", department=HACIENDA),
            )
            for d in dates
        }
    )


async def test_the_default_range_is_the_seven_days_ending_today_newest_first() -> None:
    fake = week_of_laws([ago(n) for n in range(7)])

    result = await scan(fake)

    assert (result.from_date, result.to_date) == (ago(6), TODAY)
    assert fake.requested == [ago(n) for n in range(7)]
    assert (result.days_scanned, result.days_with_issue, result.days_pending) == (7, 7, 0)
    assert (result.complete, result.days_failed, result.note) == (True, 0, None)
    assert result.matched == 7
    assert [i.date for i in result.items] == [ago(n) for n in range(7)]
    assert [(d.date, d.matches) for d in result.by_day] == [(ago(n), 1) for n in range(7)]
    assert result.items[0].model_dump(exclude={"heading"}) == {
        "identifier": "B-21",
        "title": "Anuncio de la Ley de algo",
        "section": "5A",
        "department": "MINISTERIO DE HACIENDA",
        "url": "https://www.boe.es/txt/B-21",
        "date": TODAY,
    }
    assert result.provenance.data_updated_at == datetime(2026, 11, 21, tzinfo=UTC)


async def test_days_without_an_issue_are_scanned_but_have_no_matches() -> None:
    fake = week_of_laws([ago(0), ago(2)])

    result = await scan(fake, from_date=ago(3), to_date=ago(0))

    assert (result.days_scanned, result.days_with_issue, result.matched) == (4, 2, 2)
    assert [d.date for d in result.by_day] == [ago(0), ago(2)]


async def test_one_of_words_section_or_department_is_required() -> None:
    fake = FakeSummaries({})

    for blank in (
        {"words": None},
        {"words": "   "},
        {"words": None, "section": " "},
    ):
        with pytest.raises(ValueError, match="at least one"):
            await scan(fake, **blank)
    assert fake.requested == []


async def test_filters_of_a_scan_work_like_the_single_day_ones() -> None:
    day = date(2026, 11, 20)
    fake = sample_day()

    by_section = await scan(fake, words=None, section="2A", from_date=day, to_date=day)
    by_department = await scan(fake, words=None, department_code="6000", from_date=day, to_date=day)
    by_words = await scan(fake, words="vivienda", from_date=day, to_date=day)

    assert [i.identifier for i in by_section.items] == ["A3"]
    assert [i.identifier for i in by_department.items] == ["A2", "B1", "A4"]
    assert [i.identifier for i in by_words.items] == ["A2", "B1"]


async def test_document_order_is_kept_within_a_day() -> None:
    day = date(2026, 11, 20)
    fake = FakeSummaries({day: summary(day, *[item(f"A{n}", "Ley") for n in (3, 1, 2)])})

    result = await scan(fake, from_date=day, to_date=day)

    assert [i.identifier for i in result.items] == ["A3", "A1", "A2"]


async def test_a_range_of_31_days_is_allowed_and_32_is_not() -> None:
    fake = FakeSummaries({})

    ok = await scan(fake, from_date=ago(30), to_date=TODAY)
    with pytest.raises(ValueError, match="31 days"):
        await scan(fake, from_date=ago(31), to_date=TODAY)

    assert ok.days_scanned == 31


async def test_a_range_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="from_date"):
        await scan(FakeSummaries({}), from_date=ago(1), to_date=ago(3))


async def test_only_one_end_given_the_other_follows_from_the_seven_day_default() -> None:
    fake = FakeSummaries({})

    only_to = await scan(fake, to_date=ago(10))
    only_from = await scan(fake, from_date=ago(3))

    assert (only_to.from_date, only_to.to_date) == (ago(16), ago(10))
    assert (only_from.from_date, only_from.to_date) == (ago(3), TODAY)


async def test_a_future_end_is_cut_back_to_today_and_the_archive_start_is_respected() -> None:
    fake = FakeSummaries({})

    future = await scan(fake, from_date=ago(2), to_date=TODAY + timedelta(days=30))
    early = await scan(
        fake,
        from_date=FIRST_SUMMARY_DAY - timedelta(days=5),
        to_date=FIRST_SUMMARY_DAY + timedelta(days=2),
    )

    assert (future.from_date, future.to_date, future.days_scanned) == (ago(2), TODAY, 3)
    assert (early.from_date, early.days_scanned) == (FIRST_SUMMARY_DAY, 3)


async def test_a_range_entirely_in_the_future_is_refused() -> None:
    with pytest.raises(ValueError, match="future"):
        await scan(FakeSummaries({}), from_date=TODAY + timedelta(days=1))


async def test_paging_over_the_matches_of_several_days() -> None:
    fake = week_of_laws([ago(n) for n in range(7)])

    first = await scan(fake, limit=3)
    last = await scan(fake, offset=6, limit=3)

    assert ([i.date for i in first.items], first.has_more, first.next_offset) == (
        [ago(0), ago(1), ago(2)],
        True,
        3,
    )
    assert (first.matched, first.returned) == (7, 3)
    assert ([i.date for i in last.items], last.has_more, last.next_offset) == (
        [ago(6)],
        False,
        None,
    )


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_bad_scan_paging_arguments_are_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        await scan(FakeSummaries({}), **kwargs)


async def test_the_budget_leaves_the_older_days_pending_and_a_second_call_resumes() -> None:
    # Each fresh day costs 20 s; the loop stops at 35 s (40 s minus the 5 s margin).
    fake = week_of_laws([ago(n) for n in range(7)])
    fake.seconds_each = 20.0

    first = await scan(fake)

    assert fake.requested == [ago(0), ago(1)]
    assert (first.days_scanned, first.days_pending, first.complete) == (2, 5, False)
    assert first.matched == 2
    assert first.note == (
        "Scanned 2 of 7 days. Call again with the same arguments to continue "
        "(days already read are cached)."
    )

    fake.requested.clear()
    fake.now = 1000.0  # a new call starts a new budget
    second = await scan(fake)

    # The two cached days are free, so this call reads two new ones before the budget ends.
    assert fake.requested == [ago(2), ago(3)]
    assert (second.days_scanned, second.days_pending) == (4, 3)
    assert second.matched == 4


async def test_cached_days_never_use_the_budget() -> None:
    fake = week_of_laws([ago(n) for n in range(7)])
    fake.cached = {ago(n) for n in range(7)}
    fake.seconds_each = 1000.0

    result = await scan(fake)

    assert (result.complete, fake.requested) == (True, [])


async def test_the_clock_is_read_from_the_start_of_the_call() -> None:
    fake = week_of_laws([ago(n) for n in range(3)])
    fake.seconds_each = 10.0

    short = await scan(fake, time_limit=15.0)  # 10 s of budget: one day

    assert fake.requested == [ago(0)]
    assert short.days_scanned == 1


async def test_a_retryable_error_leaves_that_day_pending_and_the_scan_goes_on() -> None:
    fake = week_of_laws([ago(n) for n in range(3)])
    fake.days[ago(1)] = BoeUnavailableError("down")

    result = await scan(fake, from_date=ago(2), to_date=ago(0))

    assert fake.requested == [ago(0), ago(1), ago(2)]
    assert (result.days_scanned, result.days_pending, result.days_failed) == (2, 1, 0)
    assert result.complete is False
    assert result.matched == 2
    assert result.note == (
        "Scanned 2 of 3 days. Call again with the same arguments to continue "
        "(days already read are cached). 1 day could not be read and will be retried."
    )


async def test_a_permanent_error_counts_the_day_as_failed_without_failing_the_scan() -> None:
    fake = week_of_laws([ago(n) for n in range(3)])
    fake.days[ago(1)] = BoeResponseError("garbage")

    result = await scan(fake, from_date=ago(2), to_date=ago(0))

    assert (result.days_scanned, result.days_pending, result.days_failed) == (2, 0, 1)
    assert result.complete is True
    assert result.matched == 2
    assert result.note == "1 day could not be read and was skipped."


async def test_progress_is_reported_once_per_day_with_increasing_values() -> None:
    fake = week_of_laws([ago(n) for n in range(3)])
    reports: list[tuple[int, int | None, str]] = []

    async def progress(done: int, total: int | None, message: str) -> None:
        reports.append((done, total, message))

    await scan(fake, from_date=ago(2), to_date=ago(0), progress=progress)

    assert [(done, total) for done, total, _ in reports] == [(1, 3), (2, 3), (3, 3)]
    assert reports[0][2] == "Read 2026-11-21 (1 of 3 days)"


# --- review fixes: C1 to C5 ----------------------------------------------------------------------


async def test_a_scan_that_ends_on_an_unpublished_today_says_so() -> None:
    fake = FakeSummaries({ago(1): summary(ago(1), item("A1", "Ley de algo"))})

    result = await scan(fake, from_date=ago(2), to_date=TODAY)

    assert result.complete is True
    assert (
        result.note == "No issue for today when scanned: none is published on most Sundays, "
        "or it is not out yet."
    )
    assert result.provenance.data_updated_at == datetime(
        ago(1).year, ago(1).month, ago(1).day, tzinfo=UTC
    )


async def test_a_scan_of_past_days_only_has_no_today_note() -> None:
    fake = FakeSummaries({})

    result = await scan(fake, from_date=ago(5), to_date=ago(3))

    assert result.note is None
    assert result.provenance.data_updated_at is None


async def test_the_department_filter_ignores_leading_zeros_and_follows_recoded_codes() -> None:
    day = date(2000, 6, 14)
    madrid_old = ("8130", "COMUNIDAD DE MADRID")
    fake = FakeSummaries({day: summary(day, item("M1", department=madrid_old), item("A1"))})

    for code in ("8130", "8131", "08130", " 8131 "):
        result = await one_day(fake, day, department_code=code)
        assert [i.identifier for i in result.items] == ["M1"], code
    assert (await one_day(fake, day, department_code="7723")).matched == 1


async def test_words_without_letters_or_digits_are_a_bad_argument() -> None:
    fake = sample_day()

    for bad in ("???", "¿?", "-"):
        with pytest.raises(ValueError, match="letters or digits"):
            await one_day(fake, date(2026, 11, 20), words=bad)
        with pytest.raises(ValueError, match="letters or digits"):
            await scan(fake, words=bad)


async def test_a_scan_range_before_the_first_summary_is_refused() -> None:
    with pytest.raises(ValueError, match=r"The BOE daily summaries start on 1960-09-01\."):
        await scan(FakeSummaries({}), from_date=date(1950, 1, 1), to_date=date(1950, 1, 10))
    with pytest.raises(ValueError, match="start on 1960-09-01"):
        await scan(FakeSummaries({}), from_date=date(1960, 8, 1), to_date=date(1960, 8, 31))


async def test_a_section_that_matches_nothing_lists_the_sections_present() -> None:
    result = await one_day(sample_day(), date(2026, 11, 20), section="III")

    assert result.matched == 0
    assert result.note == "No item of section 'III' that day; sections present: 1, 2A, 5A."


async def test_a_section_that_exists_adds_no_note() -> None:
    result = await one_day(sample_day(), date(2026, 11, 20), section="2a")

    assert result.note is None
