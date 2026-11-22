"""Tests of ``amendment_chains``: the use case behind ``explore_amendment_chain``.

The catalogue runs over a fake search client and the analyses over a fake client with an injected
clock that advances only when a "request" is made, so nothing here sleeps, reads the real clock or
touches the network. ``chains.explore`` has its own tests (``test_chains``); these cover what the
use case adds: the fetch loop, its budgets, resume, failures, titles and output bounds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from boe_insights_mcp import amendment_chains
from boe_insights_mcp.catalogue import CatalogueStore
from boe_insights_mcp.errors import BoeNotFoundError, BoeResponseError, BoeUnavailableError
from boe_insights_mcp.models import CodedValue, LawAnalysis, LawReference, LawSummary
from boe_insights_mcp.schemas import AmendmentChainResult
from tests.laws import make_law

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
NEWEST = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)

ROOT = "BOE-A-2000-1"


def lid(n: int) -> str:
    """The identifier of test law number ``n``."""
    return f"BOE-A-2000-{n}"


def ref(
    n: int | str, code: str = "270", text: str = "SE MODIFICA", detail: str = "art. 1"
) -> LawReference:
    """A reference to test law ``n`` (or to a literal id) with a relation code and text."""
    law_id = lid(n) if isinstance(n, int) else n
    return LawReference(law_id=law_id, relation=CodedValue(code=code, text=text), text=detail)


def later(*refs: LawReference) -> LawAnalysis:
    """An analysis whose laws acting on it are ``refs``."""
    return LawAnalysis(later=list(refs))


def earlier(*refs: LawReference) -> LawAnalysis:
    """An analysis whose acted-on laws are ``refs``."""
    return LawAnalysis(earlier=list(refs))


class FakeSearch:
    """Serves a fixed catalogue in pages, whatever the query."""

    def __init__(self, laws: list[LawSummary]) -> None:
        self.laws = laws

    async def search_laws(
        self,
        query: str | None = None,
        *,
        offset: int = 0,
        limit: int = 50,
        ttl: float | None = None,
    ) -> list[LawSummary]:
        return self.laws[offset : offset + limit]


def catalogue_of(*numbers: int, extra: list[LawSummary] | None = None) -> CatalogueStore:
    """A store whose snapshot holds test laws ``numbers`` (the root is always included)."""
    laws = [
        make_law(
            lid(n),
            title=f"Ley de prueba {n}",
            published=date(2000, 1, 1),
            updated_at=NEWEST if n == 1 else None,
        )
        for n in {1, *numbers}
    ]
    return CatalogueStore(FakeSearch(laws + (extra or [])), clock=lambda: NOW)


class FakeAnalyses:
    """Stands in for the two analysis methods of ``BoeClient``, with a controllable clock."""

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
            if isinstance(outcome, BoeNotFoundError | BoeResponseError):
                self.cached.add(law_id)  # the real client remembers a permanent failure
            raise outcome
        self.cached.add(law_id)
        return outcome


async def explore(
    store: CatalogueStore, fake: FakeAnalyses, *, law_id: str = ROOT, **kwargs: Any
) -> AmendmentChainResult:
    options: dict[str, Any] = {"now": NOW, "clock": fake.clock}
    options.update(kwargs)
    return await amendment_chains.explore_amendment_chain(
        store,
        fake,
        law_id,
        **options,
    )


def ids(result: AmendmentChainResult) -> list[str]:
    """The node identifiers in the order the result lists them."""
    return [node.law_id for node in result.nodes]


# --- a simple chain ----------------------------------------------------------------------------


async def test_one_level_reads_only_the_starting_law() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2), ref(3, "210", "SE DEROGA"))})

    result = await explore(catalogue_of(2, 3), fake, depth=1)

    assert fake.requested == [ROOT]
    assert (result.law_id, result.direction, result.depth) == (ROOT, "amended_by", 1)
    assert ids(result) == [ROOT, lid(2), lid(3)]
    assert result.levels == [1, 2]
    # B3: the relation is not repeated on the edge: its kind says it, its detail has the wording.
    assert [(e.amending, e.amended, e.kind) for e in result.edges] == [
        (lid(2), ROOT, "modifies"),
        (lid(3), ROOT, "repeals"),
    ]
    assert (result.examined, result.pending, result.failed) == (1, 0, 0)
    assert (result.complete, result.truncated, result.note) == (True, False, None)
    assert result.edges_total == 2
    assert [n.reason for n in result.nodes] == [None, "depth_limit", "depth_limit"]
    assert [n.expanded for n in result.nodes] == [True, False, False]


async def test_two_levels_follow_the_laws_of_the_first() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2)), lid(2): later(ref(3)), lid(3): later(ref(4))})

    result = await explore(catalogue_of(2, 3, 4), fake, depth=2)

    assert fake.requested == [ROOT, lid(2)]
    assert ids(result) == [ROOT, lid(2), lid(3)]
    assert result.levels == [1, 1, 1]
    assert result.complete is True


async def test_the_direction_amends_follows_what_the_law_acted_on() -> None:
    fake = FakeAnalyses({ROOT: earlier(ref(2, "270", "MODIFICA")), lid(2): later(ref(9))})

    result = await explore(catalogue_of(2), fake, direction="amends", depth=1)

    assert [(e.amending, e.amended, e.kind) for e in result.edges] == [(ROOT, lid(2), "modifies")]


async def test_the_id_is_normalised_and_a_malformed_one_is_refused_before_any_request() -> None:
    fake = FakeAnalyses({ROOT: later()})

    result = await explore(catalogue_of(), fake, law_id=" boe-a-2000-1 ")
    assert result.law_id == ROOT

    for bad in ("Ley 40/2015", "", "BOE-A-2000"):
        with pytest.raises(ValueError, match="identifier"):
            await explore(catalogue_of(), fake, law_id=bad)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"depth": 0},
        {"depth": 4},
        {"max_laws": 0},
        {"max_laws": 51},
        {"direction": "sideways"},
        {"include": "everything"},
        {"details": "some"},
    ],
)
async def test_arguments_out_of_range_are_refused_before_any_request(
    kwargs: dict[str, Any],
) -> None:
    fake = FakeAnalyses({ROOT: later()})

    with pytest.raises(ValueError):
        await explore(catalogue_of(), fake, **kwargs)
    assert fake.requested == []


# --- what is followed --------------------------------------------------------------------------


async def test_amendments_only_by_default_and_every_relation_with_include_all() -> None:
    analysis = later(
        ref(2),
        ref(3, "203", "SE CORRIGEN errores"),
        ref(4, "440", "SE DICTA DE CONFORMIDAD"),
        ref(5, "530", "SE RECURRE"),
    )
    fake = FakeAnalyses({ROOT: analysis})

    narrow = await explore(catalogue_of(2, 3, 4, 5), fake, depth=1)
    wide = await explore(catalogue_of(2, 3, 4, 5), fake, depth=1, include="all")

    assert ids(narrow) == [ROOT, lid(2)]
    assert [e.kind for e in wide.edges] == ["modifies", "corrects", "cites", "judicial"]
    assert ids(wide) == [ROOT, lid(2), lid(3), lid(4), lid(5)]


async def test_ids_outside_the_collection_are_never_requested() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2), ref(3)), lid(2): later()})

    # Law 3 is not in the snapshot: it is a correction, a decree... whose analysis is a 404.
    result = await explore(catalogue_of(2), fake, depth=2)

    assert fake.requested == [ROOT, lid(2)]
    node = {n.law_id: n for n in result.nodes}[lid(3)]
    assert (node.in_collection, node.expanded, node.reason) == (False, False, "unavailable")
    assert result.complete is True
    assert result.pending == 0


async def test_nodes_carry_title_rank_and_date_from_the_collection_only() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2), ref("BOE-A-1999-77"))})
    store = catalogue_of(2)

    result = await explore(store, fake, depth=1)

    inside, outside = result.nodes[1], result.nodes[2]
    assert (inside.title, inside.rank, inside.published_on) == (
        "Ley de prueba 2",
        "Ley",
        date(2000, 1, 1),
    )
    assert inside.in_collection is True
    dumped = outside.model_dump()
    # Absent fields are left out of the output instead of being sent as nulls.
    assert set(dumped) == {"law_id", "depth", "in_collection", "expanded", "reason"}
    assert dumped["in_collection"] is False


async def test_rank_and_date_are_given_for_the_root_and_first_level_only() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2)), lid(2): later(ref(3))})

    result = await explore(catalogue_of(2, 3), fake, depth=2)

    by_depth = {node.depth: node for node in result.nodes}
    assert by_depth[1].rank == "Ley" and by_depth[1].published_on == date(2000, 1, 1)
    assert by_depth[2].title is not None
    assert by_depth[2].rank is None and by_depth[2].published_on is None
    # The depth limit is why this node is not expanded; that flag stays.
    assert set(by_depth[2].model_dump()) == {"law_id", "depth", "title", "expanded", "reason"}


async def test_titles_deeper_than_the_first_level_are_cut_at_80_characters() -> None:
    laws = [
        make_law(lid(n), title="T" * 300, published=date(2000, 1, 1), updated_at=NEWEST)
        for n in (1, 2, 3)
    ]
    fake = FakeAnalyses({ROOT: later(ref(2)), lid(2): later(ref(3))})

    result = await explore(CatalogueStore(FakeSearch(laws), clock=lambda: NOW), fake, depth=2)

    by_depth = {node.depth: node for node in result.nodes}
    assert by_depth[1].title == "T" * 99 + "…"
    assert by_depth[2].title == "T" * 79 + "…"


async def test_a_law_reached_twice_is_read_once_and_the_edge_to_it_is_kept() -> None:
    # 2 and 3 are both amended by 4: a diamond. Law 4 costs one request, not two.
    fake = FakeAnalyses(
        {
            ROOT: later(ref(2), ref(3)),
            lid(2): later(ref(4)),
            lid(3): later(ref(4)),
            lid(4): later(),
        }
    )

    result = await explore(catalogue_of(2, 3, 4), fake, depth=3)

    assert fake.requested == [ROOT, lid(2), lid(3), lid(4)]
    assert ids(result) == [ROOT, lid(2), lid(3), lid(4)]
    assert [(e.amending, e.amended) for e in result.edges] == [
        (lid(2), ROOT),
        (lid(3), ROOT),
        (lid(4), lid(2)),
        (lid(4), lid(3)),
    ]


async def test_details_root_gives_the_text_of_the_edges_that_touch_the_starting_law() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2, detail="uno")), lid(2): later(ref(3, detail="dos"))})

    default = await explore(catalogue_of(2, 3), fake, depth=2)
    everything = await explore(catalogue_of(2, 3), fake, depth=2, details="all")
    nothing = await explore(catalogue_of(2, 3), fake, depth=2, details="none")

    assert [e.detail for e in default.edges] == ["uno", None]
    assert [e.detail for e in everything.edges] == ["uno", "dos"]
    assert [e.detail for e in nothing.edges] == [None, None]
    assert "detail" not in default.edges[1].model_dump()


async def test_fields_equal_to_their_common_default_are_left_out() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2), ref("BOE-A-1999-77")), lid(2): later()})

    result = await explore(catalogue_of(2), fake, depth=2)

    root, inside, outside = (n.model_dump() for n in result.nodes)
    assert "in_collection" not in root and "expanded" not in root
    assert "in_collection" not in inside and "expanded" not in inside
    assert outside["in_collection"] is False and outside["expanded"] is False
    assert "relation" not in result.model_dump()["edges"][0]


async def test_long_titles_are_cut_at_100_characters() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2))})
    laws = [make_law(lid(1), title="t", published=date(2000, 1, 1), updated_at=NEWEST)]
    laws.append(make_law(lid(2), title="T" * 300, published=date(2000, 1, 1)))
    store = CatalogueStore(FakeSearch(laws), clock=lambda: NOW)

    result = await explore(store, fake, depth=1)

    assert result.nodes[1].title == "T" * 99 + "…"


async def test_court_rulings_on_the_starting_law_are_counted_when_left_out() -> None:
    rulings = later(ref(2), ref(3, "470", "SE DECLARA"), ref(4, "552", "Recurso"))
    fake = FakeAnalyses({ROOT: rulings})

    default = await explore(catalogue_of(2, 3, 4), fake, depth=1)
    wide = await explore(catalogue_of(2, 3, 4), fake, depth=1, include="all")

    assert default.court_rulings_excluded == 2
    assert default.note == "2 court rulings on this law are not shown; use include='all'."
    assert [e.kind for e in default.edges] == ["modifies"]
    assert wide.court_rulings_excluded == 0
    assert wide.note is None
    assert [e.kind for e in wide.edges] == ["modifies", "judicial", "judicial"]


async def test_a_long_detail_is_cut_at_120_characters() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2, detail="x" * 500))})

    result = await explore(catalogue_of(2), fake, depth=1)

    assert result.edges[0].detail == "x" * 119 + "…"


# --- budget and resume -------------------------------------------------------------------------


def fan(width: int) -> dict[str, LawAnalysis | Exception]:
    """The root is amended by laws 2..width+1, and each of those by nothing."""
    analyses: dict[str, LawAnalysis | Exception] = {
        ROOT: later(*[ref(n) for n in range(2, width + 2)])
    }
    analyses.update({lid(n): later() for n in range(2, width + 2)})
    return analyses


async def test_max_laws_bounds_the_fresh_reads_and_a_second_call_continues() -> None:
    fake = FakeAnalyses(fan(5))
    store = catalogue_of(*range(2, 7))

    first = await explore(store, fake, depth=2, max_laws=3)

    # The root and two of the five laws were read.
    assert fake.requested == [ROOT, lid(2), lid(3)]
    assert (first.examined, first.pending, first.complete) == (3, 3, False)
    assert first.note == (
        "Examined 3 laws, 3 still to read. Call again with the same arguments to continue."
    )
    unread = [n for n in first.nodes if n.reason == "not_fetched"]
    assert [n.law_id for n in unread] == [lid(4), lid(5), lid(6)]

    second = await explore(store, fake, depth=2, max_laws=3)

    # The three cached analyses are free: only the three missing ones are requested.
    assert fake.requested == [ROOT, lid(2), lid(3), lid(4), lid(5), lid(6)]
    assert (second.examined, second.pending, second.complete, second.note) == (6, 0, True, None)


async def test_cached_analyses_do_not_count_against_max_laws() -> None:
    fake = FakeAnalyses(fan(4))
    fake.cached = {ROOT, lid(3), lid(4)}

    result = await explore(catalogue_of(*range(2, 6)), fake, depth=2, max_laws=1)

    # Law 2 is the one fresh read the budget allows; laws 3 and 4 come from the cache for free
    # even though the budget is spent by then; law 5 would be a second fresh read and waits.
    assert fake.requested == [lid(2)]
    assert (result.examined, result.pending, result.complete) == (4, 1, False)


async def test_the_time_budget_ends_the_loop_and_the_rest_waits_for_the_next_call() -> None:
    fake = FakeAnalyses(fan(5), seconds_each=20.0)
    store = catalogue_of(*range(2, 7))

    short = await explore(store, fake, depth=2, time_limit=45.0)

    # 40 s of budget at 20 s a request: the root and one more start, then the deadline passes.
    assert fake.requested == [ROOT, lid(2)]
    assert (short.examined, short.pending, short.complete) == (2, 4, False)

    rest = await explore(store, fake, depth=2, time_limit=1000.0)

    assert fake.requested[2:] == [lid(3), lid(4), lid(5), lid(6)]
    assert (rest.examined, rest.complete) == (6, True)


async def test_the_clock_starts_before_the_catalogue_is_read() -> None:
    fake = FakeAnalyses(fan(1))
    store = catalogue_of(2)
    ticks = iter([0.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])

    # The deadline is taken from the first reading (0.0 plus 35 s) and the next reading is
    # already past it: nothing is requested and the result says the root is still to read.
    result = await amendment_chains.explore_amendment_chain(
        store, fake, ROOT, now=NOW, clock=lambda: next(ticks)
    )

    assert fake.requested == []
    assert (result.examined, result.pending, result.complete) == (0, 1, False)
    assert ids(result) == [ROOT]
    assert result.nodes[0].reason == "not_fetched"


# --- failures ----------------------------------------------------------------------------------


async def test_the_starting_law_not_found_is_an_error() -> None:
    fake = FakeAnalyses({ROOT: BoeNotFoundError("no")})

    with pytest.raises(BoeNotFoundError):
        await explore(catalogue_of(), fake)


async def test_the_starting_law_unreachable_is_an_error_too() -> None:
    fake = FakeAnalyses({ROOT: BoeUnavailableError("down")})

    with pytest.raises(BoeUnavailableError):
        await explore(catalogue_of(), fake)


async def test_a_permanent_failure_ends_the_chain_at_that_law_and_is_not_pending() -> None:
    fake = FakeAnalyses(
        {ROOT: later(ref(2), ref(3)), lid(2): BoeNotFoundError("gone"), lid(3): later()}
    )

    result = await explore(catalogue_of(2, 3), fake, depth=2)

    node = {n.law_id: n for n in result.nodes}[lid(2)]
    assert (node.expanded, node.reason) == (False, "unavailable")
    assert (result.examined, result.pending, result.failed, result.complete) == (2, 0, 0, True)


async def test_an_unreadable_analysis_is_permanent_too() -> None:
    fake = FakeAnalyses({ROOT: later(ref(2)), lid(2): BoeResponseError("garbled")})

    result = await explore(catalogue_of(2), fake, depth=2)

    assert result.complete is True
    assert {n.law_id: n.reason for n in result.nodes}[lid(2)] == "unavailable"


async def test_a_temporary_failure_is_counted_pending_not_retried_and_retried_next_call() -> None:
    fake = FakeAnalyses(
        {ROOT: later(ref(2), ref(3)), lid(2): BoeUnavailableError("busy"), lid(3): later()}
    )
    store = catalogue_of(2, 3)

    first = await explore(store, fake, depth=2)

    # One attempt for law 2 in the whole call, however many rounds it takes to finish.
    assert fake.requested == [ROOT, lid(2), lid(3)]
    assert (first.examined, first.failed, first.pending, first.complete) == (2, 1, 1, False)
    assert {n.law_id: n.reason for n in first.nodes}[lid(2)] == "not_fetched"
    assert "Call again" in (first.note or "")

    fake.analyses[lid(2)] = later()
    second = await explore(store, fake, depth=2)

    assert fake.requested == [ROOT, lid(2), lid(3), lid(2)]
    assert (second.examined, second.failed, second.complete) == (3, 0, True)


# --- output bounds -----------------------------------------------------------------------------


async def test_at_most_250_edges_are_listed_and_edges_total_says_how_many_there_are() -> None:
    codes = [("270", "SE MODIFICA"), ("271", "SE MODIFICA"), ("272", "SE MODIFICA")]
    codes += [("220", "SE MODIFICA"), ("221", "SE MODIFICA")]
    refs = [ref(n, code, text) for n in range(2, 102) for code, text in codes]
    fake = FakeAnalyses({ROOT: later(*refs)})

    result = await explore(catalogue_of(*range(2, 102)), fake, depth=1)

    assert (len(result.edges), result.edges_total) == (250, 500)
    assert (len(result.nodes), result.nodes_total) == (100, 101)
    assert result.truncated is False


async def test_at_most_100_laws_are_listed_and_the_chain_says_it_was_cut() -> None:
    fake = FakeAnalyses(fan(200))

    result = await explore(catalogue_of(*range(2, 202)), fake, depth=1)

    assert (len(result.nodes), result.nodes_total) == (100, 150)
    assert result.truncated is True
    # include is already 'amendments', so the note must not suggest it (B4).
    assert result.note == "Stopped at 150 laws; use a smaller depth."
    assert result.complete is True


async def test_the_truncation_note_suggests_include_amendments_only_when_it_is_not_set() -> None:
    fake = FakeAnalyses(fan(200))

    result = await explore(catalogue_of(*range(2, 202)), fake, depth=1, include="all")

    assert result.note == "Stopped at 150 laws; use a smaller depth or include='amendments'."


async def test_a_note_for_both_causes_gives_both_sentences() -> None:
    fake = FakeAnalyses(fan(200))

    result = await explore(catalogue_of(*range(2, 202)), fake, depth=2, max_laws=2)

    assert result.complete is False and result.truncated is True
    assert (result.note or "").startswith("Examined 2 laws, ")
    assert (result.note or "").endswith("use a smaller depth.")


# --- progress and provenance -------------------------------------------------------------------


async def test_progress_is_reported_per_analysis_with_increasing_values() -> None:
    reports: list[tuple[int, int | None, str]] = []

    async def progress(done: int, total: int | None, message: str) -> None:
        reports.append((done, total, message))

    fake = FakeAnalyses(fan(3))

    await explore(catalogue_of(2, 3, 4), fake, depth=2, progress=progress)

    values = [done for done, _, _ in reports]
    assert values == sorted(set(values))
    assert len(values) >= 4  # one per analysis read (the catalogue pages add their own)
    assert all(total is None or total >= done for done, total, _ in reports)


async def test_the_provenance_carries_the_catalogues_newest_update() -> None:
    fake = FakeAnalyses({ROOT: later()})

    result = await explore(catalogue_of(), fake, depth=1)

    assert result.provenance.generated_at == NOW
    assert result.provenance.data_updated_at == NEWEST
    assert result.provenance.attribution.startswith("Basado en datos")
    assert "Consolidated texts" in result.provenance.notice
