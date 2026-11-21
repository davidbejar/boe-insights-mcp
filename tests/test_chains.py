"""Contract tests for ``boe_insights_mcp.chains``: relation kinds and the amendment chain explorer.

Everything under test is pure logic over ``LawAnalysis`` records. Most cases build tiny analyses by
hand with ``analysis``/``earlier``/``later`` below; a few end-to-end cases use real analyses
recorded by the fourth probe (``tests/fixtures/boe/analisis_*``), built exactly the way the client
builds them (``LawAnalysis.model_validate`` on the first record of ``data``). No network, no clock,
no randomness. Tests that rest on a reading of an ambiguous rule carry a comment
``SPEC AMBIGUITY`` that names the rule, so they are easy to find and revisit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from boe_insights_mcp.chains import (
    AMENDING_KINDS,
    Chain,
    ChainEdge,
    ChainNode,
    RelationKind,
    explore,
    level_counts,
    relation_kind,
)
from boe_insights_mcp.models import LawAnalysis, as_list

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "boe"

Analyses = dict[str, LawAnalysis | None]
Ref = tuple[str, str, str, str]

ALL_KINDS: tuple[RelationKind, ...] = get_args(RelationKind)
REASONS = {"", "depth_limit", "not_fetched", "unavailable"}

# --- factories ----------------------------------------------------------------------------------

ACTIVE = {"270": "MODIFICA", "210": "DEROGA", "231": "SUSPENDE", "201": "CORRECCION de errores"}
PASSIVE = {"270": "SE MODIFICA", "210": "SE DEROGA", "231": "SE SUSPENDE", "201": "CORRECCION"}


def analysis(earlier: Iterable[Ref] = (), later: Iterable[Ref] = ()) -> LawAnalysis:
    """Build a ``LawAnalysis`` from ``(law_id, code, text, detail)`` tuples for each side."""

    def rows(refs: Iterable[Ref]) -> list[dict[str, Any]]:
        return [
            {"id_norma": i, "relacion": {"codigo": c, "texto": t}, "texto": d}
            for (i, c, t, d) in refs
        ]

    return LawAnalysis.model_validate(
        {
            "referencias": {
                "anteriores": [{"anterior": rows(earlier)}] if earlier else "",
                "posteriores": [{"posterior": rows(later)}] if later else "",
            }
        }
    )


def earlier(*ids: str, code: str = "270") -> list[Ref]:
    """References in the ACTIVE voice (what ``earlier`` holds): the detail cites the target."""
    return [(i, code, ACTIVE.get(code, "REL " + code), f"target {i}") for i in ids]


def later(*ids: str, code: str = "270") -> list[Ref]:
    """References in the PASSIVE voice (what ``later`` holds): the detail cites the actor."""
    return [(i, code, PASSIVE.get(code, "SE REL " + code), f"actor {i}") for i in ids]


def both(*ids: str, code: str = "270") -> LawAnalysis:
    """An analysis whose two sides list the same ids, so both directions see the same graph."""
    return analysis(earlier=earlier(*ids, code=code), later=later(*ids, code=code))


def graph(**links: Sequence[str]) -> Analyses:
    """Build analyses from ``name=[ids]`` pairs; both sides list the ids (code 270)."""
    return {name: both(*ids) for name, ids in links.items()}


def ids(chain: Chain) -> list[str]:
    """The node ids in chain order."""
    return [n.law_id for n in chain.nodes]


def node(chain: Chain, law_id: str) -> ChainNode:
    """The node with the given id; fails the test when it is missing."""
    matches = [n for n in chain.nodes if n.law_id == law_id]
    assert len(matches) == 1, f"{law_id} is not a node exactly once: {ids(chain)}"
    return matches[0]


def triples(chain: Chain) -> list[tuple[str, str, str]]:
    """Edges as ``(amending, amended, code)``, in chain order."""
    return [(e.amending, e.amended, e.code) for e in chain.edges]


def pairs(chain: Chain) -> list[tuple[str, str]]:
    """Edges as ``(amending, amended)``, in chain order."""
    return [(e.amending, e.amended) for e in chain.edges]


# --- relation_kind ------------------------------------------------------------------------------

KIND_CODES: dict[str, list[str]] = {
    "modifies": ["270", "271", "272", "220", "221", "245", "247"],
    "repeals": ["210", "211", "212", "213", "214", "215", "216", "217", "230", "235"],
    "adds": ["407", "408"],
    "suspends": ["231"],
    "corrects": ["201", "202", "203", "204"],
    "judicial": ["520", "530", "540", "552", "693", "694"],
    "cites": [
        "300", "301", "302", "303", "330", "331",
        "400", "401", "402", "403", "404", "405", "406",
        "420", "421", "422", "423", "424", "425", "426", "427",
        "430", "440", "470", "480", "490",
    ],
}  # fmt: skip


@pytest.mark.parametrize(
    ("code", "kind"),
    [(code, kind) for kind, codes in KIND_CODES.items() for code in codes],
)
def test_relation_kind_by_code(code: str, kind: str) -> None:
    """Every code the spec lists classifies as the spec says."""
    assert relation_kind(code) == kind


@pytest.mark.parametrize(
    "code",
    [
        "", "0", "100", "200", "205", "209", "218", "219", "222", "232", "246", "248", "250",
        "273", "299", "304", "329", "332", "409", "410", "428", "431", "441", "471", "481",
        "491", "500", "521", "550", "600", "695", "999", "abc", "27", "2700",
    ],
)  # fmt: skip
def test_relation_kind_unknown_is_other(code: str) -> None:
    """Codes outside the spec's lists (and malformed ones) are ``other``, never an error."""
    assert relation_kind(code) == "other"


def test_relation_kind_key_codes_are_not_confused() -> None:
    """The distinctions the chain depends on: 231 suspends, 440 cites, 270 modifies."""
    assert relation_kind("231") == "suspends"
    assert relation_kind("231") != "modifies"
    assert relation_kind("440") == "cites"
    assert relation_kind("440") != "modifies"
    assert relation_kind("201") == "corrects"
    assert relation_kind("552") == "judicial"
    assert relation_kind("470") == "cites"


def test_amending_kinds_constant() -> None:
    """``AMENDING_KINDS`` holds exactly the four kinds that change another norm's text."""
    assert set(AMENDING_KINDS) == {"modifies", "repeals", "adds", "suspends"}


def _auxiliary_codes() -> dict[str, str]:
    """Every code of the two real relation tables, with one text each (``side:code``)."""
    codes: dict[str, str] = {}
    for side in ("anteriores", "posteriores"):
        body = json.loads((FIXTURE_DIR / f"aux_relaciones_{side}.json").read_text("utf-8"))
        for code, text in body["data"].items():
            codes[f"{side}:{code}"] = text
    return codes


def test_every_code_of_the_real_tables_classifies() -> None:
    """All 50 codes of both real tables give a valid kind; none falls into ``other``."""
    table = _auxiliary_codes()
    assert len(table) == 100
    others: dict[str, str] = {}
    for key, text in table.items():
        code = key.split(":")[1]
        kind = relation_kind(code)
        assert kind in ALL_KINDS
        if kind == "other":
            others[key] = text
    # 247 ("ENMIENDAS" / "SE PUBLICA Enmienda", amendments to a treaty) is a modification.
    assert relation_kind("247") == "modifies"
    assert others == {}


