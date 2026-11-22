"""Tests of ``saved_searches``: the use cases behind the five saved-search tools.

The store is a real in-memory ``SavedSearchStore``. The BOE is a fake client that records every
request in order and has a clock advancing only when a "request" is made, so nothing here
sleeps, reads the real clock or touches the network. Bookmarks are seeded straight into the
store when a test needs a search saved "a while ago".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from boe_insights_mcp import saved_searches
from boe_insights_mcp.errors import BoeNotFoundError, BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import (
    CodedValue,
    DailySummary,
    GazetteIssue,
    GazetteItem,
    LawAnalysis,
    LawMetadata,
    LawReference,
)
from boe_insights_mcp.schemas import GAZETTE_NOTICE, Provenance
from boe_insights_mcp.searchstore import SavedSearch, SavedSearchStore

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = date(2026, 11, 21)  # a Saturday
OWNER = "local"
OTHER = "user:other"
LAW = "BOE-A-2015-10566"
NO_TODAY_ISSUE = (
    "No issue for today when scanned: none is published on most Sundays, or it is not out yet."
)
NO_SEARCHES = "No saved searches. Create one with save_search."


def ago(days: int) -> date:
    """The day ``days`` before ``TODAY``."""
    return TODAY - timedelta(days=days)


# --- builders ----------------------------------------------------------------------------------


def item(
    identifier: str,
    title: str = "Resolución sobre vivienda",
    *,
    section: str = "1",
    department: tuple[str, str] | None = ("7723", "JEFATURA DEL ESTADO"),
) -> GazetteItem:
    """A gazette item with a PDF and an HTML link."""
    return GazetteItem(
        identifier=identifier,
        title=title,
        section_code=section,
        section_name=f"Sección {section}",
        department_code=department[0] if department else None,
        department_name=department[1] if department else None,
        heading=None,
        issue_number="1",
        first_page=1,
        last_page=2,
        pdf_url=f"https://www.boe.es/pdfs/{identifier}.pdf",
        html_url=f"https://www.boe.es/txt/{identifier}",
    )


def summary(day: date, *items: GazetteItem) -> DailySummary:
    """A one-issue summary of ``day``."""
    return DailySummary(
        day=day, issues=(GazetteIssue(number="1", identifier="BOE-S-2026-1"),), items=items
    )


def ref(law_id: str, code: str = "270", relation: str = "SE MODIFICA", text: str = "art. 1") -> Any:
    """A reference from ``law_id`` (the norm acting) with a relation code."""
    return LawReference(law_id=law_id, relation=CodedValue(code=code, text=relation), text=text)


def key(reference: LawReference) -> str:
    """The key under which a reference is remembered in ``seen``."""
    return f"{reference.law_id}|{reference.relation.code}|{reference.text}"


def later(*refs: LawReference) -> LawAnalysis:
    """An analysis whose laws acting on it are ``refs``."""
    return LawAnalysis(later=list(refs))


def metadata(
    law_id: str = LAW,
    *,
    title: str = "Ley del Procedimiento Administrativo Común",
    rank: str | None = "Ley",
    number: str | None = "39/2015",
) -> LawMetadata:
    """The metadata of a law."""
    return LawMetadata(
        identifier=law_id,
        title=title,
        rank=CodedValue(code="1300", text=rank) if rank else None,
        official_number=number,
    )


class FakeBoe:
    """Stands in for the BOE client: summaries, analyses and metadata, with a request log."""

    def __init__(
        self,
        *,
        days: dict[date, DailySummary | Exception | None] | None = None,
        analyses: dict[str, LawAnalysis | Exception] | None = None,
        metadata: dict[str, LawMetadata | Exception] | None = None,
        seconds_each: float = 0.0,
    ) -> None:
        self.days = days if days is not None else {}
        self.analyses = analyses if analyses is not None else {}
        self.metadata = metadata if metadata is not None else {}
        self.cached_days: set[date] = set()
        self.cached_analyses: set[str] = set()
        self.log: list[tuple[str, Any]] = []
        self.now = 1000.0
        self.seconds_each = seconds_each

    def clock(self) -> float:
        return self.now

    @property
    def requested_days(self) -> list[date]:
        return [what for kind, what in self.log if kind == "day"]

    @property
    def requested_analyses(self) -> list[str]:
        return [what for kind, what in self.log if kind == "analysis"]

    async def get_law_metadata(self, law_id: str) -> LawMetadata:
        self.log.append(("metadata", law_id))
        self.now += self.seconds_each
        outcome = self.metadata[law_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def has_cached_analysis(self, law_id: str) -> bool:
        return law_id in self.cached_analyses

    async def get_law_analysis(self, law_id: str) -> LawAnalysis:
        if law_id not in self.cached_analyses:
            self.log.append(("analysis", law_id))
            self.now += self.seconds_each
        outcome = self.analyses[law_id]
        if isinstance(outcome, Exception):
            raise outcome
        self.cached_analyses.add(law_id)
        return outcome

    async def has_cached_summary(self, day: date, *, today: date) -> bool:
        return day in self.cached_days

    async def get_daily_summary(self, day: date, *, today: date) -> DailySummary | None:
        assert day <= today, "a day after today was requested"
        if day not in self.cached_days:
            self.log.append(("day", day))
            self.now += self.seconds_each
        outcome = self.days.get(day)
        if isinstance(outcome, Exception):
            raise outcome
        # Like the real client, today's absence is not remembered (it may just be early).
        if outcome is not None or day != today:
            self.cached_days.add(day)
        return outcome


@pytest.fixture
async def store() -> AsyncIterator[SavedSearchStore]:
    """A real in-memory store."""
    opened = SavedSearchStore(None)
    yield opened
    await opened.close()


async def seed_gazette(
    store: SavedSearchStore,
    name: str = "vivienda",
    *,
    checked_through: date = TODAY,
    words: str | None = "vivienda",
    section: str | None = None,
    department_code: str | None = None,
    minutes: int = 0,
    owner: str = OWNER,
) -> SavedSearch:
    """Store a gazette search whose bookmark is ``checked_through``."""
    search = SavedSearch(
        owner=owner,
        name=name,
        kind="gazette",
        words=words,
        section=section,
        department_code=department_code,
        checked_through=checked_through,
        created_at=NOW - timedelta(days=60) + timedelta(minutes=minutes),
    )
    await store.add(search)
    return search


async def seed_law(
    store: SavedSearchStore,
    name: str = "ley-39-2015",
    law_id: str = LAW,
    *,
    seen: frozenset[str] = frozenset(),
    minutes: int = 0,
    owner: str = OWNER,
) -> SavedSearch:
    """Store a law search with the given ``seen`` set."""
    search = SavedSearch(
        owner=owner,
        name=name,
        kind="law",
        law_id=law_id,
        law_title="Ley 39/2015",
        seen=seen,
        created_at=NOW - timedelta(days=60) + timedelta(minutes=minutes),
    )
    await store.add(search)
    return search


async def save(store: SavedSearchStore, fake: FakeBoe, **kwargs: Any) -> Any:
    """``save_search`` as the local owner at the fixed clock."""
    options: dict[str, Any] = {"now": NOW, "today": TODAY}
    options.update(kwargs)
    return await saved_searches.save_search(store, fake, OWNER, **options)


async def check(store: SavedSearchStore, fake: FakeBoe, **kwargs: Any) -> Any:
    """``check_saved_searches`` as the local owner at the fixed clock."""
    options: dict[str, Any] = {"now": NOW, "today": TODAY, "clock": fake.clock}
    options.update(kwargs)
    return await saved_searches.check_saved_searches(store, fake, OWNER, **options)


def ids(search_check: Any) -> list[str]:
    """The identifiers of the items of one search's check."""
    return [entry.identifier for entry in search_check.items]


def change_ids(search_check: Any) -> list[str]:
    """The identifiers of the norms of the changes of one law search's check."""
    return [change.law_id for change in search_check.changes]


# --- constants ---------------------------------------------------------------------------------


def test_constants() -> None:
    assert saved_searches.MAX_SEARCHES == 20
    assert saved_searches.DEFAULT_CHECK_DAYS == 31
    assert saved_searches.MAX_SINCE_DAYS == 90
    assert saved_searches.CHECK_TIME_LIMIT == 40.0
    assert saved_searches.DEFAULT_ITEMS == 20
    assert saved_searches.MAX_ITEMS == 50
    assert set(saved_searches.REPORTED_KINDS) == {
        "modifies",
        "repeals",
        "adds",
        "suspends",
        "corrects",
    }


# --- 2.1 save_search: kinds ---------------------------------------------------------------------

KIND_ERROR = "give either law_id, or at least one of words, section or department_code"


async def test_saving_nothing_is_refused(store: SavedSearchStore) -> None:
    with pytest.raises(ValueError, match=KIND_ERROR):
        await save(store, FakeBoe())

    assert await store.list(OWNER) == []


@pytest.mark.parametrize(
    "extra", [{"words": "vivienda"}, {"section": "1"}, {"department_code": "7723"}]
)
async def test_a_law_id_together_with_a_gazette_filter_is_refused(
    store: SavedSearchStore, extra: dict[str, str]
) -> None:
    fake = FakeBoe(metadata={LAW: metadata()}, analyses={LAW: later()})

    with pytest.raises(ValueError, match=KIND_ERROR):
        await save(store, fake, law_id=LAW, **extra)

    assert await store.list(OWNER) == []


async def test_blank_values_alone_are_nothing(store: SavedSearchStore) -> None:
    with pytest.raises(ValueError, match=KIND_ERROR):
        await save(store, FakeBoe(), words="  ", section="", department_code=" ")


# --- 2.1 save_search: gazette -------------------------------------------------------------------


async def test_a_gazette_search_reads_only_todays_issue_when_saved(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe()

    result = await save(store, fake, words="Vivienda subvenciones")

    assert fake.requested_days == [TODAY]
    assert fake.log == [("day", TODAY)]
    assert result.search.name == "vivienda-subvenciones"
    assert result.search.kind == "gazette"
    assert result.search.words == "Vivienda subvenciones"
    assert result.name_was_proposed is True
    assert result.total == 1
    stored = await store.get(OWNER, "vivienda-subvenciones")
    assert stored.kind == "gazette"
    assert stored.checked_through == ago(1)  # the baseline is yesterday; see test_..._bookmark
    assert stored.created_at == NOW
    assert stored.last_checked_at is None
    assert stored.law_id is None
    assert stored.seen == frozenset()


async def test_the_note_of_a_proposed_gazette_name(store: SavedSearchStore) -> None:
    result = await save(store, FakeBoe(), words="vivienda")

    assert result.note == (
        "Saved as 'vivienda'; rename it with edit_saved_search if you prefer another name. "
        "The first check reports what is published from now on."
        " Stored in memory only: it is lost when the server stops."
    )


async def test_the_bookmark_of_a_gazette_search_is_the_day_before_the_madrid_day_given(
    store: SavedSearchStore,
) -> None:
    await save(store, FakeBoe(), words="vivienda", today=ago(3))

    assert (await store.get(OWNER, "vivienda")).checked_through == ago(4)


async def test_gazette_values_are_normalised(store: SavedSearchStore) -> None:
    result = await save(store, FakeBoe(), section=" 2a ", words="  ", department_code="8161")

    stored = await store.get(OWNER, result.search.name)
    assert stored.section == "2A"
    assert stored.words is None
    assert stored.department_code == "8161"
    assert result.search.section == "2A"
    assert result.name_was_proposed is True


async def test_words_without_letters_or_digits_are_refused(store: SavedSearchStore) -> None:
    with pytest.raises(ValueError, match="words must contain letters or digits"):
        await save(store, FakeBoe(), words="¿?!", section="1")

    assert await store.list(OWNER) == []


@pytest.mark.parametrize(
    ("filters", "proposed"),
    [
        ({"words": "vivienda", "section": "3"}, "vivienda-seccion-3"),
        ({"department_code": "8161"}, "dept-8161"),
        (
            {"words": "vivienda", "section": "3", "department_code": "8161"},
            "vivienda-seccion-3-dept-8161",
        ),
    ],
)
async def test_proposed_names_come_from_the_filters(
    store: SavedSearchStore, filters: dict[str, str], proposed: str | None
) -> None:
    result = await save(store, FakeBoe(), **filters)

    assert result.search.name == proposed
    assert (await store.names(OWNER)) == {result.search.name}


async def test_a_proposed_name_gets_a_suffix_when_taken(store: SavedSearchStore) -> None:
    fake = FakeBoe()
    first = await save(store, fake, words="vivienda")
    second = await save(store, fake, words="vivienda")
    third = await save(store, fake, words="Vivienda")

    assert [first.search.name, second.search.name, third.search.name] == [
        "vivienda",
        "vivienda-2",
        "vivienda-3",
    ]
    assert third.total == 3


async def test_the_suffix_only_looks_at_the_owners_own_names(store: SavedSearchStore) -> None:
    await seed_gazette(store, "vivienda", owner=OTHER)

    result = await save(store, FakeBoe(), words="vivienda")

    assert result.search.name == "vivienda"
    assert result.total == 1


# --- 2.1 save_search: law -----------------------------------------------------------------------


async def test_a_law_search_takes_its_baseline_from_all_current_references(
    store: SavedSearchStore,
) -> None:
    refs = [
        ref("BOE-A-2016-1", "270"),
        ref("BOE-A-2017-2", "210", "SE DEROGA", "art. 5"),
        ref("BOE-A-2018-3", "330", "SE CITA", "disp. final"),
        ref("BOE-A-2019-4", "470", "SENTENCIA", "STC 1/2019"),
    ]
    fake = FakeBoe(metadata={LAW: metadata()}, analyses={LAW: later(*refs)})

    result = await save(store, fake, law_id=LAW)

    assert result.search.kind == "law"
    assert result.search.law_id == LAW
    assert result.search.law_title == "Ley del Procedimiento Administrativo Común"
    assert result.search.name == "ley-39-2015"
    assert result.name_was_proposed is True
    assert result.note == (
        "Saved as 'ley-39-2015'; rename it with edit_saved_search if you prefer another name. "
        "The first check reports changes recorded from now on "
        "(the BOE data read can be up to 6 hours old)."
        " Stored in memory only: it is lost when the server stops."
    )
    stored = await store.get(OWNER, "ley-39-2015")
    assert stored.seen == frozenset(key(r) for r in refs)
    assert stored.checked_through is None
    assert stored.words is None
    assert stored.created_at == NOW
    assert stored.last_checked_at is None


async def test_the_baseline_ignores_the_laws_this_law_acts_on(store: SavedSearchStore) -> None:
    analysis = LawAnalysis(
        earlier=[ref("BOE-A-1990-1", "270", "MODIFICA")], later=[ref("BOE-A-2016-1")]
    )
    fake = FakeBoe(metadata={LAW: metadata()}, analyses={LAW: analysis})

    await save(store, fake, law_id=LAW)

    assert (await store.get(OWNER, "ley-39-2015")).seen == frozenset({key(analysis.later[0])})


async def test_the_law_identifier_is_normalised(store: SavedSearchStore) -> None:
    fake = FakeBoe(metadata={LAW: metadata()}, analyses={LAW: later()})

    result = await save(store, fake, law_id="  boe-a-2015-10566 ")

    assert result.search.law_id == LAW
    assert ("metadata", LAW) in fake.log
    assert (await store.get(OWNER, result.search.name)).law_id == "BOE-A-2015-10566"


async def test_a_law_without_rank_or_number_is_named_after_its_identifier(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe(metadata={LAW: metadata(rank=None, number=None)}, analyses={LAW: later()})

    result = await save(store, fake, law_id=LAW)

    assert result.search.name == "boe-a-2015-10566"


async def test_an_unknown_law_propagates_and_stores_nothing(store: SavedSearchStore) -> None:
    fake = FakeBoe(metadata={LAW: BoeNotFoundError("no such law", status=404)})

    with pytest.raises(BoeNotFoundError):
        await save(store, fake, law_id=LAW)

    assert await store.list(OWNER) == []


async def test_a_very_long_law_title_is_cut_in_the_result(store: SavedSearchStore) -> None:
    title = "Ley " + "muy larga " * 60
    fake = FakeBoe(metadata={LAW: metadata(title=title)}, analyses={LAW: later()})

    result = await save(store, fake, law_id=LAW)

    assert result.search.law_title is not None
    assert len(result.search.law_title) <= 201  # 200 plus an ellipsis
    assert result.search.law_title.startswith("Ley muy larga muy larga")


# --- 2.1 save_search: names and the limit -------------------------------------------------------


async def test_a_given_name_is_slugified_and_not_proposed(store: SavedSearchStore) -> None:
    result = await save(store, FakeBoe(), words="vivienda", name="Mi Búsqueda Nº 1!")

    assert result.search.name == "mi-busqueda-n-1"
    assert result.name_was_proposed is False
    assert await store.names(OWNER) == {"mi-busqueda-n-1"}


async def test_a_given_name_without_letters_or_digits_is_refused(
    store: SavedSearchStore,
) -> None:
    with pytest.raises(ValueError, match="name must contain letters or digits"):
        await save(store, FakeBoe(), words="vivienda", name="¡¡!!")

    assert await store.list(OWNER) == []


async def test_a_given_name_already_used_is_refused(store: SavedSearchStore) -> None:
    await seed_gazette(store, "mi-busqueda")

    with pytest.raises(
        ValueError,
        match="a saved search named 'mi-busqueda' already exists; choose another name",
    ):
        await save(store, FakeBoe(), words="otra", name="Mi Búsqueda")

    assert len(await store.list(OWNER)) == 1


async def test_a_given_name_may_be_used_by_another_owner(store: SavedSearchStore) -> None:
    await seed_gazette(store, "mi-busqueda", owner=OTHER)

    result = await save(store, FakeBoe(), words="otra", name="mi-busqueda")

    assert result.search.name == "mi-busqueda"


async def test_the_twenty_first_search_is_refused(store: SavedSearchStore) -> None:
    for number in range(20):
        await seed_gazette(store, f"s{number}", minutes=number)

    with pytest.raises(
        ValueError, match="the limit of 20 saved searches is reached; delete one first"
    ):
        await save(store, FakeBoe(), words="vivienda")

    assert len(await store.list(OWNER)) == 20


async def test_the_limit_counts_each_owner_apart(store: SavedSearchStore) -> None:
    for number in range(20):
        await seed_gazette(store, f"s{number}", minutes=number, owner=OTHER)

    result = await save(store, FakeBoe(), words="vivienda")

    assert result.total == 1


# --- 2.2 list_saved_searches --------------------------------------------------------------------


async def test_listing_without_searches(store: SavedSearchStore) -> None:
    result = await saved_searches.list_saved_searches(store, OWNER, now=NOW)

    assert result.searches == []
    assert result.total == 0
    assert result.limit == 20
    assert result.note == NO_SEARCHES
    assert result.provenance.generated_at == NOW
    assert result.provenance.notice == Provenance(generated_at=NOW).notice


async def test_listing_is_oldest_first_and_describes_each_search(
    store: SavedSearchStore,
) -> None:
    await seed_law(store, "ley-b", minutes=5)
    await seed_gazette(store, "vivienda", section="3", minutes=1)

    result = await saved_searches.list_saved_searches(store, OWNER, now=NOW)

    assert [s.name for s in result.searches] == ["vivienda", "ley-b"]
    assert result.total == 2
    assert result.note is None
    gazette_info, law_info = result.searches
    assert (gazette_info.kind, gazette_info.words, gazette_info.section) == (
        "gazette",
        "vivienda",
        "3",
    )
    assert gazette_info.checked_through == TODAY
    assert gazette_info.last_checked_at is None
    assert (law_info.kind, law_info.law_id, law_info.law_title) == ("law", LAW, "Ley 39/2015")


async def test_listing_omits_what_a_search_does_not_have(store: SavedSearchStore) -> None:
    await seed_gazette(store, "vivienda", section=None, minutes=1)
    await seed_law(store, "ley-b", minutes=5)

    result = await saved_searches.list_saved_searches(store, OWNER, now=NOW)

    gazette_json, law_json = (info.model_dump(mode="json") for info in result.searches)
    assert "law_id" not in gazette_json
    assert "law_title" not in gazette_json
    assert "section" not in gazette_json
    assert "department_code" not in gazette_json
    assert gazette_json["words"] == "vivienda"
    assert "words" not in law_json
    assert "checked_through" not in law_json
    assert law_json["law_id"] == LAW
    # "Never checked" is a null that stays visible.
    assert "last_checked_at" in gazette_json
    assert gazette_json["last_checked_at"] is None


async def test_listing_shows_only_the_owners_searches(store: SavedSearchStore) -> None:
    await seed_gazette(store, "mia")
    await seed_gazette(store, "ajena", owner=OTHER)

    result = await saved_searches.list_saved_searches(store, OWNER, now=NOW)

    assert [s.name for s in result.searches] == ["mia"]
    assert result.total == 1


# --- 2.3 check_saved_searches: general ----------------------------------------------------------


async def test_checking_without_searches(store: SavedSearchStore) -> None:
    fake = FakeBoe()

    result = await check(store, fake)

    assert result.checked == 0
    assert result.total == 0
    assert result.complete is True
    assert result.results == []
    assert result.note == NO_SEARCHES
    assert result.provenance.data_updated_at is None
    assert fake.log == []


async def test_checking_an_unknown_name(store: SavedSearchStore) -> None:
    await seed_gazette(store, "vivienda")

    with pytest.raises(
        ValueError,
        match="no saved search named 'otra-cosa'; list_saved_searches shows the names",
    ):
        await check(store, FakeBoe(), name="Otra Cosa")


async def test_checking_one_search_by_name(store: SavedSearchStore) -> None:
    await seed_gazette(store, "uno", checked_through=ago(2))
    await seed_gazette(store, "dos", checked_through=ago(2), minutes=1)
    fake = FakeBoe(days={ago(1): summary(ago(1), item("A1"))})

    result = await check(store, fake, name="DOS")

    assert result.total == 1
    assert [r.name for r in result.results] == ["dos"]
    assert (await store.get(OWNER, "uno")).checked_through == ago(2)
    assert (await store.get(OWNER, "dos")).checked_through == ago(1)


async def test_another_owners_searches_are_neither_checked_nor_reachable(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, "ajena", checked_through=ago(2), owner=OTHER)
    fake = FakeBoe()

    result = await check(store, fake)

    assert result.total == 0
    assert fake.log == []
    with pytest.raises(ValueError, match="no saved search named 'ajena'"):
        await check(store, fake, name="ajena")
    assert (await store.get(OTHER, "ajena")).checked_through == ago(2)


async def test_results_follow_the_list_order_but_laws_are_read_first(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, "primero", checked_through=ago(2), minutes=0)
    await seed_law(store, "segundo", minutes=1)
    fake = FakeBoe(
        days={ago(1): summary(ago(1), item("A1"))},
        analyses={LAW: later(ref("BOE-A-2020-1"))},
    )

    result = await check(store, fake)

    assert [(r.name, r.kind) for r in result.results] == [
        ("primero", "gazette"),
        ("segundo", "law"),
    ]
    assert fake.log[0] == ("analysis", LAW)
    assert [kind for kind, _ in fake.log] == ["analysis", "day", "day"]


async def test_the_provenance_of_a_check_that_read_the_gazette(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(3))
    fake = FakeBoe(
        days={
            ago(2): summary(ago(2), item("A1")),
            ago(1): summary(ago(1), item("A2")),
        }
    )

    result = await check(store, fake)

    assert result.provenance.generated_at == NOW
    assert result.provenance.notice == GAZETTE_NOTICE
    assert result.provenance.data_updated_at == datetime(
        ago(1).year, ago(1).month, ago(1).day, tzinfo=UTC
    )


async def test_the_provenance_of_a_check_that_read_no_issue_has_no_data_date(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(2))

    result = await check(store, FakeBoe())

    assert result.provenance.data_updated_at is None


# --- 2.3 law searches ---------------------------------------------------------------------------

# One reference per relation code of the spec: three norms that change the text (modifies,
# repeals, adds), a suspension and a correction, then a court ruling and a citation.
MODIFIES = ref("BOE-A-2021-10", "270", "SE MODIFICA", "art. 1")
REPEALS = ref("BOE-A-2021-11", "210", "SE DEROGA", "art. 2")
ADDS = ref("BOE-A-2021-12", "407", "SE AÑADE", "art. 3")
SUSPENDS = ref("BOE-A-2021-13", "231", "SE SUSPENDE", "art. 4")
CORRECTS = ref("BOE-A-2021-14", "201", "SE CORRIGE", "art. 5")
JUDICIAL = ref("BOE-A-2021-15", "470", "SENTENCIA", "STC 7/2021")
CITES = ref("BOE-A-2021-16", "330", "SE CITA", "art. 6")
ALL_REFS = [MODIFIES, REPEALS, ADDS, SUSPENDS, CORRECTS, JUDICIAL, CITES]


async def test_new_references_split_into_changes_and_other_new(store: SavedSearchStore) -> None:
    await seed_law(store, seen=frozenset({key(MODIFIES)}))
    fake = FakeBoe(analyses={LAW: later(*ALL_REFS)})

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.kind == "law"
    assert law_check.status == "checked"
    assert law_check.law_id == LAW
    assert law_check.law_title == "Ley 39/2015"
    assert [(c.law_id, c.kind, c.relation, c.text) for c in law_check.changes] == [
        ("BOE-A-2021-11", "repeals", "SE DEROGA", "art. 2"),
        ("BOE-A-2021-12", "adds", "SE AÑADE", "art. 3"),
        ("BOE-A-2021-13", "suspends", "SE SUSPENDE", "art. 4"),
        ("BOE-A-2021-14", "corrects", "SE CORRIGE", "art. 5"),
    ]
    assert law_check.changes_total == 4
    assert law_check.other_new == {"court_rulings": 1, "citations": 1}
    assert law_check.note == (
        "1 new court ruling on this law is not listed; explore_amendment_chain with "
        "include='all' shows them."
    )
    assert result.checked == 1
    assert result.complete is True
    assert result.note is None


async def test_a_modification_is_reported_with_its_kind(store: SavedSearchStore) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES)})

    result = await check(store, fake)

    assert [(c.law_id, c.kind) for c in result.results[0].changes] == [
        ("BOE-A-2021-10", "modifies")
    ]