def test_the_two_real_tables_share_their_codes() -> None:
    """The codes are the same on both sides, which is why classification keys on them."""
    table = _auxiliary_codes()
    earlier_codes = {k.split(":")[1] for k in table if k.startswith("anteriores")}
    later_codes = {k.split(":")[1] for k in table if k.startswith("posteriores")}
    assert earlier_codes == later_codes


# --- the root and the three ways of not expanding ------------------------------------------------


def test_root_not_fetched_is_the_only_thing_to_fetch() -> None:
    """With nothing known the chain is the root alone and asks for its analysis."""
    chain = explore("R", {}, direction="amended_by")
    assert chain.root == "R"
    assert chain.direction == "amended_by"
    assert chain.nodes == (ChainNode("R", 0, False, "not_fetched"),)
    assert chain.edges == ()
    assert chain.to_fetch == ("R",)
    assert chain.truncated is False


def test_root_is_always_fetchable() -> None:
    """``fetchable`` is never allowed to veto the root."""
    chain = explore("R", {}, direction="amends", fetchable=lambda law_id: False)
    assert node(chain, "R").reason == "not_fetched"
    assert chain.to_fetch == ("R",)


def test_root_with_unavailable_analysis() -> None:
    """A root whose analysis is known to be unavailable is a lone, unexpanded node."""
    chain = explore("R", {"R": None}, direction="amended_by")
    assert chain.nodes == (ChainNode("R", 0, False, "unavailable"),)
    assert chain.to_fetch == ()
    assert chain.edges == ()
    assert chain.truncated is False


def test_root_without_references_is_expanded() -> None:
    """An analysis with no references still counts as expanded."""
    chain = explore("R", {"R": analysis()}, direction="amended_by")
    assert chain.nodes == (ChainNode("R", 0, True, ""),)
    assert chain.edges == ()
    assert chain.to_fetch == ()


def test_expanded_nodes_have_an_empty_reason() -> None:
    """``expanded`` and an empty ``reason`` always go together, and reasons are the known ones."""
    analyses: Analyses = {"R": both("A", "B"), "A": analysis(), "B": None}
    chain = explore("R", analyses, direction="amends", max_depth=3)
    for n in chain.nodes:
        assert n.reason in REASONS
        assert n.expanded == (n.reason == "")


def test_unknown_child_is_not_fetched_and_listed() -> None:
    """A fetchable child absent from the mapping is ``not_fetched`` and goes to ``to_fetch``."""
    chain = explore("R", {"R": both("A")}, direction="amended_by")
    assert node(chain, "A") == ChainNode("A", 1, False, "not_fetched")
    assert chain.to_fetch == ("A",)


def test_child_known_unavailable_is_not_listed() -> None:
    """A child whose analysis is ``None`` is ``unavailable`` and is not fetched again."""
    chain = explore("R", {"R": both("A"), "A": None}, direction="amended_by")
    assert node(chain, "A") == ChainNode("A", 1, False, "unavailable")
    assert chain.to_fetch == ()


def test_child_not_fetchable_is_unavailable_and_not_listed() -> None:
    """An id outside the consolidated collection is ``unavailable`` and never to be fetched."""
    chain = explore(
        "R",
        {"R": both("A", "OUT")},
        direction="amended_by",
        fetchable=lambda law_id: law_id != "OUT",
    )
    assert node(chain, "OUT") == ChainNode("OUT", 1, False, "unavailable")
    assert node(chain, "A").reason == "not_fetched"
    assert chain.to_fetch == ("A",)


def test_a_known_analysis_wins_over_fetchable() -> None:
    """An analysis in the mapping is used even when ``fetchable`` says no for that id."""
    chain = explore(
        "R",
        {"R": both("A"), "A": both("B")},
        direction="amended_by",
        fetchable=lambda law_id: False,
    )
    assert node(chain, "A").expanded is True
    assert "B" in ids(chain)


def test_depth_limit_reason_for_last_level() -> None:
    """Nodes at ``max_depth`` are not expanded, whatever is known about them."""
    analyses: Analyses = {"R": both("A"), "A": both("B"), "B": both("C")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2)
    assert node(chain, "R").expanded and node(chain, "A").expanded
    assert node(chain, "B") == ChainNode("B", 2, False, "depth_limit")
    assert "C" not in ids(chain)
    assert chain.to_fetch == ()


def test_depth_limit_takes_precedence_over_other_reasons() -> None:
    """A node at the depth limit is ``depth_limit`` even if unavailable or not fetchable."""
    analyses: Analyses = {"R": both("A", "B", "C"), "A": None}
    chain = explore(
        "R",
        analyses,
        direction="amends",
        max_depth=1,
        fetchable=lambda law_id: law_id != "B",
    )
    for law_id in ("A", "B", "C"):
        assert node(chain, law_id).reason == "depth_limit"
    assert chain.to_fetch == ()


# --- direction, orientation, relation text -------------------------------------------------------


def test_amended_by_follows_later_and_orients_edges() -> None:
    """``amended_by`` reads ``later``: the referenced law acts on the one being read."""
    a = analysis(earlier=earlier("IGNORED"), later=later("ACTOR"))
    chain = explore("R", {"R": a}, direction="amended_by", max_depth=1)
    assert ids(chain) == ["R", "ACTOR"]
    (edge,) = chain.edges
    assert edge.amending == "ACTOR"
    assert edge.amended == "R"
    assert edge.code == "270"
    assert edge.kind == "modifies"


def test_amends_follows_earlier_and_orients_edges() -> None:
    """``amends`` reads ``earlier``: the law being read acts on the referenced one."""
    a = analysis(earlier=earlier("TARGET"), later=later("IGNORED"))
    chain = explore("R", {"R": a}, direction="amends", max_depth=1)
    assert ids(chain) == ["R", "TARGET"]
    (edge,) = chain.edges
    assert edge.amending == "R"
    assert edge.amended == "TARGET"
    assert edge.kind == "modifies"


def test_relation_text_is_kept_as_read_on_each_side() -> None:
    """The text is not rewritten: active from ``earlier``, passive from ``later``."""
    a = analysis(earlier=earlier("T"), later=later("A"))
    amends = explore("R", {"R": a}, direction="amends", max_depth=1)
    amended_by = explore("R", {"R": a}, direction="amended_by", max_depth=1)
    assert amends.edges[0].relation == "MODIFICA"
    assert amended_by.edges[0].relation == "SE MODIFICA"


def test_relation_text_is_passed_through_unaltered() -> None:
    """Odd casing and inner spacing in the relation text survive; so does the detail."""
    odd: Ref = ("A", "216", "Se DEROGA de  forma reiterada", "  el art. 43,\tpor Ley 3/2020 ")
    parsed = analysis(later=[odd]).later[0]
    chain = explore("R", {"R": analysis(later=[odd])}, direction="amended_by", max_depth=1)
    (edge,) = chain.edges
    assert edge.relation == parsed.relation.text
    assert edge.detail == parsed.text
    assert edge.code == "216"
    assert edge.kind == "repeals"


def test_edge_detail_is_the_reference_text() -> None:
    """``detail`` is the reference's ``texto`` for the side that was read."""
    ref: Ref = ("T", "270", "MODIFICA", "el art. 6.2 de la Ley 11/2002, de 6 de mayo")
    chain = explore("R", {"R": analysis(earlier=[ref])}, direction="amends", max_depth=1)
    assert chain.edges[0].detail == "el art. 6.2 de la Ley 11/2002, de 6 de mayo"