async def test_a_check_marks_everything_seen_and_stamps_the_time(
    store: SavedSearchStore,
) -> None:
    seed = await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(*ALL_REFS)})

    await check(store, fake)

    stored = await store.get(OWNER, seed.name)
    assert stored.seen == frozenset(key(r) for r in ALL_REFS)
    assert stored.last_checked_at == NOW
    assert stored.created_at == seed.created_at


async def test_a_second_check_reports_nothing(store: SavedSearchStore) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(*ALL_REFS)})
    await check(store, fake)

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.status == "checked"
    assert law_check.changes == []
    assert law_check.changes_total == 0
    assert law_check.other_new is None


async def test_only_references_new_since_the_last_check_are_reported(
    store: SavedSearchStore,
) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES)})
    await check(store, fake)
    newcomer = ref("BOE-A-2022-1", "210", "SE DEROGA", "art. 9")
    fake.analyses[LAW] = later(MODIFIES, newcomer)

    result = await check(store, fake)

    assert change_ids(result.results[0]) == ["BOE-A-2022-1"]


async def test_seen_only_grows(store: SavedSearchStore) -> None:
    # A reference that vanished from the analysis stays remembered, so it is never reported as new
    # if it comes back.
    gone = "BOE-A-1999-1|270|art. 1"
    await seed_law(store, seen=frozenset({gone}))
    fake = FakeBoe(analyses={LAW: later(MODIFIES)})

    await check(store, fake)

    assert (await store.get(OWNER, "ley-39-2015")).seen == frozenset({gone, key(MODIFIES)})


async def test_the_same_norm_with_another_relation_or_text_is_a_new_reference(
    store: SavedSearchStore,
) -> None:
    await seed_law(store, seen=frozenset({key(MODIFIES)}))
    again = ref(MODIFIES.law_id, "270", "SE MODIFICA", "art. 99")
    other_relation = ref(MODIFIES.law_id, "210", "SE DEROGA", "art. 1")
    fake = FakeBoe(analyses={LAW: later(MODIFIES, again, other_relation)})

    result = await check(store, fake)

    assert [(c.kind, c.text) for c in result.results[0].changes] == [
        ("modifies", "art. 99"),
        ("repeals", "art. 1"),
    ]


async def test_a_baseline_taken_by_saving_leaves_nothing_to_report(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe(metadata={LAW: metadata()}, analyses={LAW: later(*ALL_REFS)})
    await save(store, fake, law_id=LAW)

    first = await check(store, fake)
    fake.analyses[LAW] = later(*ALL_REFS, ref("BOE-A-2023-1", "407", "SE AÑADE", "art. 8"))
    second = await check(store, fake)

    assert first.results[0].changes == []
    assert first.results[0].other_new is None
    assert change_ids(second.results[0]) == ["BOE-A-2023-1"]


async def test_limit_bounds_the_changes_shown_but_not_the_total(
    store: SavedSearchStore,
) -> None:
    # Clarification S2: the changes cut by the limit are not lost; only the shown ones are marked
    # seen and the search is partial, so a second call brings the rest.
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES, REPEALS, ADDS, SUSPENDS, CORRECTS)})

    result = await check(store, fake, limit=2)

    law_check = result.results[0]
    assert change_ids(law_check) == [MODIFIES.law_id, REPEALS.law_id]
    assert law_check.changes_total == 5
    assert law_check.status == "partial"
    assert law_check.note == "Showing 2 of 5 changes; call again for the rest."
    assert result.checked == 0
    assert result.complete is False
    assert result.note == "0 of 1 saved searches fully checked. Call again to continue."


async def test_changes_cut_by_the_limit_stay_new_until_shown(store: SavedSearchStore) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES, REPEALS, ADDS, SUSPENDS, CORRECTS)})

    first = await check(store, fake, limit=2)
    seen_after_first = (await store.get(OWNER, "ley-39-2015")).seen
    second = await check(store, fake, limit=2)
    third = await check(store, fake, limit=2)
    fourth = await check(store, fake, limit=2)

    assert seen_after_first == frozenset({key(MODIFIES), key(REPEALS)})
    assert first.results[0].status == "partial"
    assert change_ids(second.results[0]) == [ADDS.law_id, SUSPENDS.law_id]
    assert second.results[0].changes_total == 3
    assert second.results[0].status == "partial"
    assert change_ids(third.results[0]) == [CORRECTS.law_id]
    assert third.results[0].changes_total == 1
    assert third.results[0].status == "checked"
    assert third.results[0].note is None
    assert third.complete is True
    assert fourth.results[0].changes == []


async def test_a_cut_check_still_marks_the_unreported_references_seen(
    store: SavedSearchStore,
) -> None:
    # Court rulings and citations are only counted, so they are consumed on the first call.
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES, REPEALS, JUDICIAL, CITES)})

    first = await check(store, fake, limit=1)
    second = await check(store, fake, limit=1)

    assert first.results[0].other_new == {"court_rulings": 1, "citations": 1}
    assert second.results[0].other_new is None
    assert change_ids(second.results[0]) == [REPEALS.law_id]
    assert second.results[0].status == "checked"


async def test_exactly_limit_changes_are_not_cut(store: SavedSearchStore) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES, REPEALS)})

    result = await check(store, fake, limit=2)

    assert result.results[0].status == "checked"
    assert result.results[0].note is None
    assert result.complete is True


LAW_SINCE_NOTE = (
    "since applies to gazette searches; get_amendment_history lists a law's past amendments."
)


async def test_since_does_not_read_a_law_search_and_says_so(store: SavedSearchStore) -> None:
    refs = [ref(f"BOE-A-2026-{n}", "270", "SE MODIFICA", f"art. {n}") for n in range(1, 6)]
    seed = await seed_law(store, seen=frozenset())
    fake = FakeBoe(analyses={LAW: later(*refs)})

    result = await check(store, fake, since=ago(10), limit=2)

    law_check = result.results[0]
    assert fake.log == []
    assert law_check.changes == []
    assert law_check.changes_total == 0
    assert law_check.other_new is None
    assert law_check.status == "checked"
    assert law_check.note == LAW_SINCE_NOTE
    assert result.complete is True
    assert await store.get(OWNER, seed.name) == seed


async def test_a_long_reference_text_is_cut(store: SavedSearchStore) -> None:
    await seed_law(store)
    long_text = "artículo " * 80
    fake = FakeBoe(analyses={LAW: later(ref("BOE-A-2021-10", text=long_text))})

    result = await check(store, fake)

    text = result.results[0].changes[0].text
    assert len(text) <= 201
    assert text.startswith("artículo artículo")


# since, for a law


async def test_since_changes_nothing_stored_for_a_law(store: SavedSearchStore) -> None:
    seed = await seed_law(store, seen=frozenset())
    fake = FakeBoe(analyses={LAW: later(MODIFIES)})

    await check(store, fake, since=ago(10))

    assert await store.get(OWNER, seed.name) == seed


# law failures and deadline


async def test_a_law_the_boe_no_longer_has_fails(store: SavedSearchStore) -> None:
    seed = await seed_law(store)
    fake = FakeBoe(analyses={LAW: BoeNotFoundError("gone", status=404)})

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.status == "failed"
    assert law_check.note == "The BOE no longer has this law."
    assert result.checked == 0
    # Calling again cannot bring the law back, so a failed search does not make a call "again".
    assert result.complete is True
    assert result.note == "1 saved search could not be checked; see its note."
    assert await store.get(OWNER, seed.name) == seed


async def test_a_retryable_failure_of_a_law_is_failed_and_says_to_try_later(
    store: SavedSearchStore,
) -> None:
    seed = await seed_law(store)
    fake = FakeBoe(analyses={LAW: BoeUnavailableError("down", status=503)})

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.status == "failed"
    assert law_check.note == "Could not be read; try again later."
    # Calling again at once cannot help, so the model is not asked to.
    assert result.complete is True
    assert result.note == "1 saved search could not be checked; see its note."
    assert await store.get(OWNER, seed.name) == seed


async def test_another_failure_of_a_law_is_failed_without_the_apis_words(
    store: SavedSearchStore,
) -> None:
    await seed_law(store)
    error = BoeResponseError("unreadable body", api_message="mensaje secreto del API")
    fake = FakeBoe(analyses={LAW: error})

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.status == "failed"
    assert law_check.note == "Could not be read."
    assert "secreto" not in (law_check.note or "")
    assert "unreadable" not in (law_check.note or "")


async def test_one_failing_law_does_not_stop_the_others(store: SavedSearchStore) -> None:
    other = "BOE-A-2000-7"
    await seed_law(store, "uno", LAW, minutes=0)
    await seed_law(store, "dos", other, minutes=1)
    fake = FakeBoe(analyses={LAW: BoeNotFoundError("gone", status=404), other: later(MODIFIES)})

    result = await check(store, fake)

    assert [r.status for r in result.results] == ["failed", "checked"]
    assert change_ids(result.results[1]) == ["BOE-A-2021-10"]
    assert result.checked == 1
    assert result.total == 2
    assert result.complete is True
    assert result.note == "1 saved search could not be checked; see its note."


async def test_an_uncached_law_is_not_requested_after_the_deadline(
    store: SavedSearchStore,
) -> None:
    first, second, third = "BOE-A-2000-1", "BOE-A-2000-2", "BOE-A-2000-3"
    await seed_law(store, "uno", first, minutes=0)
    await seed_law(store, "dos", second, minutes=1)
    await seed_law(store, "tres", third, minutes=2)
    fake = FakeBoe(
        analyses={first: later(MODIFIES), second: later(REPEALS), third: later(ADDS)},
        seconds_each=6.0,
    )
    fake.cached_analyses.add(third)

    # Deadline: 10 s minus the 5 s margin. The first request ends at 6 s, past it.
    result = await check(store, fake, time_limit=10.0)

    assert fake.requested_analyses == [first]
    assert [r.status for r in result.results] == ["checked", "partial", "checked"]
    assert change_ids(result.results[2]) == ["BOE-A-2021-12"]  # cached: read after the deadline
    assert result.checked == 2
    assert result.complete is False
    assert result.note == "2 of 3 saved searches fully checked. Call again to continue."
    untouched = await store.get(OWNER, "dos")
    assert untouched.seen == frozenset()
    assert untouched.last_checked_at is None


# --- 2.3 gazette searches -----------------------------------------------------------------------