def test_the_same_amendment_read_from_both_sides_has_the_same_orientation() -> None:
    """R amends T: read from R (amends) and from T (amended_by) gives the same amending/amended."""
    r = analysis(earlier=earlier("T"))
    t = analysis(later=later("R"))
    from_r = explore("R", {"R": r}, direction="amends", max_depth=1)
    from_t = explore("T", {"T": t}, direction="amended_by", max_depth=1)
    assert pairs(from_r) == pairs(from_t) == [("R", "T")]
    assert triples(from_r) == triples(from_t)
    assert from_r.edges[0].kind == from_t.edges[0].kind


def test_edge_kind_is_the_relation_kind_of_its_code() -> None:
    """Each edge carries ``relation_kind`` of its own code."""
    codes = ["270", "210", "407", "231", "201", "330", "552", "248"]
    refs: list[Ref] = [(f"T{c}", c, f"TEXT {c}", f"d{c}") for c in codes]
    chain = explore(
        "R",
        {"R": analysis(earlier=refs)},
        direction="amends",
        max_depth=1,
        kinds=ALL_KINDS,
    )
    assert [e.code for e in chain.edges] == codes
    assert [e.kind for e in chain.edges] == [relation_kind(c) for c in codes]
    assert [e.kind for e in chain.edges] == [
        "modifies", "repeals", "adds", "suspends", "corrects", "cites", "judicial", "other",
    ]  # fmt: skip


def test_directions_are_independent_on_the_same_analyses() -> None:
    """The two directions of one mapping give different graphs (no cross-contamination)."""
    analyses: Analyses = {
        "R": analysis(earlier=earlier("T1", "T2"), later=later("A1")),
        "T1": analysis(earlier=earlier("T3"), later=later("R")),
        "A1": analysis(earlier=earlier("R"), later=later("A2")),
    }
    amends = explore("R", analyses, direction="amends", max_depth=2)
    amended_by = explore("R", analyses, direction="amended_by", max_depth=2)
    assert ids(amends) == ["R", "T1", "T2", "T3"]
    assert ids(amended_by) == ["R", "A1", "A2"]
    assert pairs(amends) == [("R", "T1"), ("R", "T2"), ("T1", "T3")]
    assert pairs(amended_by) == [("A1", "R"), ("A2", "A1")]


# --- kinds filter --------------------------------------------------------------------------------


def _mixed() -> Analyses:
    refs: list[Ref] = [
        ("MOD", "270", "MODIFICA", "d"),
        ("COR", "201", "CORRECCION", "d"),
        ("CIT", "330", "CITA", "d"),
        ("JUD", "552", "Recurso", "d"),
        ("REP", "210", "DEROGA", "d"),
        ("ADD", "407", "AÑADE", "d"),
        ("SUS", "231", "SUSPENDE", "d"),
        ("OTH", "248", "OTRA", "d"),
    ]
    return {"R": analysis(earlier=refs)}


def test_default_kinds_follow_only_amendments() -> None:
    """The default ``kinds`` is the four amending kinds."""
    chain = explore("R", _mixed(), direction="amends", max_depth=1)
    assert ids(chain) == ["R", "MOD", "REP", "ADD", "SUS"]
    assert {e.kind for e in chain.edges} == set(AMENDING_KINDS)


def test_kinds_filter_selects_exactly_the_requested_kinds() -> None:
    """A single other kind gives only those references."""
    chain = explore("R", _mixed(), direction="amends", max_depth=1, kinds={"corrects"})
    assert ids(chain) == ["R", "COR"]
    chain = explore("R", _mixed(), direction="amends", max_depth=1, kinds={"cites", "judicial"})
    assert ids(chain) == ["R", "CIT", "JUD"]
    chain = explore("R", _mixed(), direction="amends", max_depth=1, kinds={"other"})
    assert ids(chain) == ["R", "OTH"]


def test_all_kinds_follow_everything() -> None:
    """Listing every kind reports every non-empty reference, in analysis order."""
    chain = explore("R", _mixed(), direction="amends", max_depth=1, kinds=ALL_KINDS)
    assert ids(chain) == ["R", "MOD", "COR", "CIT", "JUD", "REP", "ADD", "SUS", "OTH"]
    assert len(chain.edges) == 8


def test_empty_kinds_follow_nothing() -> None:
    """An empty collection of kinds yields the root alone."""
    chain = explore("R", _mixed(), direction="amends", max_depth=1, kinds=())
    assert ids(chain) == ["R"]
    assert chain.edges == ()
    assert chain.truncated is False


@pytest.mark.parametrize("make", [list, tuple, set, frozenset])
def test_kinds_accepts_any_collection(make: Callable[[list[str]], Any]) -> None:
    """``kinds`` is any collection of kinds, not only a set."""
    kinds = cast("Collection[RelationKind]", make(["corrects"]))
    chain = explore("R", _mixed(), direction="amends", max_depth=1, kinds=kinds)
    assert ids(chain) == ["R", "COR"]


def test_filtered_out_references_do_not_create_nodes_or_fetches() -> None:
    """A reference of another kind neither becomes a node nor an id to fetch."""
    chain = explore("R", _mixed(), direction="amends", max_depth=2)
    assert "COR" not in ids(chain)
    assert "CIT" not in ids(chain)
    assert set(chain.to_fetch) == {"MOD", "REP", "ADD", "SUS"}


def test_kinds_apply_at_every_level() -> None:
    """The filter also applies to the references of deeper laws."""
    analyses: Analyses = {
        "R": analysis(earlier=earlier("A")),
        "A": analysis(earlier=earlier("B") + earlier("C", code="330")),
    }
    chain = explore("R", analyses, direction="amends", max_depth=2)
    assert ids(chain) == ["R", "A", "B"]


def test_same_target_with_different_kinds_respects_the_filter() -> None:
    """One id listed as a citation only never appears under the amendment filter."""
    a = analysis(earlier=earlier("X", code="330"))
    chain = explore("R", {"R": a}, direction="amends", max_depth=1)
    assert ids(chain) == ["R"]


# --- empty ids -----------------------------------------------------------------------------------


def test_empty_ids_are_skipped() -> None:
    """A reference with an empty id gives no node and no edge."""
    refs: list[Ref] = [("", "270", "MODIFICA", "regional decree"), *earlier("T")]
    chain = explore("R", {"R": analysis(earlier=refs)}, direction="amends", max_depth=1)
    assert ids(chain) == ["R", "T"]
    assert pairs(chain) == [("R", "T")]
    assert chain.to_fetch == ()


def test_empty_ids_never_reach_to_fetch() -> None:
    """Even where fetching is possible, an empty id is not requested."""
    refs: list[Ref] = [("", "270", "MODIFICA", "d")]
    chain = explore("R", {"R": analysis(earlier=refs)}, direction="amends", max_depth=3)
    assert "" not in chain.to_fetch
    assert "" not in ids(chain)


def test_empty_ids_do_not_count_against_the_node_limit() -> None:
    """Skipping an empty id is not a drop: it cannot make the chain ``truncated``."""
    refs: list[Ref] = [("", "270", "MODIFICA", "d"), *earlier("T")]
    chain = explore(
        "R", {"R": analysis(earlier=refs)}, direction="amends", max_depth=1, max_nodes=2
    )
    assert ids(chain) == ["R", "T"]
    assert chain.truncated is False


# --- breadth-first order, depth, cycles, diamonds ------------------------------------------------