async def test_a_save_then_a_check_reports_only_what_came_after(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe(
        days={
            ago(2): summary(ago(2), item("DURING-SAVE-DAY", "Ley de vivienda")),
            ago(1): summary(ago(1), item("NEW", "Real Decreto de vivienda")),
        }
    )
    await save(store, fake, words="vivienda", today=ago(2))
    assert fake.requested_days == [ago(2)]
    fake.log.clear()

    result = await check(store, fake)

    assert ids(result.results[0]) == ["NEW"]
    # The issue of the day of saving is read again (it can grow); it was cached by the save.
    assert fake.requested_days == [ago(1), TODAY]


async def test_a_gazette_check_reports_matches_and_moves_the_bookmark(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(3))
    fake = FakeBoe(
        days={
            ago(2): summary(ago(2), item("A1"), item("A2", "Otra cosa"), item("A3")),
            ago(1): summary(ago(1), item("B1")),
        }
    )

    result = await check(store, fake)

    gazette_check = result.results[0]
    assert gazette_check.kind == "gazette"
    assert gazette_check.status == "checked"
    assert gazette_check.from_date == ago(2)
    assert gazette_check.to_date == TODAY  # today answered "no issue": read, not finished
    assert gazette_check.matched == 3
    assert gazette_check.has_more is False
    assert gazette_check.days_failed == 0
    assert gazette_check.days_pending == 0
    # Oldest day first, document order within a day.
    assert ids(gazette_check) == ["A1", "A3", "B1"]
    assert [entry.date for entry in gazette_check.items] == [ago(2), ago(2), ago(1)]
    first = gazette_check.items[2]
    assert first.title == "Resolución sobre vivienda"
    assert first.section == "1"
    assert first.url == "https://www.boe.es/txt/B1"
    stored = await store.get(OWNER, "vivienda")
    assert stored.checked_through == ago(1)
    assert stored.last_checked_at == NOW
    assert result.checked == 1
    assert result.complete is True


async def test_the_filters_all_apply(store: SavedSearchStore) -> None:
    await seed_gazette(
        store,
        words="vivienda",
        section="3",
        department_code="8161",
        checked_through=ago(2),
    )
    fake = FakeBoe(
        days={
            ago(1): summary(
                ago(1),
                item("OK", section="3", department=("8161", "X")),
                item("WRONG-SECTION", section="1", department=("8161", "X")),
                item("WRONG-DEPT", section="3", department=("7723", "Y")),
                item("WRONG-WORDS", "Otra cosa", section="3", department=("8161", "X")),
            )
        }
    )

    result = await check(store, fake)

    assert ids(result.results[0]) == ["OK"]
    assert result.results[0].matched == 1


async def test_a_second_check_reads_only_what_came_after(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(days={ago(1): summary(ago(1), item("A1"))})
    await check(store, fake)
    fake.log.clear()

    second = await check(store, fake)

    assert fake.requested_days == [TODAY]  # yesterday is cached and behind the bookmark
    assert second.results[0].matched == 0


async def test_today_without_an_issue_is_read_again_and_never_moves_the_bookmark(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(days={ago(1): summary(ago(1), item("A1"))})  # nothing for today yet

    result = await check(store, fake)

    gazette_check = result.results[0]
    assert gazette_check.status == "checked"  # today answered "no issue": read, nothing pending
    assert gazette_check.to_date == TODAY
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(1)
    assert result.complete is True
    assert result.note == NO_TODAY_ISSUE

    # Later today the issue appears and the next check reads it.
    fake.days[TODAY] = summary(TODAY, item("LATE"))
    fake.log.clear()
    second = await check(store, fake)

    assert ids(second.results[0]) == ["LATE"]
    # Today is never finished (its issue may grow), so the bookmark stays and LATE is remembered.
    stored = await store.get(OWNER, "vivienda")
    assert stored.checked_through == ago(1)
    assert stored.seen_items == {TODAY: frozenset({"LATE"})}
    assert second.note is None


async def test_a_past_day_without_an_issue_counts_as_read(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(8))
    days: dict[date, Any] = {ago(n): summary(ago(n)) for n in (5, 4, 3, 2)}
    days |= {
        ago(7): summary(ago(7), item("A1")),
        ago(1): summary(ago(1), item("A2")),
        TODAY: summary(TODAY, item("A3")),
    }  # ago(6) was a Sunday: no issue
    fake = FakeBoe(days=days)

    result = await check(store, fake)

    assert result.results[0].status == "checked"
    assert ids(result.results[0]) == ["A1", "A2", "A3"]
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(1)
    assert result.note is None


async def test_nothing_new_to_read_when_checked_through_today(store: SavedSearchStore) -> None:
    seed = await seed_gazette(store, checked_through=TODAY)
    fake = FakeBoe()

    result = await check(store, fake)

    gazette_check = result.results[0]
    assert gazette_check.status == "checked"
    assert gazette_check.matched == 0
    assert gazette_check.items == []
    assert gazette_check.note == "Already checked through 2026-11-21."
    assert fake.log == []
    assert (await store.get(OWNER, seed.name)).checked_through == TODAY


# the 31-day cap


async def test_a_long_gap_is_capped_to_the_last_31_days(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(40))
    fake = FakeBoe(days={ago(n): summary(ago(n)) for n in range(31)})

    result = await check(store, fake)

    gazette_check = result.results[0]
    assert gazette_check.note is not None
    assert (
        "Only the last 31 days were checked; 9 earlier days were skipped. "
        "Pass since to read up to 90 days back."
    ) in gazette_check.note
    assert fake.requested_days == [ago(30 - n) for n in range(30)] + [TODAY]
    assert gazette_check.from_date == ago(30)
    assert gazette_check.status == "checked"
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(1)


async def test_the_cap_applies_only_beyond_31_days(store: SavedSearchStore) -> None:
    await seed_gazette(store, "a", checked_through=ago(31))
    fake = FakeBoe()

    result = await check(store, fake)

    assert fake.requested_days[0] == ago(30)
    assert "Only the last 31 days" not in (result.results[0].note or "")


async def test_the_cap_is_one_day_beyond_the_boundary(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(32))
    fake = FakeBoe()

    result = await check(store, fake)

    assert fake.requested_days[0] == ago(30)
    assert "Only the last 31 days were checked; 1 earlier" in (result.results[0].note or "")


# several searches share the days


async def test_days_are_read_once_for_several_searches_oldest_first(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, "uno", words="vivienda", checked_through=ago(3), minutes=0)
    await seed_gazette(store, "dos", words="hacienda", checked_through=ago(5), minutes=1)
    fake = FakeBoe(
        days={
            ago(4): summary(ago(4), item("D4", "Ley de hacienda")),
            ago(3): summary(ago(3), item("D3", "Ley de vivienda y hacienda")),
            ago(2): summary(ago(2), item("D2", "Orden de vivienda")),
        }
    )

    result = await check(store, fake)

    assert fake.requested_days == [ago(4), ago(3), ago(2), ago(1), TODAY]
    uno, dos = result.results
    # "uno" starts after its own bookmark, so it does not see ago(3) or ago(4).
    assert ids(uno) == ["D2"]
    assert uno.from_date == ago(2)
    assert ids(dos) == ["D4", "D3"]  # oldest day first
    assert dos.from_date == ago(4)
    assert result.checked == 2


# deadline and failures


async def test_reading_stops_at_the_deadline_and_the_bookmark_follows_the_run(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(5))
    fake = FakeBoe(
        days={ago(4): summary(ago(4), item("A1")), ago(3): summary(ago(3), item("A2"))},
        seconds_each=6.0,
    )

    result = await check(store, fake, time_limit=10.0)

    assert fake.requested_days == [ago(4)]
    gazette_check = result.results[0]
    assert gazette_check.status == "partial"
    assert gazette_check.from_date == ago(4)
    assert gazette_check.to_date == ago(4)
    assert gazette_check.days_pending == 4  # ago(3), ago(2), ago(1) and today
    assert ids(gazette_check) == ["A1"]
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(4)
    assert result.checked == 0
    assert result.complete is False
    assert result.note == "0 of 1 saved searches fully checked. Call again to continue."


async def test_cached_days_are_still_read_after_the_deadline(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(5))
    fake = FakeBoe(
        days={
            ago(4): summary(ago(4), item("A1")),
            ago(3): summary(ago(3), item("A2")),
            ago(2): summary(ago(2), item("A3")),
        },
        seconds_each=6.0,
    )
    fake.cached_days.add(ago(3))

    result = await check(store, fake, time_limit=10.0)

    assert fake.requested_days == [ago(4)]  # ago(3) is free, ago(2) is uncached and too late
    assert result.results[0].to_date == ago(3)
    assert ids(result.results[0]) == ["A1", "A2"]
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(3)


async def test_a_retryable_failure_does_not_stop_the_reading(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(5))
    fake = FakeBoe(
        days={
            ago(4): summary(ago(4), item("A1")),
            ago(3): BoeUnavailableError("down", status=503),
            ago(2): summary(ago(2), item("A2")),
        }
    )

    result = await check(store, fake)

    assert fake.requested_days == [ago(4), ago(3), ago(2), ago(1), TODAY]
    gazette_check = result.results[0]
    assert gazette_check.status == "checked"
    assert gazette_check.days_failed == 0
    assert gazette_check.days_retrying == 1
    assert ids(gazette_check) == ["A1", "A2"]
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(4)
    assert result.complete is True


async def test_a_retryable_failure_on_the_only_day_reads_nothing_new(
    store: SavedSearchStore,
) -> None:
    seed = await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(days={ago(1): BoeUnavailableError("down", status=503)})

    result = await check(store, fake)

    gazette_check = result.results[0]
    assert gazette_check.status == "checked"
    assert gazette_check.days_retrying == 1
    assert gazette_check.matched == 0
    assert (await store.get(OWNER, seed.name)).checked_through == ago(2)


async def test_a_permanent_failure_leaves_the_day_to_be_retried_and_reading_goes_on(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(4))
    fake = FakeBoe(
        days={
            ago(3): summary(ago(3), item("A1")),
            ago(2): BoeResponseError("unreadable"),
            ago(1): summary(ago(1), item("A3")),
        }
    )

    result = await check(store, fake)

    assert fake.requested_days == [ago(3), ago(2), ago(1), TODAY]
    gazette_check = result.results[0]
    assert gazette_check.days_failed == 0
    assert gazette_check.days_retrying == 1
    assert ids(gazette_check) == ["A1", "A3"]
    assert gazette_check.status == "checked"
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(3)


# limit


async def test_limit_bounds_the_items_and_says_so(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(3))
    fake = FakeBoe(
        days={
            ago(2): summary(ago(2), item("O1"), item("O2")),
            ago(1): summary(ago(1), item("N1"), item("N2"), item("N3")),
        }
    )

    result = await check(store, fake, limit=3)

    gazette_check = result.results[0]
    assert ids(gazette_check) == ["O1", "O2", "N1"]  # the oldest day first
    assert gazette_check.matched == 5
    assert gazette_check.has_more is True
    assert gazette_check.status == "partial"
    assert gazette_check.note == "Showing 3 of 5 new items; call again for the rest."
    assert result.complete is False
    # What was not shown stays new: the bookmark stops before the day it was cut in.
    stored = await store.get(OWNER, "vivienda")
    assert stored.checked_through == ago(2)
    assert stored.seen_items == {ago(1): frozenset({"N1"})}

    second = await check(store, fake, limit=3)

    assert ids(second.results[0]) == ["N2", "N3"]
    assert second.complete is True


async def test_no_showing_note_when_everything_fits(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(days={ago(1): summary(ago(1), item("N1"), item("N2"))})

    result = await check(store, fake, limit=2)

    assert result.results[0].has_more is False
    assert "Showing" not in (result.results[0].note or "")


# since, for the gazette


async def test_since_rereads_without_changing_anything_stored(store: SavedSearchStore) -> None:
    seed = await seed_gazette(store, checked_through=TODAY)
    fake = FakeBoe(days={ago(2): summary(ago(2), item("A1")), ago(1): summary(ago(1), item("A2"))})

    result = await check(store, fake, since=ago(2))

    gazette_check = result.results[0]
    assert fake.requested_days == [ago(2), ago(1), TODAY]
    assert gazette_check.from_date == ago(2)
    assert ids(gazette_check) == ["A1", "A2"]
    assert gazette_check.status == "checked"
    assert await store.get(OWNER, seed.name) == seed


async def test_since_is_not_capped_to_31_days(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=TODAY)
    fake = FakeBoe()

    result = await check(store, fake, since=ago(60))

    assert fake.requested_days[0] == ago(60)
    assert len(fake.requested_days) == 61
    assert result.results[0].from_date == ago(60)
    assert "Only the last 31 days" not in (result.results[0].note or "")


async def test_since_in_the_future_is_refused(store: SavedSearchStore) -> None:
    await seed_gazette(store)

    with pytest.raises(ValueError, match="since is in the future"):
        await check(store, FakeBoe(), since=TODAY + timedelta(days=1))


async def test_since_more_than_90_days_back_is_refused(store: SavedSearchStore) -> None:
    await seed_gazette(store)

    with pytest.raises(ValueError, match="since may be at most 90 days back"):
        await check(store, FakeBoe(), since=ago(91))


@pytest.mark.parametrize("back", [0, 89, 90])
async def test_since_within_bounds_is_accepted(store: SavedSearchStore, back: int) -> None:
    await seed_gazette(store)

    result = await check(store, FakeBoe(), since=ago(back))

    assert result.total == 1


# progress


async def test_progress_is_reported_per_law_and_per_day(store: SavedSearchStore) -> None:
    await seed_law(store, minutes=0)
    await seed_gazette(store, checked_through=ago(3), minutes=1)
    fake = FakeBoe(
        analyses={LAW: later(MODIFIES)},
        days={ago(2): summary(ago(2), item("A1"))},
    )
    calls: list[tuple[int, int | None, str]] = []

    async def progress(done: int, total: int | None, message: str) -> None:
        calls.append((done, total, message))

    await check(store, fake, progress=progress)

    # One law and three days (ago(2), ago(1) and today, which has no issue yet).
    assert calls == [(n, 4, f"Read {n} of 4 laws and days.") for n in range(1, 5)]


# --- 2.4 edit_saved_search ----------------------------------------------------------------------


async def edit(store: SavedSearchStore, name: str, **kwargs: Any) -> Any:
    """``edit_saved_search`` as the local owner at the fixed clock."""
    return await saved_searches.edit_saved_search(store, OWNER, name, now=NOW, **kwargs)


async def seed_checked_gazette(store: SavedSearchStore) -> SavedSearch:
    """A gazette search with all three filters, a bookmark and a last check."""
    search = SavedSearch(
        owner=OWNER,
        name="vivienda",
        kind="gazette",
        words="vivienda",
        section="3",
        department_code="8161",
        checked_through=ago(4),
        created_at=NOW - timedelta(days=30),
        last_checked_at=NOW - timedelta(days=4),
    )
    await store.add(search)
    return search


async def test_editing_nothing_is_refused(store: SavedSearchStore) -> None:
    await seed_gazette(store)

    with pytest.raises(ValueError, match="nothing to change"):
        await edit(store, "vivienda")


async def test_editing_nothing_on_a_law_search_is_refused(store: SavedSearchStore) -> None:
    await seed_law(store)

    with pytest.raises(ValueError, match="nothing to change"):
        await edit(store, "ley-39-2015")


async def test_a_gazette_search_is_renamed_keeping_everything_else(
    store: SavedSearchStore,
) -> None:
    before = await seed_checked_gazette(store)

    result = await edit(store, "vivienda", new_name="Ayudas a la Vivienda")

    assert result.search.name == "ayudas-a-la-vivienda"
    assert result.name_was_proposed is False
    assert result.note is None
    assert result.total == 1
    assert await store.names(OWNER) == {"ayudas-a-la-vivienda"}
    after = await store.get(OWNER, "ayudas-a-la-vivienda")
    assert after == replace(before, name="ayudas-a-la-vivienda")


async def test_the_name_to_edit_is_slugified(store: SavedSearchStore) -> None:
    await seed_gazette(store, "mi-busqueda")

    result = await edit(store, "Mi Búsqueda", new_name="otra")

    assert result.search.name == "otra"


async def test_a_new_name_already_used_is_refused(store: SavedSearchStore) -> None:
    await seed_gazette(store, "uno")
    await seed_gazette(store, "dos", minutes=1)

    with pytest.raises(
        ValueError, match="a saved search named 'dos' already exists; choose another name"
    ):
        await edit(store, "uno", new_name="Dos")

    assert await store.names(OWNER) == {"uno", "dos"}


async def test_a_new_name_without_letters_or_digits_is_refused(store: SavedSearchStore) -> None:
    await seed_gazette(store, "uno")

    with pytest.raises(ValueError, match="name must contain letters or digits"):
        await edit(store, "uno", new_name="--")


async def test_a_new_name_may_be_one_only_another_owner_uses(store: SavedSearchStore) -> None:
    await seed_gazette(store, "uno")
    await seed_gazette(store, "dos", owner=OTHER)

    result = await edit(store, "uno", new_name="dos")

    assert result.search.name == "dos"


async def test_filters_not_given_stay_and_given_ones_change(store: SavedSearchStore) -> None:
    before = await seed_checked_gazette(store)

    result = await edit(store, "vivienda", words="alquiler")

    assert result.search.name == "vivienda"  # the name does not follow the filters
    after = await store.get(OWNER, "vivienda")
    assert after.words == "alquiler"
    assert after.section == "3"
    assert after.department_code == "8161"
    assert after.checked_through == before.checked_through
    assert after.last_checked_at == before.last_checked_at
    assert after.created_at == before.created_at


async def test_an_empty_or_blank_string_clears_a_filter(store: SavedSearchStore) -> None:
    await seed_checked_gazette(store)

    await edit(store, "vivienda", section="", department_code="   ")

    after = await store.get(OWNER, "vivienda")
    assert after.section is None
    assert after.department_code is None
    assert after.words == "vivienda"


async def test_a_section_is_upper_cased_when_edited(store: SavedSearchStore) -> None:
    await seed_checked_gazette(store)

    await edit(store, "vivienda", section=" 2a")

    assert (await store.get(OWNER, "vivienda")).section == "2A"


async def test_the_last_filter_cannot_be_cleared(store: SavedSearchStore) -> None:
    await seed_gazette(store, "vivienda", words="vivienda")

    with pytest.raises(ValueError, match="give at least one of words, section or department_code"):
        await edit(store, "vivienda", words="")

    assert (await store.get(OWNER, "vivienda")).words == "vivienda"


async def test_clearing_every_filter_at_once_is_refused(store: SavedSearchStore) -> None:
    before = await seed_checked_gazette(store)

    with pytest.raises(ValueError, match="give at least one of words, section or department_code"):
        await edit(store, "vivienda", words="", section="", department_code="")

    assert await store.get(OWNER, "vivienda") == before


async def test_edited_words_without_letters_or_digits_are_refused(
    store: SavedSearchStore,
) -> None:
    before = await seed_checked_gazette(store)

    with pytest.raises(ValueError, match="words must contain letters or digits"):
        await edit(store, "vivienda", words="?!")

    assert await store.get(OWNER, "vivienda") == before


async def test_filters_and_a_rename_may_change_together(store: SavedSearchStore) -> None:
    await seed_checked_gazette(store)

    await edit(store, "vivienda", new_name="alquiler", words="alquiler")

    after = await store.get(OWNER, "alquiler")
    assert after.words == "alquiler"
    assert after.checked_through == ago(4)


async def test_a_law_search_can_only_be_renamed(store: SavedSearchStore) -> None:
    before = await seed_law(store, seen=frozenset({"a|270|x"}))

    for filters in ({"words": "vivienda"}, {"section": "3"}, {"department_code": "8161"}):
        with pytest.raises(
            ValueError,
            match="a law search can only be renamed; save a new search for another law",
        ):
            await edit(store, "ley-39-2015", **filters)

    assert await store.get(OWNER, "ley-39-2015") == before


async def test_a_law_search_is_renamed_keeping_its_seen_set(store: SavedSearchStore) -> None:
    before = await seed_law(store, seen=frozenset({"a|270|x", "b|210|y"}))

    result = await edit(store, "ley-39-2015", new_name="procedimiento")

    assert result.search.name == "procedimiento"
    assert result.search.kind == "law"
    assert result.name_was_proposed is False
    assert result.note is None
    after = await store.get(OWNER, "procedimiento")
    assert after == replace(before, name="procedimiento")


async def test_editing_an_unknown_name_is_refused(store: SavedSearchStore) -> None:
    with pytest.raises(
        ValueError, match="no saved search named 'nada'; list_saved_searches shows the names"
    ):
        await edit(store, "Nada", new_name="otra")


async def test_another_owners_search_cannot_be_edited(store: SavedSearchStore) -> None:
    before = await seed_gazette(store, "ajena", owner=OTHER)

    with pytest.raises(ValueError, match="no saved search named 'ajena'"):
        await edit(store, "ajena", new_name="mia")

    assert await store.get(OTHER, "ajena") == before
    assert await store.list(OWNER) == []


# --- 2.5 delete_saved_search --------------------------------------------------------------------


async def test_deleting_a_search(store: SavedSearchStore) -> None:
    await seed_gazette(store, "uno")
    await seed_gazette(store, "dos", minutes=1)

    result = await saved_searches.delete_saved_search(store, OWNER, "Uno", now=NOW)

    assert result.deleted == "uno"
    assert result.remaining == 1
    assert await store.names(OWNER) == {"dos"}
    assert result.provenance.generated_at == NOW


async def test_deleting_the_last_search(store: SavedSearchStore) -> None:
    await seed_law(store)

    result = await saved_searches.delete_saved_search(store, OWNER, "ley-39-2015", now=NOW)

    assert result.remaining == 0


async def test_deleting_an_unknown_name_is_refused(store: SavedSearchStore) -> None:
    with pytest.raises(
        ValueError, match="no saved search named 'nada'; list_saved_searches shows the names"
    ):
        await saved_searches.delete_saved_search(store, OWNER, "nada", now=NOW)


async def test_another_owners_search_cannot_be_deleted(store: SavedSearchStore) -> None:
    before = await seed_gazette(store, "ajena", owner=OTHER)

    with pytest.raises(ValueError, match="no saved search named 'ajena'"):
        await saved_searches.delete_saved_search(store, OWNER, "ajena", now=NOW)

    assert await store.get(OTHER, "ajena") == before


async def test_remaining_counts_only_the_owners_searches(store: SavedSearchStore) -> None:
    await seed_gazette(store, "uno")
    await seed_gazette(store, "otra", owner=OTHER)

    result = await saved_searches.delete_saved_search(store, OWNER, "uno", now=NOW)

    assert result.remaining == 0


# --- clarifications S4, S6, S7, S8, S9, S11 ----------------------------------------------------


async def test_s4_days_pending_counts_today_when_it_was_not_read(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(
        days={ago(1): summary(ago(1), item("A1")), TODAY: summary(TODAY, item("A2"))},
        seconds_each=6.0,
    )

    result = await check(store, fake, time_limit=10.0)

    assert fake.requested_days == [ago(1)]
    assert result.results[0].status == "partial"
    assert result.results[0].days_pending == 1  # today


async def test_s4_days_pending_is_zero_when_checked_only_because_today_has_no_issue(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(days={ago(1): summary(ago(1), item("A1"))})

    result = await check(store, fake)

    assert result.results[0].status == "checked"
    assert result.results[0].days_pending == 0


async def test_s4_a_search_that_starts_today_with_no_issue_is_checked(
    store: SavedSearchStore,
) -> None:
    seed = await seed_gazette(store, checked_through=ago(1))
    fake = FakeBoe()  # today has no issue yet

    result = await check(store, fake)

    gazette_check = result.results[0]
    assert fake.requested_days == [TODAY]
    assert gazette_check.status == "checked"
    assert gazette_check.days_pending == 0
    assert gazette_check.from_date == TODAY
    assert (await store.get(OWNER, seed.name)).checked_through == ago(1)
    assert result.note == NO_TODAY_ISSUE


async def test_s5_the_top_level_note_is_null_when_complete_and_silent(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(2))
    fake = FakeBoe(days={ago(1): summary(ago(1), item("A1")), TODAY: summary(TODAY, item("A2"))})

    result = await check(store, fake)

    assert result.complete is True
    assert result.note is None


async def test_s3_an_unreadable_day_is_part_of_the_run(store: SavedSearchStore) -> None:
    await seed_gazette(store, checked_through=ago(3))
    fake = FakeBoe(
        days={ago(2): BoeResponseError("unreadable"), ago(1): summary(ago(1), item("A1"))}
    )

    result = await check(store, fake)

    assert result.results[0].from_date == ago(2)
    assert result.results[0].days_retrying == 1
    assert (await store.get(OWNER, "vivienda")).checked_through == ago(3)


async def test_s6_a_given_name_leaves_only_the_first_check_sentence(
    store: SavedSearchStore,
) -> None:
    gazette_result = await save(store, FakeBoe(), words="vivienda", name="mia")
    fake = FakeBoe(metadata={LAW: metadata()}, analyses={LAW: later()})
    law_result = await save(store, fake, law_id=LAW, name="ley")

    memory = " Stored in memory only: it is lost when the server stops."
    assert gazette_result.note == "The first check reports what is published from now on." + memory
    assert law_result.note == (
        "The first check reports changes recorded from now on "
        "(the BOE data read can be up to 6 hours old)." + memory
    )


async def test_s7_values_equal_to_the_stored_ones_are_not_a_change(
    store: SavedSearchStore,
) -> None:
    await seed_checked_gazette(store)

    with pytest.raises(ValueError, match="nothing to change"):
        await edit(store, "vivienda", new_name="Vivienda", words="vivienda", section="3 ")


async def test_s7_the_same_name_alone_is_not_a_change(store: SavedSearchStore) -> None:
    await seed_law(store)

    with pytest.raises(ValueError, match="nothing to change"):
        await edit(store, "ley-39-2015", new_name="LEY 39 2015")


async def test_s7_a_change_alongside_unchanged_values_is_accepted(
    store: SavedSearchStore,
) -> None:
    await seed_checked_gazette(store)

    await edit(store, "vivienda", words="vivienda", section="4")

    assert (await store.get(OWNER, "vivienda")).section == "4"


@pytest.mark.parametrize("filters", [{"words": ""}, {"section": ""}, {"department_code": ""}])
async def test_s7_an_empty_filter_on_a_law_search_is_still_the_rename_only_error(
    store: SavedSearchStore, filters: dict[str, str]
) -> None:
    await seed_law(store)

    with pytest.raises(ValueError, match="a law search can only be renamed"):
        await edit(store, "ley-39-2015", **filters)


class StrictFake(FakeBoe):
    """A fake whose metadata call refuses a malformed identifier like the real client."""

    async def get_law_metadata(self, law_id: str) -> LawMetadata:
        if not law_id.startswith("BOE-"):
            raise ValueError(f"invalid law identifier: {law_id!r}")
        return await super().get_law_metadata(law_id)


async def test_s8_a_malformed_law_id_is_the_clients_value_error(
    store: SavedSearchStore,
) -> None:
    with pytest.raises(ValueError, match="invalid law identifier"):
        await save(store, StrictFake(), law_id="not an id")

    assert await store.list(OWNER) == []


async def test_s8_a_blank_law_id_counts_as_not_given(store: SavedSearchStore) -> None:
    with pytest.raises(ValueError, match=KIND_ERROR):
        await save(store, FakeBoe(), law_id="  ")

    result = await save(store, FakeBoe(), law_id=" ", words="vivienda")
    assert result.search.kind == "gazette"


@pytest.mark.parametrize("limit", [0, -1, 51, 1000])
async def test_s9_a_limit_outside_1_to_50_is_refused(store: SavedSearchStore, limit: int) -> None:
    await seed_gazette(store)
    fake = FakeBoe()

    with pytest.raises(ValueError, match="limit must be between 1 and 50"):
        await check(store, fake, limit=limit)

    assert fake.log == []


@pytest.mark.parametrize("limit", [1, 50])
async def test_s9_the_limit_bounds_are_accepted(store: SavedSearchStore, limit: int) -> None:
    await seed_gazette(store)

    result = await check(store, FakeBoe(), limit=limit)

    assert result.total == 1


async def test_s11_a_check_stamps_last_checked_at_even_when_nothing_was_to_read(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=TODAY)

    await check(store, FakeBoe())

    assert (await store.get(OWNER, "vivienda")).last_checked_at == NOW


async def test_s11_a_search_that_read_nothing_is_not_stamped(store: SavedSearchStore) -> None:
    seed = await seed_gazette(store, checked_through=ago(2))
    down = BoeUnavailableError("down", status=503)
    fake = FakeBoe(days={ago(1): down, TODAY: down})

    await check(store, fake)

    assert await store.get(OWNER, seed.name) == seed


async def test_s11_a_partial_law_check_cut_by_the_limit_is_stamped(
    store: SavedSearchStore,
) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES, REPEALS)})

    await check(store, fake, limit=1)

    assert (await store.get(OWNER, "ley-39-2015")).last_checked_at == NOW


# --- more edges --------------------------------------------------------------------------------


async def test_the_bookmark_never_moves_back(store: SavedSearchStore) -> None:
    # A bookmark in the future of ``today`` (clock changes) must not be pulled back.
    await seed_gazette(store, checked_through=TODAY + timedelta(days=2))

    await check(store, FakeBoe())

    assert (await store.get(OWNER, "vivienda")).checked_through == TODAY + timedelta(days=2)


async def test_a_gazette_item_with_several_issues_shows_the_issue_number(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(2))
    two_issues = DailySummary(
        day=ago(1),
        issues=(
            GazetteIssue(number="1", identifier="BOE-S-2026-1"),
            GazetteIssue(number="2", identifier="BOE-S-2026-2"),
        ),
        items=(item("A1"),),
    )
    fake = FakeBoe(days={ago(1): two_issues})

    result = await check(store, fake)

    assert result.results[0].items[0].issue == "1"


async def test_a_check_with_since_and_a_stored_law_and_gazette_changes_nothing(
    store: SavedSearchStore,
) -> None:
    gazette_seed = await seed_gazette(store, checked_through=ago(5), minutes=0)
    law_seed = await seed_law(store, minutes=1)
    fake = FakeBoe(
        analyses={LAW: later(ref("BOE-A-2026-1"))}, days={ago(3): summary(ago(3), item("A"))}
    )

    await check(store, fake, since=ago(4))

    assert await store.list(OWNER) == [gazette_seed, law_seed]


async def test_the_gazette_notice_is_used_only_when_a_gazette_search_is_checked(
    store: SavedSearchStore,
) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later()})

    result = await check(store, fake)

    assert result.provenance.notice == Provenance(generated_at=NOW).notice
    assert result.provenance.notice != GAZETTE_NOTICE


# --- the acting owner is never assumed to be "local" -------------------------------------------


async def test_every_use_case_acts_for_the_owner_it_is_given(store: SavedSearchStore) -> None:
    fake = FakeBoe(
        days={ago(2): summary(ago(2)), ago(1): summary(ago(1), item("A1"))},
        analyses={LAW: later(MODIFIES)},
        metadata={LAW: metadata()},
    )
    # A row of the local owner with the same names must be left alone throughout.
    local_gazette = await seed_gazette(store, "vivienda", checked_through=ago(2), owner=OWNER)
    local_law = await seed_law(store, "ley-39-2015", owner=OWNER, minutes=1)

    saved = await saved_searches.save_search(
        store, fake, OTHER, words="vivienda", name="vivienda", now=NOW, today=ago(2)
    )
    saved_law = await saved_searches.save_search(
        store, fake, OTHER, law_id=LAW, name="ley-39-2015", now=NOW, today=ago(2)
    )
    listed = await saved_searches.list_saved_searches(store, OTHER, now=NOW)
    checked = await saved_searches.check_saved_searches(
        store, fake, OTHER, now=NOW, today=TODAY, clock=fake.clock
    )
    edited = await saved_searches.edit_saved_search(
        store, OTHER, "vivienda", new_name="otra", now=NOW
    )
    deleted = await saved_searches.delete_saved_search(store, OTHER, "ley-39-2015", now=NOW)

    assert (saved.total, saved_law.total) == (1, 2)
    # Both were created at the same moment, so the list falls back to the order of the names.
    assert [s.name for s in listed.searches] == ["ley-39-2015", "vivienda"]
    assert [r.name for r in checked.results] == ["ley-39-2015", "vivienda"]
    assert edited.search.name == "otra"
    assert (deleted.deleted, deleted.remaining) == ("ley-39-2015", 1)
    assert await store.names(OTHER) == {"otra"}
    # The same-named rows of the local owner are untouched.
    assert await store.get(OWNER, "vivienda") == local_gazette
    assert await store.get(OWNER, "ley-39-2015") == local_law
    stored = await store.get(OTHER, "otra")
    assert stored.owner == OTHER
    assert stored.checked_through == ago(1)


# --- review fixes: laws and the total, lost updates, names, words -------------------------------


async def test_law_changes_count_toward_the_total_of_a_call(store: SavedSearchStore) -> None:
    many = [ref(f"BOE-A-2026-{n}", "270", "SE MODIFICA", f"art. {n}") for n in range(50)]
    await seed_law(store, "ley", LAW, minutes=0)
    await seed_gazette(store, "vivienda", checked_through=ago(1), minutes=1)
    await seed_gazette(store, "otra", checked_through=ago(1), minutes=2)
    todays = [item(f"A{n:02}", "Vivienda") for n in range(30)]
    fake = FakeBoe(analyses={LAW: later(*many)}, days={TODAY: summary(TODAY, *todays)})

    result = await check(store, fake, limit=50)

    law_check, first, second = result.results
    assert len(law_check.changes) == 50
    assert len(first.items) == 10  # 60 - 50
    assert second.items == []
    assert second.status == "partial"
    assert second.note == "Not reached in this call; call again."


async def test_a_law_the_total_did_not_reach_is_partial_and_marks_nothing(
    store: SavedSearchStore,
) -> None:
    second_law, third_law = "BOE-A-2000-7", "BOE-A-2000-8"
    await seed_law(store, "uno", LAW, minutes=0)
    await seed_law(store, "dos", second_law, minutes=1)
    seed = await seed_law(store, "tres", third_law, minutes=2)
    fifty = [ref(f"BOE-A-2026-{n}", "270", "SE MODIFICA", f"art. {n}") for n in range(50)]
    ten = [ref(f"BOE-A-2027-{n}", "270", "SE MODIFICA", f"art. {n}") for n in range(10)]
    fake = FakeBoe(
        analyses={LAW: later(*fifty), second_law: later(*ten), third_law: later(MODIFIES, JUDICIAL)}
    )

    result = await check(store, fake, limit=50)
    untouched = await store.get(OWNER, "tres")
    again = await check(store, fake, limit=50)

    assert [len(r.changes) for r in result.results] == [50, 10, 0]
    waiting = result.results[2]
    assert (waiting.status, waiting.changes_total) == ("partial", 1)
    assert waiting.note == "Not reached in this call; call again."
    assert waiting.other_new is None
    assert untouched == seed
    # The next call finds the first two done and serves the third, rulings included.
    assert change_ids(again.results[2]) == [MODIFIES.law_id]
    assert again.results[2].other_new == {"court_rulings": 1}
    assert again.complete is True


async def test_other_new_counts_each_kind_and_leaves_zeros_out(store: SavedSearchStore) -> None:
    other = ref("BOE-A-2022-5", "999", "OTRA RELACION", "art. 9")
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(JUDICIAL, JUDICIAL, CITES, other)})

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.other_new == {"court_rulings": 1, "citations": 1, "other": 1}
    # Rulings with the same key are one reference; two different ones are two.


async def test_court_rulings_are_counted_and_the_note_says_where_to_see_them(
    store: SavedSearchStore,
) -> None:
    rulings = [ref(f"BOE-A-2022-{n}", "470", "SENTENCIA", f"STC {n}/2022") for n in range(1, 4)]
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES, *rulings)})

    result = await check(store, fake)

    law_check = result.results[0]
    assert law_check.other_new == {"court_rulings": 3}
    assert law_check.note == (
        "3 new court rulings on this law are not listed; explore_amendment_chain with "
        "include='all' shows them."
    )


async def test_a_failed_and_a_partial_search_say_both_things(store: SavedSearchStore) -> None:
    await seed_law(store, "uno", LAW, minutes=0)
    await seed_gazette(store, minutes=1, checked_through=ago(3))
    fake = FakeBoe(
        analyses={LAW: BoeNotFoundError("gone", status=404)},
        days={ago(2): summary(ago(2), item("A1"), item("A2")), TODAY: summary(TODAY)},
    )

    result = await check(store, fake, limit=1)

    assert result.complete is False
    assert result.note == (
        "1 saved search could not be checked; see its note. "
        "0 of 2 saved searches fully checked. Call again to continue."
    )


async def test_several_failed_searches_are_counted(store: SavedSearchStore) -> None:
    await seed_law(store, "uno", LAW, minutes=0)
    await seed_law(store, "dos", "BOE-A-2000-7", minutes=1)
    gone = BoeNotFoundError("gone", status=404)
    fake = FakeBoe(analyses={LAW: gone, "BOE-A-2000-7": gone})

    result = await check(store, fake)

    assert result.complete is True
    assert result.note == "2 saved searches could not be checked; see their notes."