def test_nodes_are_in_breadth_first_order() -> None:
    """By depth, then by order of discovery; not depth-first."""
    analyses: Analyses = {
        "R": both("A", "B"),
        "A": both("C", "D"),
        "B": both("E"),
        "C": both("F"),
    }
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert ids(chain) == ["R", "A", "B", "C", "D", "E", "F"]
    assert [n.depth for n in chain.nodes] == [0, 1, 1, 2, 2, 2, 3]


def test_edges_are_in_order_of_discovery() -> None:
    """Edges follow the expansion order: node by node in breadth-first order."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("C"), "B": both("D")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2)
    assert pairs(chain) == [("A", "R"), ("B", "R"), ("C", "A"), ("D", "B")]


def test_discovery_order_follows_reference_order() -> None:
    """Reversing the references reverses the sibling order."""
    forward = explore("R", {"R": both("A", "B", "C")}, direction="amends", max_depth=1)
    backward = explore("R", {"R": both("C", "B", "A")}, direction="amends", max_depth=1)
    assert ids(forward) == ["R", "A", "B", "C"]
    assert ids(backward) == ["R", "C", "B", "A"]


def test_node_keeps_its_shortest_depth_when_reached_twice() -> None:
    """C is reached at depth 1 (from R) and at depth 2 (from A): depth 1 stays."""
    analyses: Analyses = {"R": both("A", "C"), "A": both("C")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert node(chain, "C").depth == 1
    assert ids(chain).count("C") == 1
    assert ids(chain) == ["R", "A", "C"]


def test_node_found_first_deep_and_later_shallow_takes_the_shallow_depth() -> None:
    """Whichever order the paths are found in, the shortest distance wins."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("C"), "B": both("A")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    # A is at depth 1 although B (depth 1) also points to it; C only via A.
    assert node(chain, "A").depth == 1
    assert node(chain, "C").depth == 2
    deeper: Analyses = {"R": both("A", "B"), "A": both("C"), "B": both("C", "D"), "C": both("E")}
    chain = explore("R", deeper, direction="amended_by", max_depth=3)
    assert node(chain, "C").depth == 2
    assert node(chain, "E").depth == 3
    assert ids(chain) == ["R", "A", "B", "C", "D", "E"]


def test_a_shallow_node_is_expanded_by_its_shortest_depth() -> None:
    """Expansion depends on the shortest depth: C at depth 1 is expanded under max_depth 2."""
    analyses: Analyses = {"R": both("A", "C"), "A": both("C"), "C": both("Z")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2)
    assert node(chain, "C").expanded is True
    assert node(chain, "Z").depth == 2
    assert node(chain, "Z").reason == "depth_limit"