async def test_the_provenance_of_a_check_of_both_kinds_carries_both_statements(
    store: SavedSearchStore,
) -> None:
    await seed_law(store)
    await seed_gazette(store, minutes=1)
    fake = FakeBoe(analyses={LAW: later()})

    result = await check(store, fake)

    notice = result.provenance.notice
    assert "summaries of the official gazette" in notice
    assert "Consolidated texts are provided for information only" in notice


class GatedBoe(FakeBoe):
    """A fake whose day reads wait for a gate, so another tool can run in the middle."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def get_daily_summary(self, day: date, *, today: date) -> DailySummary | None:
        self.entered.set()
        await self.gate.wait()
        return await super().get_daily_summary(day, today=today)


async def start_check(store: SavedSearchStore, fake: GatedBoe, **kwargs: Any) -> asyncio.Task[Any]:
    """Start a check and wait until it is reading."""
    task = asyncio.create_task(check(store, fake, **kwargs))
    await fake.entered.wait()
    return task


async def test_a_check_does_not_undo_an_edit_made_while_it_was_reading(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(1))
    fake = GatedBoe(days={TODAY: summary(TODAY, item("A1"))})
    task = await start_check(store, fake)

    await saved_searches.edit_saved_search(store, OWNER, "vivienda", words="alquiler")
    fake.gate.set()
    result = await task

    assert ids(result.results[0]) == ["A1"]
    stored = await store.get(OWNER, "vivienda")
    assert stored.words == "alquiler"
    assert stored.seen_items == {TODAY: frozenset({"A1"})}


async def test_a_check_of_a_search_renamed_meanwhile_marks_nothing(
    store: SavedSearchStore,
) -> None:
    await seed_gazette(store, checked_through=ago(1))
    fake = GatedBoe(days={TODAY: summary(TODAY, item("A1"))})
    task = await start_check(store, fake)

    await saved_searches.edit_saved_search(store, OWNER, "vivienda", new_name="piso")
    fake.gate.set()
    result = await task

    gazette_check = result.results[0]
    assert gazette_check.status == "partial"
    assert gazette_check.items == []
    assert gazette_check.note == "Changed during the check; nothing was marked as seen."
    assert result.complete is False
    assert await store.names(OWNER) == {"piso"}
    assert (await store.get(OWNER, "piso")).seen_items == {}


async def test_a_check_of_a_search_deleted_meanwhile_is_harmless(store: SavedSearchStore) -> None:
    await seed_gazette(store, "vivienda", checked_through=ago(1), minutes=0)
    await seed_gazette(store, "becas", words="becas", checked_through=ago(1), minutes=1)
    fake = GatedBoe(days={TODAY: summary(TODAY, item("A1"), item("A2", "Becas"))})
    task = await start_check(store, fake)

    await saved_searches.delete_saved_search(store, OWNER, "becas")
    fake.gate.set()
    result = await task

    assert [(r.name, r.status) for r in result.results] == [
        ("vivienda", "checked"),
        ("becas", "partial"),
    ]
    assert await store.names(OWNER) == {"vivienda"}


async def test_two_checks_at_once_deliver_each_item_once(store: SavedSearchStore) -> None:
    await seed_law(store)
    fake = FakeBoe(analyses={LAW: later(MODIFIES)})

    first, second = await asyncio.gather(check(store, fake), check(store, fake))

    shown = [change_ids(r.results[0]) for r in (first, second)]
    assert sum(len(ids_) for ids_ in shown) == 1  # delivered once, whoever got there first
    for r in (first, second):
        if r.results[0].status == "partial":
            assert r.results[0].note == "Changed during the check; nothing was marked as seen."


async def test_two_unnamed_saves_at_once_get_two_names(store: SavedSearchStore) -> None:
    results = await asyncio.gather(
        save(store, FakeBoe(), words="vivienda"), save(store, FakeBoe(), words="vivienda")
    )

    assert sorted(r.search.name for r in results) == ["vivienda", "vivienda-2"]


async def test_a_chosen_name_taken_in_the_meantime_is_still_an_error(
    store: SavedSearchStore,
) -> None:
    results = await asyncio.gather(
        save(store, FakeBoe(), words="vivienda", name="mia"),
        save(store, FakeBoe(), words="vivienda", name="mia"),
        return_exceptions=True,
    )

    assert sum(isinstance(r, ValueError) for r in results) == 1


async def test_words_longer_than_200_characters_are_refused_on_save_and_edit(
    store: SavedSearchStore,
) -> None:
    await save(store, FakeBoe(), words="vivienda")
    too_long = "vivienda " * 30

    with pytest.raises(ValueError, match="words must be at most 200 characters"):
        await save(store, FakeBoe(), words=too_long)
    with pytest.raises(ValueError, match="words must be at most 200 characters"):
        await saved_searches.edit_saved_search(store, OWNER, "vivienda", words=too_long)
    await save(store, FakeBoe(), words="x" * 200, name="limite")


async def test_a_failure_reading_a_save_baseline_is_the_only_error_swallowed(
    store: SavedSearchStore,
) -> None:
    fake = FakeBoe(days={TODAY: BoeResponseError("unreadable")})

    result = await save(store, fake, words="vivienda")

    assert result.note is not None
    assert "Today's issue could not be read" in result.note