def test_a_node_first_seen_deep_is_limited_by_the_shorter_path() -> None:
    """D is a child of A (depth 1) and of C (depth 2): its depth 2 is not reduced or raised."""
    analyses: Analyses = {"R": both("A"), "A": both("C", "D"), "C": both("D")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert node(chain, "D").depth == 2
    assert node(chain, "C").depth == 2
    # D is not expanded twice, but both of its incoming edges exist.
    assert ("D", "A", "270") in triples(chain)
    assert ("D", "C", "270") in triples(chain)


def test_cycle_gives_one_node_each_and_both_edges() -> None:
    """A amended by B amended by A: two nodes, and the edge closing the cycle is kept."""
    analyses: Analyses = {
        "A": analysis(later=later("B")),
        "B": analysis(later=later("A")),
    }
    chain = explore("A", analyses, direction="amended_by", max_depth=3)
    assert ids(chain) == ["A", "B"]
    assert [n.depth for n in chain.nodes] == [0, 1]
    assert pairs(chain) == [("B", "A"), ("A", "B")]
    assert chain.to_fetch == ()
    assert chain.truncated is False
    assert all(n.expanded for n in chain.nodes)


def test_cycle_in_amends_direction() -> None:
    """The same cycle explored the other way round."""
    analyses: Analyses = {
        "A": analysis(earlier=earlier("B")),
        "B": analysis(earlier=earlier("A")),
    }
    chain = explore("A", analyses, direction="amends", max_depth=3)
    assert ids(chain) == ["A", "B"]
    assert pairs(chain) == [("A", "B"), ("B", "A")]


def test_longer_cycle_terminates() -> None:
    """A -> B -> C -> A with depth 3: every node once, three edges."""
    analyses = graph(A=["B"], B=["C"], C=["A"])
    chain = explore("A", analyses, direction="amended_by", max_depth=3)
    assert ids(chain) == ["A", "B", "C"]
    assert len(chain.edges) == 3


def test_cycle_edge_closing_through_an_unexpanded_node_is_not_seen() -> None:
    """With max_depth 1, B is not expanded, so the edge B -> A (read from B) is not seen."""
    analyses: Analyses = {
        "A": analysis(later=later("B")),
        "B": analysis(later=later("A")),
    }
    chain = explore("A", analyses, direction="amended_by", max_depth=1)
    assert pairs(chain) == [("B", "A")]
    assert node(chain, "B").reason == "depth_limit"


def test_self_reference_does_not_loop() -> None:
    """A law that lists itself still yields a finite chain with the root once."""
    chain = explore("A", {"A": both("A", "B")}, direction="amended_by", max_depth=2)
    assert ids(chain).count("A") == 1
    assert "B" in ids(chain)


def test_self_reference_is_ignored_and_first_duplicate_is_kept() -> None:
    """A law citing itself adds no edge; of equal (amending, amended, code) rows the first wins."""
    refs: list[Ref] = [
        ("A", "270", "SE MODIFICA", "self"),
        ("B", "270", "SE MODIFICA", "first"),
        ("B", "270", "SE MODIFICA", "second"),
    ]
    chain = explore("A", {"A": analysis(later=refs)}, direction="amended_by", max_depth=2)
    assert pairs(chain) == [("B", "A")]
    assert chain.edges[0].detail == "first"
    assert ids(chain) == ["A", "B"]


def test_diamond_has_one_node_and_two_edges() -> None:
    """R <- X, R <- Y, X <- Z, Y <- Z: Z is one node at depth 2 with two incoming edges."""
    analyses: Analyses = {"R": both("X", "Y"), "X": both("Z"), "Y": both("Z")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2)
    assert ids(chain) == ["R", "X", "Y", "Z"]
    assert node(chain, "Z").depth == 2
    assert pairs(chain) == [("X", "R"), ("Y", "R"), ("Z", "X"), ("Z", "Y")]
    assert chain.to_fetch == ()


def test_diamond_to_fetch_lists_the_shared_node_once() -> None:
    """An unexpanded node reached by two paths is asked for once."""
    analyses: Analyses = {"R": both("X", "Y"), "X": both("Z"), "Y": both("Z")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert chain.to_fetch == ("Z",)
    assert node(chain, "Z").reason == "not_fetched"


def test_edge_to_an_already_known_node_is_recorded() -> None:
    """Sibling edges: A (depth 1) lists B (depth 1); the A-B edge is kept."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("B")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2)
    assert ("B", "A", "270") in triples(chain)
    assert ids(chain) == ["R", "A", "B"]


def test_edge_back_to_the_root_is_recorded() -> None:
    """An edge into the root from a deeper node is kept."""
    analyses: Analyses = {"R": both("A"), "A": both("R")}
    chain = explore("R", analyses, direction="amends", max_depth=2)
    assert ("A", "R", "270") in triples(chain)
    assert ids(chain) == ["R", "A"]


# --- duplicate edges -----------------------------------------------------------------------------


def test_duplicate_reference_gives_one_edge() -> None:
    """The same (amending, amended, code) twice in one analysis is reported once."""
    refs = earlier("T") + earlier("T")
    chain = explore("R", {"R": analysis(earlier=refs)}, direction="amends", max_depth=1)
    assert triples(chain) == [("R", "T", "270")]
    assert ids(chain) == ["R", "T"]


def test_same_pair_with_a_different_code_gives_two_edges() -> None:
    """Different codes between the same two laws are different edges."""
    refs = earlier("T", code="270") + earlier("T", code="210")
    chain = explore("R", {"R": analysis(earlier=refs)}, direction="amends", max_depth=1)
    assert triples(chain) == [("R", "T", "270"), ("R", "T", "210")]
    assert [e.kind for e in chain.edges] == ["modifies", "repeals"]
    assert ids(chain) == ["R", "T"]


def test_edges_have_no_duplicates_in_a_larger_graph() -> None:
    """No (amending, amended, code) is repeated anywhere, cycles and diamonds included."""
    analyses = graph(R=["A", "B", "A"], A=["B", "R", "C"], B=["C", "A", "R"], C=["A", "R"])
    for direction in ("amended_by", "amends"):
        d: Any = direction
        chain = explore("R", analyses, direction=d, max_depth=3)
        keys = triples(chain)
        assert len(keys) == len(set(keys))
        assert ids(chain) == ["R", "A", "B", "C"]


# --- max_depth -----------------------------------------------------------------------------------


def _line() -> Analyses:
    """R <- A <- B <- C <- D, every analysis known."""
    return graph(R=["A"], A=["B"], B=["C"], C=["D"], D=["E"])


@pytest.mark.parametrize(
    ("depth", "expected"),
    [(1, ["R", "A"]), (2, ["R", "A", "B"]), (3, ["R", "A", "B", "C"])],
)
def test_max_depth_levels_followed_from_the_root(depth: int, expected: list[str]) -> None:
    """``max_depth`` is the number of levels of references followed."""
    chain = explore("R", _line(), direction="amended_by", max_depth=depth)
    assert ids(chain) == expected
    assert [n.depth for n in chain.nodes] == list(range(depth + 1))
    assert len(chain.edges) == depth
    last = chain.nodes[-1]
    assert last == ChainNode(expected[-1], depth, False, "depth_limit")
    assert all(n.expanded for n in chain.nodes[:-1])


def test_max_depth_defaults_to_two() -> None:
    """Without ``max_depth`` the chain goes two levels."""
    chain = explore("R", _line(), direction="amended_by")
    assert ids(chain) == ["R", "A", "B"]


def test_max_depth_one_does_not_expand_children() -> None:
    """Even a child whose analysis is known stays unexpanded at depth 1."""
    chain = explore("R", _line(), direction="amends", max_depth=1)
    assert node(chain, "A").expanded is False
    assert chain.to_fetch == ()


@pytest.mark.parametrize("depth", [0, -1, 4, 10])
def test_max_depth_out_of_range_raises(depth: int) -> None:
    """Only 1..3 are accepted."""
    with pytest.raises(ValueError):
        explore("R", {}, direction="amended_by", max_depth=depth)


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_max_nodes_below_one_raises(limit: int) -> None:
    """``max_nodes`` must be at least 1."""
    with pytest.raises(ValueError):
        explore("R", {}, direction="amended_by", max_nodes=limit)


def test_max_nodes_of_one_is_valid() -> None:
    """The smallest legal limit is accepted."""
    chain = explore("R", {}, direction="amended_by", max_nodes=1)
    assert ids(chain) == ["R"]


# --- to_fetch ------------------------------------------------------------------------------------


def test_to_fetch_is_breadth_first_and_complete() -> None:
    """Unexpanded fetchable nodes below the depth limit, by depth then discovery."""
    analyses: Analyses = {"R": both("A", "B", "C"), "B": both("D", "E")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert chain.to_fetch == ("A", "C", "D", "E")
    assert [node(chain, i).reason for i in chain.to_fetch] == ["not_fetched"] * 4


def test_to_fetch_excludes_expanded_unavailable_and_depth_limit_nodes() -> None:
    """Only ``not_fetched`` nodes are listed."""
    analyses: Analyses = {
        "R": both("A", "B", "C", "OUT"),
        "A": both("D"),
        "B": None,
    }
    chain = explore(
        "R",
        analyses,
        direction="amends",
        max_depth=2,
        fetchable=lambda law_id: law_id != "OUT",
    )
    assert chain.to_fetch == ("C",)
    assert node(chain, "D").reason == "depth_limit"
    assert node(chain, "B").reason == "unavailable"
    assert node(chain, "OUT").reason == "unavailable"


def test_to_fetch_matches_the_not_fetched_nodes() -> None:
    """``to_fetch`` is exactly the ``not_fetched`` nodes, in node order."""
    analyses: Analyses = {"R": both("A", "B", "C", "D"), "C": both("E")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert chain.to_fetch == tuple(n.law_id for n in chain.nodes if n.reason == "not_fetched")


def test_to_fetch_holds_no_duplicates() -> None:
    """A node is listed once even when many laws point at it."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("C"), "B": both("C")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert chain.to_fetch == ("C",)


def test_root_is_listed_first_when_unknown() -> None:
    """Fetching starts with the root, and nothing else can be listed before it is known."""
    chain = explore("R", {}, direction="amends", max_depth=3)
    assert chain.to_fetch == ("R",)


# --- max_nodes -----------------------------------------------------------------------------------


def test_limit_reached_exactly_is_not_truncated() -> None:
    """Filling the limit without dropping anything leaves ``truncated`` False."""
    analyses: Analyses = {"R": both("A", "B")}
    chain = explore("R", analyses, direction="amended_by", max_depth=1, max_nodes=3)
    assert ids(chain) == ["R", "A", "B"]
    assert chain.truncated is False
    assert len(chain.edges) == 2


def test_one_more_than_the_limit_is_truncated() -> None:
    """A reference that would add node number limit + 1 is dropped and flagged."""
    analyses: Analyses = {"R": both("A", "B", "C")}
    chain = explore("R", analyses, direction="amended_by", max_depth=1, max_nodes=3)
    assert ids(chain) == ["R", "A", "B"]
    assert chain.truncated is True


def test_edges_to_dropped_nodes_are_not_recorded() -> None:
    """The edge to the dropped C is absent; the kept ones remain."""
    analyses: Analyses = {"R": both("A", "B", "C")}
    chain = explore("R", analyses, direction="amends", max_depth=1, max_nodes=3)
    assert pairs(chain) == [("R", "A"), ("R", "B")]
    assert all("C" not in p for p in pairs(chain))


def test_dropped_nodes_are_not_fetched() -> None:
    """A dropped id never reaches ``to_fetch``."""
    analyses: Analyses = {"R": both("A", "B", "C")}
    chain = explore("R", analyses, direction="amends", max_depth=2, max_nodes=3)
    assert chain.to_fetch == ("A", "B")


def test_limit_of_one_keeps_the_root_only() -> None:
    """With ``max_nodes=1`` every reference is dropped."""
    chain = explore("R", {"R": both("A")}, direction="amended_by", max_nodes=1)
    assert ids(chain) == ["R"]
    assert chain.edges == ()
    assert chain.truncated is True
    assert chain.to_fetch == ()


def test_limit_of_one_without_references_is_not_truncated() -> None:
    """Nothing to drop, nothing truncated."""
    chain = explore("R", {"R": analysis()}, direction="amended_by", max_nodes=1)
    assert chain.truncated is False


def test_no_new_node_after_the_limit_across_levels() -> None:
    """Deeper expansions also stop adding nodes once the limit is reached."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("C"), "B": both("D")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2, max_nodes=4)
    assert ids(chain) == ["R", "A", "B", "C"]
    assert chain.truncated is True
    assert pairs(chain) == [("A", "R"), ("B", "R"), ("C", "A")]


def test_edges_between_known_nodes_survive_the_limit() -> None:
    """At the limit, a reference to a node already in the chain is still an edge, not a drop."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("B", "R")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2, max_nodes=3)
    assert ids(chain) == ["R", "A", "B"]
    assert chain.truncated is False
    assert ("B", "A", "270") in triples(chain)
    assert ("R", "A", "270") in triples(chain)


def test_expanding_a_node_that_adds_nothing_new_at_the_limit_is_fine() -> None:
    """A node whose references are all known is expanded even when the limit is full."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("R", "B"), "B": analysis()}
    chain = explore("R", analyses, direction="amended_by", max_depth=2, max_nodes=3)
    assert node(chain, "A") == ChainNode("A", 1, True, "")
    assert node(chain, "B") == ChainNode("B", 1, True, "")
    assert chain.truncated is False


def test_unexpanded_nodes_never_get_a_foreign_reason_under_a_limit() -> None:
    """Under a limit every node still has one of the five known reasons and consistent flags."""
    analyses: Analyses = {"R": both("A", "B", "C"), "A": both("D", "E"), "B": both("F")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2, max_nodes=4)
    assert chain.truncated is True
    for n in chain.nodes:
        assert n.reason in REASONS
        assert n.expanded == (n.reason == "")
    assert chain.nodes[0].expanded is True


def _wide() -> Analyses:
    return {
        "R": both("A", "B", "C"),
        "A": both("D", "E", "B"),
        "B": both("F", "R"),
        "C": both("D", "G"),
        "D": both("H", "A"),
        "E": both("H"),
    }


@pytest.mark.parametrize("limit", range(1, 12))
@pytest.mark.parametrize("direction", ["amended_by", "amends"])
def test_limit_invariants(direction: str, limit: int) -> None:
    """Whatever the limit: a prefix of the unlimited nodes, consistent edges, honest flag."""
    d: Any = direction
    full = explore("R", _wide(), direction=d, max_depth=3)
    chain = explore("R", _wide(), direction=d, max_depth=3, max_nodes=limit)
    assert len(chain.nodes) <= limit
    assert ids(chain) == ids(full)[:limit]
    assert [n.depth for n in chain.nodes] == [n.depth for n in full.nodes][:limit]
    known = set(ids(chain))
    assert all(e.amending in known and e.amended in known for e in chain.edges)
    full_edges = list(full.edges)
    assert all(e in full_edges for e in chain.edges)
    positions = [full_edges.index(e) for e in chain.edges]
    assert positions == sorted(positions)
    assert known.issuperset(chain.to_fetch)
    if len(full.nodes) <= limit:
        # Nothing was dropped, so the result is the unlimited one.
        assert chain == full
        assert chain.truncated is False
    else:
        assert len(chain.nodes) == limit
        # SPEC AMBIGUITY (explore, truncated): when nodes exist beyond the limit, some reference
        # to one of them was dropped by an expanded node, so ``truncated`` must be True.
        assert chain.truncated is True


# --- determinism ---------------------------------------------------------------------------------


def test_explore_is_deterministic() -> None:
    """The same inputs give equal chains."""
    first = explore("R", _wide(), direction="amended_by", max_depth=3)
    second = explore("R", _wide(), direction="amended_by", max_depth=3)
    assert first == second


def test_mapping_order_does_not_matter() -> None:
    """Discovery follows the references, not the insertion order of the mapping."""
    forward = _wide()
    backward = dict(reversed(list(forward.items())))
    for direction in ("amended_by", "amends"):
        d: Any = direction
        assert explore("R", forward, direction=d, max_depth=3) == explore(
            "R", backward, direction=d, max_depth=3
        )


def test_explore_does_not_mutate_its_inputs() -> None:
    """The mapping is only read."""
    analyses = _wide()
    before = dict(analyses)
    explore("R", analyses, direction="amended_by", max_depth=3)
    assert analyses == before


def test_results_are_immutable_value_objects() -> None:
    """Chain, nodes and edges are frozen dataclasses holding tuples."""
    chain = explore("R", {"R": both("A")}, direction="amended_by", max_depth=1)
    assert isinstance(chain.nodes, tuple)
    assert isinstance(chain.edges, tuple)
    assert isinstance(chain.to_fetch, tuple)
    with pytest.raises(AttributeError):
        chain.root = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        chain.nodes[0].depth = 5  # type: ignore[misc]
    with pytest.raises(AttributeError):
        ChainEdge.__setattr__(chain.edges[0], "code", "1")


# --- level_counts --------------------------------------------------------------------------------


def test_level_counts_per_depth() -> None:
    """Nodes per depth, root first."""
    analyses: Analyses = {"R": both("A", "B"), "A": both("C", "D", "E"), "B": both("F")}
    chain = explore("R", analyses, direction="amended_by", max_depth=2)
    assert level_counts(chain) == (1, 2, 4)


def test_level_counts_root_only() -> None:
    """A lone root is ``(1,)``."""
    assert level_counts(explore("R", {}, direction="amends")) == (1,)


def test_level_counts_sum_to_the_node_count() -> None:
    """The counts add up to the nodes and follow the node depths."""
    chain = explore("R", _wide(), direction="amended_by", max_depth=3)
    counts = level_counts(chain)
    assert sum(counts) == len(chain.nodes)
    for depth, count in enumerate(counts):
        assert count == sum(1 for n in chain.nodes if n.depth == depth)
    assert isinstance(counts, tuple)


def test_level_counts_count_shared_nodes_once() -> None:
    """A diamond's shared node is counted at its shortest depth only."""
    analyses: Analyses = {"R": both("X", "Y", "Z"), "X": both("Z"), "Y": both("Z", "W")}
    chain = explore("R", analyses, direction="amended_by", max_depth=3)
    assert level_counts(chain) == (1, 3, 1)


# --- iterative use: fetch what to_fetch asks for, one at a time ----------------------------------


def _store() -> Analyses:
    """A small world with a cycle, a diamond, a dead end, a missing law and an outside id."""
    corr: Ref = ("COR", "201", "CORRECCION", "d")
    return {
        "R": analysis(earlier=[*earlier("A", "B"), corr], later=[*later("A", "B"), corr]),
        "A": both("C", "D", "OUT"),
        "B": both("C", "R"),
        "C": both("E"),
        "D": None,
        "E": both("F", "A"),
        "F": both("G"),
        "G": both("H"),
        "H": analysis(),
    }


def _outside(law_id: str) -> bool:
    return law_id != "OUT"


def _converge(
    root: str,
    store: Mapping[str, LawAnalysis | None],
    **options: Any,
) -> tuple[Chain, list[str]]:
    """Start with the root's analysis only and feed ``to_fetch`` one id at a time."""
    known: Analyses = {root: store[root]}
    fetched: list[str] = []
    chain = explore(root, known, **options)
    for _ in range(200):
        if not chain.to_fetch:
            return chain, fetched
        next_id = chain.to_fetch[0]
        assert next_id not in known, "an id already known was asked for again"
        known[next_id] = store.get(next_id)
        fetched.append(next_id)
        before_nodes, before_edges = set(ids(chain)), set(chain.edges)
        chain = explore(root, known, **options)
        if "max_nodes" not in options:
            # Without a limit, more knowledge can only add nodes and edges.
            assert before_nodes <= set(ids(chain))
            assert before_edges <= set(chain.edges)
    raise AssertionError("the chain did not converge")


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("direction", ["amended_by", "amends"])
def test_iterative_use_converges_to_the_one_shot_result(direction: str, depth: int) -> None:
    """Feeding analyses one at a time ends where a single call with everything known ends."""
    store = _store()
    options: dict[str, Any] = {"direction": direction, "max_depth": depth, "fetchable": _outside}
    converged, fetched = _converge("R", store, **options)
    once = explore("R", store, **options)
    assert converged == once
    assert converged.to_fetch == ()
    assert len(fetched) == len(set(fetched))
    assert set(fetched) <= set(ids(converged))
    assert "OUT" not in fetched
    assert "COR" not in fetched


def test_iterative_use_with_a_small_budget_makes_partial_progress() -> None:
    """After only some fetches the chain is valid and still asks for the rest."""
    store = _store()
    known: Analyses = {"R": store["R"]}
    chain = explore("R", known, direction="amended_by", max_depth=3, fetchable=_outside)
    assert chain.to_fetch == ("A", "B")
    known["A"] = store["A"]
    chain = explore("R", known, direction="amended_by", max_depth=3, fetchable=_outside)
    assert chain.to_fetch == ("B", "C", "D")
    assert node(chain, "OUT").reason == "unavailable"
    assert node(chain, "A").expanded is True
    assert node(chain, "B").reason == "not_fetched"


def test_iterative_use_discovers_in_breadth_first_order() -> None:
    """Fetch order is breadth first: all depth-1 laws before any depth-2 law."""
    store = _store()
    _, fetched = _converge("R", store, direction="amended_by", max_depth=3, fetchable=_outside)
    assert fetched == ["A", "B", "C", "D"]


def test_iterative_use_with_an_unavailable_root() -> None:
    """If the root itself is unavailable the loop ends at once with the lone root."""
    chain, fetched = _converge("R", {"R": None}, direction="amends")
    assert fetched == []
    assert chain.nodes == (ChainNode("R", 0, False, "unavailable"),)


# --- real data -----------------------------------------------------------------------------------

RDL = "BOE-A-2020-3824"
LEY11 = "BOE-A-2002-8628"
LEY19 = "BOE-A-2003-13471"
LEY14 = "BOE-A-2011-9617"
ORDEN = "BOE-A-2015-2495"
BOJA = "BOJA-b-2022-90314"

_FILES = {
    RDL: "analisis_rdl8_2020_BOE-A-2020-3824.json",
    LEY11: "analisis_ley11_2002_BOE-A-2002-8628.json",
    LEY19: "analisis_ley19_2003_BOE-A-2003-13471.json",
    LEY14: "analisis_ley14_2011_BOE-A-2011-9617.json",
    ORDEN: "analisis_orden_2015_BOE-A-2015-2495.json",
    BOJA: "analisis_boja_b_2022_90314.json",
}


def _load(law_id: str) -> LawAnalysis:
    """Build the analysis the way the client does: first record of ``data`` through the model."""
    body = json.loads((FIXTURE_DIR / _FILES[law_id]).read_text("utf-8"))
    return LawAnalysis.model_validate(as_list(body["data"])[0])


@pytest.fixture(scope="module")
def real() -> dict[str, LawAnalysis]:
    """The six recorded analyses."""
    return {law_id: _load(law_id) for law_id in _FILES}


def _in_collection(law_id: str) -> bool:
    return law_id.startswith("BOE-A-")


def test_real_rdl_amends_depth_one(real: dict[str, LawAnalysis]) -> None:
    """RDL 8/2020 amends eight laws (5 modify, 1 repeals, 2 suspend); 401 and 330 are not."""
    chain = explore(RDL, {RDL: real[RDL]}, direction="amends", max_depth=1)
    assert [e.amended for e in chain.edges] == [
        "BOE-A-2020-3580",
        "BOE-A-2018-9268",
        LEY19,
        LEY11,
        "BOE-A-1993-25359",
        LEY14,
        ORDEN,
        "BOE-A-2009-10329",
    ]
    assert {e.amending for e in chain.edges} == {RDL}
    assert [e.kind for e in chain.edges] == ["modifies"] * 5 + ["repeals"] + ["suspends"] * 2
    assert [e.code for e in chain.edges] == ["270"] * 5 + ["235", "231", "231"]
    assert level_counts(chain) == (1, 8)
    assert all(n.reason == "depth_limit" for n in chain.nodes[1:])
    assert chain.to_fetch == ()
    assert chain.truncated is False
    assert "BOE-A-2017-11505" not in ids(chain)
    assert "BOE-A-2020-3692" not in ids(chain)


def test_real_rdl_amends_edge_to_ley11(real: dict[str, LawAnalysis]) -> None:
    """The edge to Ley 11/2002 is a modification of art. 6.2, in the active voice."""
    chain = explore(RDL, {RDL: real[RDL]}, direction="amends", max_depth=1)
    (edge,) = [e for e in chain.edges if e.amended == LEY11]
    assert edge.amending == RDL
    assert edge.kind == "modifies"
    assert edge.code == "270"
    assert edge.relation == "MODIFICA"
    assert "art. 6.2" in edge.detail
    assert "Ley 11/2002" in edge.detail


def test_real_rdl_amends_with_all_kinds(real: dict[str, LawAnalysis]) -> None:
    """With every kind all ten ``anteriores`` rows are edges, and 401/330 are citations."""
    chain = explore(RDL, {RDL: real[RDL]}, direction="amends", max_depth=1, kinds=ALL_KINDS)
    assert len(chain.edges) == 10
    by_target = {e.amended: e for e in chain.edges}
    assert by_target["BOE-A-2017-11505"].kind == "cites"
    assert by_target["BOE-A-2020-3692"].kind == "cites"
    only = explore(RDL, {RDL: real[RDL]}, direction="amends", max_depth=1, kinds={"cites"})
    assert [e.amended for e in only.edges] == ["BOE-A-2017-11505", "BOE-A-2020-3692"]


def test_real_ley11_amended_by_depth_one(real: dict[str, LawAnalysis]) -> None:
    """Four norms amended Ley 11/2002; the 440, 331, 470 and 552 rows are not amendments."""
    chain = explore(LEY11, {LEY11: real[LEY11]}, direction="amended_by", max_depth=1)
    assert [e.amending for e in chain.edges] == [
        RDL,
        "BOE-A-2008-20744",
        "BOE-A-2007-19880",
        "BOE-A-2003-23936",
    ]
    assert {e.amended for e in chain.edges} == {LEY11}
    assert {e.kind for e in chain.edges} == {"modifies"}
    assert ids(chain)[0] == LEY11
    assert level_counts(chain) == (1, 4)


@pytest.mark.parametrize(
    ("target", "code", "kind", "active", "passive"),
    [
        (LEY11, "270", "modifies", "MODIFICA", "SE MODIFICA"),
        (LEY19, "270", "modifies", "MODIFICA", "SE MODIFICA"),
        (LEY14, "235", "repeals", "SUPRIME", "SE SUPRIME"),
        (ORDEN, "231", "suspends", "SUSPENDE", "SE SUSPENDE"),
    ],
)
def test_real_amendments_mirror_between_the_two_directions(
    real: dict[str, LawAnalysis],
    target: str,
    code: str,
    kind: str,
    active: str,
    passive: str,
) -> None:
    """RDL 8/2020 -> target read as ``amends`` mirrors target <- RDL read as ``amended_by``."""
    forward = explore(RDL, {RDL: real[RDL]}, direction="amends", max_depth=1)
    backward = explore(target, {target: real[target]}, direction="amended_by", max_depth=1)
    (out,) = [e for e in forward.edges if e.amended == target]
    (back,) = [e for e in backward.edges if e.amending == RDL]
    assert (out.amending, out.amended, out.code, out.kind) == (RDL, target, code, kind)
    assert (back.amending, back.amended, back.code, back.kind) == (RDL, target, code, kind)
    assert out.relation == active
    assert back.relation == passive
    # The detail is written from the viewpoint of the law that was read.
    assert out.detail != back.detail
    assert "Real Decreto-ley 8/2020" in back.detail


def test_real_mirrored_detail_mentions_the_article(real: dict[str, LawAnalysis]) -> None:
    """Both sides of the Ley 11/2002 amendment mention art. 6.2."""
    backward = explore(LEY11, {LEY11: real[LEY11]}, direction="amended_by", max_depth=1)
    (back,) = [e for e in backward.edges if e.amending == RDL]
    assert "art. 6.2" in back.detail


def test_real_orden_amended_by_has_two_suspensions(real: dict[str, LawAnalysis]) -> None:
    """The Orden has three amending rows (270, 231, 231) and one 440 that is dropped."""
    chain = explore(ORDEN, {ORDEN: real[ORDEN]}, direction="amended_by", max_depth=1)
    assert [e.code for e in chain.edges] == ["270", "231", "231"]
    assert [e.kind for e in chain.edges] == ["modifies", "suspends", "suspends"]
    assert [e.amending for e in chain.edges] == ["BOE-A-2025-4410", "BOE-A-2020-4208", RDL]
    assert "BOE-A-2016-8534" not in ids(chain)


def test_real_depth_two_amends_with_five_known_analyses(real: dict[str, LawAnalysis]) -> None:
    """From RDL 8/2020 with its five amended laws known: expanded, to_fetch and depth limit."""
    known: Analyses = {k: real[k] for k in (RDL, LEY11, LEY19, LEY14, ORDEN)}
    chain = explore(RDL, known, direction="amends", max_depth=2, fetchable=_in_collection)
    assert chain.to_fetch == (
        "BOE-A-2020-3580",
        "BOE-A-2018-9268",
        "BOE-A-1993-25359",
        "BOE-A-2009-10329",
    )
    for law_id in (LEY11, LEY19, LEY14, ORDEN):
        assert node(chain, law_id).expanded is True
    assert node(chain, "BOE-A-2020-3580").reason == "not_fetched"
    # Ley 19/2003 adds provisions to two laws (407) and repeals three (210).
    by_edge = {(e.amending, e.amended, e.code): e for e in chain.edges}
    assert by_edge[(LEY19, "BOE-A-2002-25286", "407")].kind == "adds"
    assert by_edge[(LEY19, "BOE-A-1999-21980", "210")].kind == "repeals"
    # The 440 of Ley 11/2002 is a citation: not followed.
    assert "BOE-A-1997-7878" not in ids(chain)
    # Depth-2 nodes are exactly the new targets, all at the depth limit.
    amending_codes = {"270", "210", "235", "231", "407"}
    expected: set[str] = set()
    for law_id in (LEY11, LEY19, LEY14, ORDEN):
        expected.update(r.law_id for r in real[law_id].earlier if r.relation.code in amending_codes)
    expected -= set(ids(chain)[:9])
    level_two = {n.law_id for n in chain.nodes if n.depth == 2}
    assert level_two == expected
    assert all(node(chain, i).reason == "depth_limit" for i in level_two)
    assert level_counts(chain)[:2] == (1, 8)
    assert sum(level_counts(chain)) == len(chain.nodes)


def test_real_regional_law_amends_and_amended_by(real: dict[str, LawAnalysis]) -> None:
    """The regional analysis: two amending rows out, only a citation (301) in."""
    known: Analyses = {BOJA: real[BOJA]}
    out = explore(BOJA, known, direction="amends", max_depth=1)
    assert [(e.amending, e.amended, e.kind) for e in out.edges] == [
        (BOJA, "BOE-A-2022-757", "repeals"),
        (BOJA, "BOE-A-2018-6938", "modifies"),
    ]
    incoming = explore(BOJA, known, direction="amended_by", max_depth=1)
    assert ids(incoming) == [BOJA]
    assert incoming.edges == ()
    citations = explore(BOJA, known, direction="amended_by", max_depth=2, kinds={"cites"})
    (edge,) = citations.edges
    assert (edge.amending, edge.amended, edge.code, edge.kind) == (
        "BOJA-b-2022-90339",
        BOJA,
        "301",
        "cites",
    )
    assert edge.relation == real[BOJA].later[0].relation.text
    assert node(citations, "BOJA-b-2022-90339").reason == "not_fetched"
    assert citations.to_fetch == ("BOJA-b-2022-90339",)
    outside = explore(
        BOJA, known, direction="amended_by", max_depth=2, kinds={"cites"}, fetchable=_in_collection
    )
    assert node(outside, "BOJA-b-2022-90339").reason == "unavailable"
    assert outside.to_fetch == ()


def _complete(real: dict[str, LawAnalysis]) -> Analyses:
    """The six analyses plus an explicit ``None`` for every other id they reference."""
    store: Analyses = dict(real)
    for analysis_ in real.values():
        for ref in [*analysis_.earlier, *analysis_.later]:
            if ref.law_id:
                store.setdefault(ref.law_id, None)
    return store


@pytest.mark.parametrize("root", [RDL, LEY11, LEY14, BOJA])
@pytest.mark.parametrize("direction", ["amended_by", "amends"])
@pytest.mark.parametrize("depth", [1, 2, 3])
def test_real_iterative_use_converges(
    real: dict[str, LawAnalysis], root: str, direction: str, depth: int
) -> None:
    """On real data, one-at-a-time fetching reaches the same chain as knowing everything."""
    store = _complete(real)
    options: dict[str, Any] = {"direction": direction, "max_depth": depth}
    converged, fetched = _converge(root, store, **options)
    assert converged == explore(root, store, **options)
    assert converged.to_fetch == ()
    assert len(fetched) == len(set(fetched))


def test_real_iterative_use_with_all_kinds_and_a_limit(real: dict[str, LawAnalysis]) -> None:
    """The limit and the kinds filter do not break convergence on real data."""
    store = _complete(real)
    options: dict[str, Any] = {
        "direction": "amends",
        "max_depth": 3,
        "kinds": ALL_KINDS,
        "fetchable": _in_collection,
        "max_nodes": 40,
    }
    converged, _ = _converge(RDL, store, **options)
    once = explore(RDL, store, **options)
    # With a limit, the nodes admitted depend on which analyses are known when the limit is hit,
    # so only the invariants are asserted, not equality.
    assert len(converged.nodes) <= 40
    assert len(once.nodes) <= 40
    assert converged.nodes[0] == ChainNode(RDL, 0, True, "")
