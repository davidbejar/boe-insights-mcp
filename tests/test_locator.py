"""Contract tests for ``boe_insights_mcp.locator``.

``locate`` finds the block of a law's text index that a person means by "artículo 46 bis",
"DA 3" or "exposición de motivos". The first half of this module runs against the real indexes
recorded in ``tests/fixtures/boe/texto_indice_*.json`` (the authority on what titles and block
ids look like); the second half builds small synthetic indexes for the rules the fixtures
cannot show. Only the public interface is used: no canonical form and no similarity score is
asserted beyond what the spec states.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from boe_insights_mcp.locator import LocateResult, locate, locate_block_id
from boe_insights_mcp.models import IndexEntry

FIXTURES = {
    "constitucion": "texto_indice_constitucion_1978.json",
    "codigo_civil": "texto_indice_codigo_civil_trimmed.json",
    "iva": "texto_indice_iva_1992_trimmed.json",
    "ley30": "texto_indice_ley30_1992_repealed_trimmed.json",
    "ley39": "texto_indice_ley39_2015.json",
    "ley40": "texto_indice_ley40_2015_trimmed.json",
    "ley49": "texto_indice_ley49_1960_ordinals.json",
    "lopd": "texto_indice_lopd1999_repealed_with_no.json",
    "rd1006": "texto_indice_rd1006_2026_full.json",
    "rd1624": "texto_indice_rd1624_1992_dup_articulo1.json",
    "rd424": "texto_indice_rd424_2005_nota_no_repeal.json",
    "rd784": "texto_indice_rd784_2021_bracket_titles.json",
}
ALL_LAWS = list(FIXTURES)

IndexLoader = Callable[[str], list[IndexEntry]]


# --- Helpers ----------------------------------------------------------------------------


def _blocks_of(payload: Any) -> list[dict[str, Any]]:
    """Flatten the index envelope ``data -> [{"bloque": [...]}]`` (lists may be collapsed)."""
    data = payload["data"]
    items = data if isinstance(data, list) else [data]
    blocks: list[dict[str, Any]] = []
    for item in items:
        inner = item["bloque"] if isinstance(item, dict) and "bloque" in item else item
        blocks.extend(inner if isinstance(inner, list) else [inner])
    return blocks


@pytest.fixture
def index_of(load_fixture: Callable[[str], bytes]) -> IndexLoader:
    """Return a function turning a short law name into the real ``list[IndexEntry]``."""

    def _load(law: str) -> list[IndexEntry]:
        payload = json.loads(load_fixture(FIXTURES[law]))
        return [IndexEntry.model_validate(raw) for raw in _blocks_of(payload)]

    return _load


def _entries(*titles: str) -> list[IndexEntry]:
    """Synthetic index; block ids are ``b0``, ``b1``... (never equal to a plausible query)."""
    return [IndexEntry(block_id=f"b{i}", title=t) for i, t in enumerate(titles)]


def _pairs(*pairs: tuple[str, str]) -> list[IndexEntry]:
    """Synthetic index from explicit ``(block_id, title)`` pairs."""
    return [IndexEntry(block_id=i, title=t) for i, t in pairs]


def _ids(entries: Sequence[IndexEntry]) -> list[str]:
    return [e.block_id for e in entries]


def _found_id(index: Sequence[IndexEntry], query: str) -> str:
    """Locate ``query`` and assert it is a unique hit; return the block id."""
    result = locate(index, query)
    assert result.outcome == "found", (query, result.outcome, _ids(result.candidates))
    assert result.entry is not None
    return result.entry.block_id


# --- Real indexes: queries that must find exactly one block ----------------------------

# (law, query, expected block id), every id verified against the fixture.
REAL_FOUND: list[tuple[str, str, str]] = [
    # Ley 40/2015: "Artículo 46 bis" is block a4-2, not a46.
    ("ley40", "46 bis", "a4-2"),
    ("ley40", "artículo 46bis", "a4-2"),
    ("ley40", "Artículo 46 bis.", "a4-2"),
    ("ley40", "artículo 108 ter", "a1-3"),
    ("ley40", "artículo 48", "a48"),
    ("ley40", "disposición final décima", "dfdecima"),
    ("ley40", "preámbulo", "preambulo"),
    ("ley40", "co", "co"),  # untitled block, found by its id only
    # Constitución.
    ("constitucion", "artículo 14", "a14"),
    ("constitucion", "Disposición adicional primera", "primera"),
    ("constitucion", "DA 1", "primera"),
    ("constitucion", "disposición transitoria primera", "primera-2"),
    ("constitucion", "disposición derogatoria", "dd"),
    ("constitucion", "Disposición final", "df"),
    ("constitucion", "preámbulo", "preambulo"),
    ("constitucion", "primera", "primera"),  # no title is just "Primera": the id is the answer
    # Ley 39/2015 (NBSP in most article titles).
    ("ley39", "artículo 1", "a1"),
    ("ley39", "disposición adicional 4", "dacuaa"),
    ("ley39", "disposición derogatoria única", "ddunica"),
    ("ley39", "exposición de motivos", "preambulo"),
    # Ley 30/1992 (repealed): one provision of each type, so no ordinal is needed.
    ("ley30", "disposición transitoria", "dttercera"),
    ("ley30", "no", "no"),
    # Ley 49/1960: ordinal and cardinal words for articles.
    ("ley49", "artículo 22", "aveintidos"),
    ("ley49", "artículo veintidós", "aveintidos"),
    ("ley49", "artículo primero", "aprimero"),
    ("ley49", "disposición adicional primera", "da-2"),
    ("ley49", "disposición final", "df"),
    # Real Decreto 1624/1992: unique articles, suffixes with a trailing period, annex.
    ("rd1624", "1 bis", "a1-3"),
    ("rd1624", "61 quáter", "a61quater"),
    ("rd1624", "disposición transitoria cuarta", "dt"),
    ("rd1624", "a1-2", "a1-2"),
    # Real Decreto 1006/2026: ids that are also abbreviations.
    ("rd1006", "disposición derogatoria única", "dd"),
    ("rd1006", "preámbulo", "pr"),
    # Real Decreto 784/2021: bracketed preamble, sole article plus a Reglamento.
    ("rd784", "artículo único", "au"),
    ("rd784", "artículo 1", "a1"),
    # Real Decreto 424/2005.
    ("rd424", "artículo único", "aunico"),
    ("rd424", "disposición final única", "dfunica"),
    # Here the range blocks coexist with one block per article: the exact article wins.
    ("rd424", "118", "a118"),
    ("rd424", "arts. 102 a 104", "as102a104"),
    # IVA law: latin suffixes (including the spelling "cuarter"), ranges, annex.
    ("iva", "140 quinque", "a140quinque"),
    ("iva", "140 quinquies", "a140quinque"),
    ("iva", "140 quater", "a140cuarter"),
    ("iva", "163 decies", "a163decies"),
    ("iva", "166 quater", "a1-9"),  # the real title has a trailing space
    ("iva", "anexo", "an"),
    ("iva", "DT 13", "dtdecimotercera"),
    # Código Civil: "Art N", ranges, § and bare ordinals.
    ("codigo_civil", "1", "a1"),
    ("codigo_civil", "artículo 1", "a1"),
    ("codigo_civil", "94 bis", "a9"),
    ("codigo_civil", "958 bis", "a"),
    ("codigo_civil", "128", "as127a130"),
    ("codigo_civil", "310", "arts307al313"),
    ("codigo_civil", "1250", "art.1231a1253"),
    ("codigo_civil", "1870", "arts.1863bisa1873bis."),
    ("codigo_civil", "1873 bis", "arts.1863bisa1873bis."),
    ("codigo_civil", "§ 1", "S1"),
    ("codigo_civil", "décima", "decima"),
    # LOPD 1999 (repealed).
    ("lopd", "disposición derogatoria única", "ddunica"),
]


@pytest.mark.parametrize(("law", "query", "expected_id"), REAL_FOUND)
def test_real_index_query_finds_the_expected_block(
    index_of: IndexLoader, law: str, query: str, expected_id: str
) -> None:
    index = index_of(law)
    result = locate(index, query)

    assert result.outcome == "found", (result.outcome, _ids(result.candidates))
    assert result.entry is not None
    assert result.entry.block_id == expected_id
    assert result.entry in index
    assert result.candidates == ()


def test_real_exact_article_beats_the_range_that_also_contains_it(index_of: IndexLoader) -> None:
    # Ambiguity in the spec: "Artículo 163 quater" is both its own block and inside the range
    # block "Artículos 163 bis a 163 quáter". The exact title is the more specific answer.
    index = index_of("iva")

    assert _found_id(index, "artículo 163 quater") == "a163quater"


@pytest.mark.parametrize("query", ["163 bis", "163 ter", "Artículos 163 bis a 163 quáter"], ids=str)
def test_real_range_block_answers_for_articles_without_their_own_block(
    index_of: IndexLoader, query: str
) -> None:
    index = index_of("iva")

    assert _found_id(index, query) == "as163bisa163quater"


# --- Real indexes: duplicates are ambiguous -------------------------------------------

REAL_AMBIGUOUS: list[tuple[str, str, list[str]]] = [
    ("rd1624", "artículo 1", ["a1", "a1-2"]),
    ("rd1624", "1", ["a1", "a1-2"]),
    ("rd1624", "Art. 2", ["a2", "a2-2"]),
    ("rd1624", "artículo 3", ["a3", "a3-2"]),
    ("rd424", "disposición adicional única", ["daunica", "daunica-2"]),
    ("rd424", "disposición transitoria primera", ["dtprimera", "dtprimera-2"]),
    ("rd424", "DT 2", ["dtsegunda", "dtsegunda-2"]),
    ("codigo_civil", "Primera", ["primera", "primera-2"]),
    ("codigo_civil", "primera", ["primera", "primera-2"]),  # as ambiguous as "Primera"
    ("codigo_civil", "SEGUNDA", ["segunda", "segunda-2"]),
]


@pytest.mark.parametrize(("law", "query", "expected_ids"), REAL_AMBIGUOUS)
def test_real_duplicate_designations_are_ambiguous_in_index_order(
    index_of: IndexLoader, law: str, query: str, expected_ids: list[str]
) -> None:
    result = locate(index_of(law), query)

    assert result.outcome == "ambiguous"
    assert result.entry is None
    assert _ids(result.candidates) == expected_ids


def test_real_ambiguous_article_still_lets_a_block_id_disambiguate(index_of: IndexLoader) -> None:
    index = index_of("rd1624")

    assert _found_id(index, "a1") == "a1"
    assert _found_id(index, "a1-2") == "a1-2"


# --- Real indexes: misses -------------------------------------------------------------


def test_real_missing_article_offers_nearest_numbers_first(index_of: IndexLoader) -> None:
    result = locate(index_of("constitucion"), "artículo 170")

    assert result.outcome == "not_found"
    assert result.entry is None
    assert result.candidates[0].block_id == "a169"
    assert "a168" in _ids(result.candidates[:3])
    assert "a160" not in _ids(result.candidates[:3])
    assert len(result.candidates) <= 8


def test_real_missing_provision_offers_same_type_first(index_of: IndexLoader) -> None:
    result = locate(index_of("constitucion"), "disposición adicional quinta")

    assert result.outcome == "not_found"
    assert _ids(result.candidates[:4]) == ["cuarta", "tercera", "segunda", "primera"]


def test_real_missing_provision_number_prefers_the_nearest_ordinal(index_of: IndexLoader) -> None:
    result = locate(index_of("ley40"), "disposición final novena")

    assert result.outcome == "not_found"
    ids = _ids(result.candidates)
    assert ids[0] == "dfdecima"
    assert "dfquinta" in ids


def test_real_plain_number_does_not_match_its_bis_article(index_of: IndexLoader) -> None:
    # Ley 40/2015 has "Artículo 46 bis" and "Artículo 48" but no plain "Artículo 46".
    result = locate(index_of("ley40"), "46")

    assert result.outcome == "not_found"
    ids = _ids(result.candidates)
    assert "a4-2" in ids
    assert ids.index("a4-2") < ids.index("a54")


def test_real_bare_article_number_does_not_match_its_suffixed_siblings(
    index_of: IndexLoader,
) -> None:
    # "Artículo 108 bis" and "108 ter" exist; "Artículo 108" does not.
    result = locate(index_of("ley40"), "artículo 108")

    assert result.outcome == "not_found"
    assert set(_ids(result.candidates[:2])) == {"a1-2", "a1-3"}


def test_real_article_between_neighbours_lists_both(index_of: IndexLoader) -> None:
    result = locate(index_of("ley40"), "artículo 47")

    assert result.outcome == "not_found"
    assert {"a4-2", "a48"} <= set(_ids(result.candidates[:3]))


def test_real_old_law_missing_ordinal_article(index_of: IndexLoader) -> None:
    result = locate(index_of("ley49"), "artículo 25")

    assert result.outcome == "not_found"
    assert result.candidates[0].block_id == "aveinticuatro"


def test_real_missing_transitional_provision_lists_transitional_ones(
    index_of: IndexLoader,
) -> None:
    result = locate(index_of("ley39"), "disposición transitoria única")

    assert result.outcome == "not_found"
    assert result.candidates[0].title.startswith("Disposición transitoria")


def test_real_bare_provision_type_with_several_blocks_is_not_a_unique_hit(
    index_of: IndexLoader,
) -> None:
    # The Constitution has nine transitional provisions: "the only one of that type" fails.
    result = locate(index_of("constitucion"), "disposición transitoria")

    assert result.outcome != "found"
    assert result.entry is None


def test_real_preamble_with_two_untitled_candidates_still_resolves_to_the_preamble(
    index_of: IndexLoader,
) -> None:
    # The IVA index lists both `preambulo` and `pr` with empty titles. The spec allows either
    # a single hit or an ambiguity, but the `preambulo` block must be in the answer.
    result = locate(index_of("iva"), "preámbulo")

    assert result.outcome in ("found", "ambiguous")
    answered = [result.entry] if result.entry else list(result.candidates)
    assert "preambulo" in _ids([e for e in answered if e is not None])


# --- Real indexes: properties over every fixture ----------------------------------------


_DESIGNATION_TITLE = re.compile(
    r"^(Art(ículo|\.)? \d+( (bis|ter|quater|quáter|quinque|quinquies))?|"
    r"Disposición (adicional|transitoria|derogatoria|final)\b.*)$"
)


@pytest.mark.parametrize("law", ALL_LAWS)
def test_real_index_invariants(index_of: IndexLoader, law: str) -> None:
    index = index_of(law)

    # Every block id finds its own block, whatever its title (including empty ones).
    for entry in index:
        by_id = locate_block_id(index, entry.block_id)
        assert by_id.outcome == "found", entry.block_id
        assert by_id.entry == entry
        assert by_id.candidates == ()

    # Every article or provision title, used as a query, finds its block or its duplicates.
    checked = 0
    for entry in index:
        if not _DESIGNATION_TITLE.match(entry.title):
            continue
        checked += 1
        by_title = locate(index, entry.title)
        if by_title.outcome == "found":
            assert by_title.entry is not None
            assert by_title.entry.title == entry.title, entry.title
        else:
            assert by_title.outcome == "ambiguous", entry.title
            assert entry in by_title.candidates, entry.title
            assert all(c.title == entry.title for c in by_title.candidates), entry.title
    assert checked > 0

    # Misses offer titled blocks of this index only, at most eight, without repeats.
    for query in ("zzzz qqqq", "artículo 99999"):
        miss = locate(index, query)
        assert miss.outcome == "not_found", query
        assert miss.entry is None
        assert 0 < len(miss.candidates) <= 8, query
        assert all(c.title for c in miss.candidates), query
        assert all(c in index for c in miss.candidates), query
        assert len(set(_ids(miss.candidates))) == len(miss.candidates)


# --- Synthetic: latin suffixes ----------------------------------------------------------

SUFFIX_CASES = [
    ("bis", "7 bis"),
    ("quater", "7 quáter"),
    ("quáter", "7 quater"),
    ("quinquies", "7 quinque"),
    ("quinque", "7 quinquies"),
    ("sexies", "art. 7 sexies"),
    ("septies", "7 septies."),
    ("nonies", "7nonies"),
    ("decies", "7 decies"),
]


@pytest.mark.parametrize(("title_suffix", "query"), SUFFIX_CASES)
def test_latin_suffix_forms_resolve_to_the_suffixed_article(title_suffix: str, query: str) -> None:
    index = _pairs(
        ("plain", "Artículo 7"),
        ("target", f"Artículo 7 {title_suffix}"),
        ("other", "Artículo 70"),
    )

    assert _found_id(index, query) == "target"


@pytest.mark.parametrize("query", ["7", "artículo 7", "art. 7"])
def test_plain_number_ignores_suffixed_siblings(query: str) -> None:
    index = _pairs(("bis", "Artículo 7 bis"), ("plain", "Artículo 7"), ("ter", "Artículo 7 ter"))

    assert _found_id(index, query) == "plain"


def test_different_suffixes_are_different_articles() -> None:
    index = _pairs(("ter", "Artículo 7 ter"))

    result = locate(index, "7 bis")

    assert result.outcome == "not_found"
    assert _ids(result.candidates) == ["ter"]


# --- Synthetic: ranges ----------------------------------------------------------------

RANGE_INDEX = _pairs(
    ("r1", "Arts 10 a 12"),
    ("r2", "Arts. 20 al 22"),
    ("r3", "Artículos 30 A 32"),
    ("r4", "Artículos 163 bis a 163 quáter"),
    ("plain", "Artículo 50"),
)


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("11", "r1"),
        ("art. 22", "r2"),
        ("31", "r3"),
        ("163 ter", "r4"),
        ("163 quater", "r4"),
        ("50", "plain"),
    ],
)
def test_article_inside_a_range_resolves_to_the_range_block(query: str, expected_id: str) -> None:
    assert _found_id(RANGE_INDEX, query) == expected_id


@pytest.mark.parametrize("query", ["9", "13", "23", "163", "163 quinquies"])
def test_article_outside_every_range_is_not_found(query: str) -> None:
    result = locate(RANGE_INDEX, query)

    assert result.outcome == "not_found"
    assert result.candidates


# --- Synthetic: annexes and sole article ---------------------------------------------

ANNEX_INDEX = _pairs(
    ("an1", "Anexo I"),
    ("an2", "ANEXO II"),
    ("an3", "Anexo 3"),
    ("an4", "Anexo IV. Modelo de solicitud"),
    ("an12", "Anexo XII"),
)


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("anexo I", "an1"),
        ("anexo 1", "an1"),
        ("Anexo ii", "an2"),
        ("ANEXO III", "an3"),
        ("anexo 4", "an4"),
        ("anexo 12", "an12"),
    ],
)
def test_numbered_annexes_accept_roman_and_arabic_numerals(query: str, expected_id: str) -> None:
    assert _found_id(ANNEX_INDEX, query) == expected_id


def test_missing_annex_number_is_not_found_and_offers_annexes_first() -> None:
    index = [*_pairs(("a5", "Artículo 5")), *ANNEX_INDEX]

    result = locate(index, "anexo 7")

    assert result.outcome == "not_found"
    assert result.candidates[0].title.lower().startswith("anexo")


def test_a_single_unnumbered_annex_answers_the_bare_word() -> None:
    index = _pairs(("a1", "Artículo 1"), ("an", "ANEXO"))

    assert _found_id(index, "anexo") == "an"
    assert _found_id(index, "Anexo.") == "an"


@pytest.mark.parametrize("query", ["artículo único", "único", "ARTÍCULO ÚNICO", "articulo unico"])
def test_sole_article_by_name(query: str) -> None:
    index = _pairs(("u", "Artículo único"), ("a2", "Artículo 2"))

    assert _found_id(index, query) == "u"


def test_sole_article_is_not_article_one() -> None:
    index = _pairs(("u", "Artículo único"))

    assert locate(index, "artículo 1").outcome == "not_found"


def test_article_one_is_not_the_sole_article() -> None:
    index = _pairs(("a1", "Artículo 1"))

    assert locate(index, "artículo único").outcome == "not_found"


# --- Synthetic: number words ----------------------------------------------------------

CARDINALS = [
    (1, "uno"),
    (22, "veintidós"),
    (31, "treinta y uno"),
    (99, "noventa y nueve"),
]
ORDINALS = [
    (1, "primero"),
    (23, "vigesimotercero"),
    (50, "quincuagésimo"),
    (99, "nonagésimo noveno"),
]
ARTICLE_WORDS = CARDINALS + ORDINALS


@pytest.mark.parametrize(("number", "word"), ARTICLE_WORDS, ids=lambda v: str(v))
def test_number_word_query_finds_the_article_titled_with_digits(number: int, word: str) -> None:
    index = _pairs(
        ("before", f"Artículo {number + 10}"),
        ("target", f"Artículo {number}"),
        ("after", f"Artículo {number + 11}"),
    )

    assert _found_id(index, f"artículo {word}") == "target"


@pytest.mark.parametrize(("number", "word"), ARTICLE_WORDS, ids=lambda v: str(v))
def test_digit_query_finds_the_article_titled_with_a_number_word(number: int, word: str) -> None:
    index = _pairs(
        ("decoy", f"Artículo {number + 1}"),
        ("target", f"Artículo {word}"),
    )

    assert _found_id(index, f"{number}") == "target"
    assert _found_id(index, f"art. {number}") == "target"


def test_number_words_in_both_titles_and_queries_match_each_other() -> None:
    index = _pairs(("t", "Artículo veintidós"), ("d", "Artículo veintitrés"))

    assert _found_id(index, "artículo veintidos") == "t"


# --- Synthetic: provisions --------------------------------------------------------------

PROVISION_ORDINALS = [
    (1, "primera"),
    (3, "tercera"),
    (7, "séptima"),
    (10, "décima"),
    (13, "decimotercera"),
    (20, "vigésima"),
    (23, "vigesimotercera"),
    (30, "trigésima"),
    (50, "quincuagésima"),
]


@pytest.mark.parametrize(("number", "word"), PROVISION_ORDINALS, ids=lambda v: str(v))
def test_provision_ordinal_word_matches_the_numeric_query(number: int, word: str) -> None:
    decoy = "segunda" if number == 1 else "primera"
    index = _pairs(
        ("decoy", f"Disposición adicional {decoy}"),
        ("other_type", f"Disposición transitoria {word}"),
        ("target", f"Disposición adicional {word}"),
    )

    assert _found_id(index, f"disposición adicional {number}") == "target"
    assert _found_id(index, f"DA {number}") == "target"
    assert _found_id(index, f"disposición transitoria {number}") == "other_type"


@pytest.mark.parametrize(
    ("number", "word"),
    [(1, "primera"), (7, "séptima"), (25, "vigesimoquinta"), (50, "quincuagésima")],
    ids=lambda v: str(v),
)
def test_provision_masculine_and_feminine_ordinals_are_the_same(number: int, word: str) -> None:
    masculine = word[:-1] + "o"
    index = _pairs(("target", f"Disposición adicional {word}"), ("x", "Disposición final primera"))

    assert _found_id(index, f"disposición adicional {masculine}") == "target"
    assert _found_id(index, f"disposición adicional {word}") == "target"
    assert _found_id(index, f"disposición adicional {number}") == "target"


PROVISION_FORMS = [
    "disposición adicional tercera",
    "Disposición Adicional Tercera.",
    "disposicion adicional 3",
    "disposición adicional 3.ª",
    "DA 3",
    "D.A. 3",
    "d.a. tercera",
]


@pytest.mark.parametrize("query", PROVISION_FORMS)
def test_provision_spellings_are_equivalent(query: str) -> None:
    index = _pairs(
        ("a3", "Artículo 3"),
        ("da2", "Disposición adicional segunda"),
        ("da3", "Disposición adicional tercera"),
        ("dt3", "Disposición transitoria tercera"),
        ("df3", "Disposición final tercera"),
    )

    assert _found_id(index, query) == "da3"


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("DT 2", "dt"),
        ("d.t. segunda", "dt"),
        ("DD 1", "dd"),
        ("d.d. primera", "dd"),
        ("DF 1", "df"),
        ("disposición final 1.ª", "df"),
    ],
)
def test_provision_type_abbreviations(query: str, expected_id: str) -> None:
    index = _pairs(
        ("da", "Disposición adicional segunda"),
        ("dt", "Disposición transitoria segunda"),
        ("dd", "Disposición derogatoria primera"),
        ("df", "Disposición final primera"),
        ("df2", "Disposición final segunda"),
    )

    assert _found_id(index, query) == expected_id


@pytest.mark.parametrize(
    "query",
    ["disposición transitoria única", "disposición transitoria", "DT única", "d.t. unica"],
)
def test_sole_provision_answers_unica_and_bare_type(query: str) -> None:
    index = _pairs(("dt", "Disposición transitoria única"), ("da", "Disposición adicional primera"))

    assert _found_id(index, query) == "dt"


@pytest.mark.parametrize("query", ["disposición final única", "disposición final"])
def test_ordinal_less_provision_answers_unica_and_bare_type(query: str) -> None:
    index = _pairs(("x", "Disposición transitoria primera"), ("y", "Disposición final"))

    assert _found_id(index, query) == "y"


def test_bare_provision_type_is_not_unique_when_the_law_has_several() -> None:
    index = _pairs(("p", "Disposición adicional primera"), ("s", "Disposición adicional segunda"))

    result = locate(index, "disposición adicional")

    assert result.outcome != "found"


def test_unica_does_not_match_a_numbered_provision_when_the_law_has_several() -> None:
    index = _pairs(
        ("p", "Disposición transitoria primera"), ("s", "Disposición transitoria segunda")
    )

    result = locate(index, "disposición transitoria única")

    assert result.outcome == "not_found"
    assert set(_ids(result.candidates)) == {"p", "s"}


def test_same_number_in_different_provision_types_is_not_confused() -> None:
    index = _pairs(("a", "Disposición adicional primera"), ("f", "Disposición final primera"))

    assert _found_id(index, "DF 1") == "f"
    assert _found_id(index, "DA 1") == "a"


def test_provision_ordinal_is_not_an_article_number() -> None:
    index = _pairs(("art3", "Artículo 3"), ("da3", "Disposición adicional tercera"))

    assert _found_id(index, "3") == "art3"
    assert _found_id(index, "artículo 3") == "art3"


def test_unknown_provision_type_is_not_found_and_not_confused_with_an_article() -> None:
    index = _pairs(("a3", "Artículo 3"), ("da3", "Disposición adicional tercera"))

    result = locate(index, "disposición testamentaria 3")

    assert result.outcome == "not_found"
    assert result.entry is None
    assert result.candidates


def test_provision_of_another_type_is_not_found_with_that_type_first() -> None:
    index = _pairs(
        ("a4", "Artículo 4"),
        ("da1", "Disposición adicional primera"),
        ("da2", "Disposición adicional segunda"),
    )

    result = locate(index, "disposición adicional 4")

    assert result.outcome == "not_found"
    assert _ids(result.candidates[:2]) == ["da2", "da1"]
    assert "a4" in _ids(result.candidates)


# --- Synthetic: bare ordinals, § and preamble -----------------------------------------


def test_bare_ordinal_title_matches_only_by_exact_normalised_title() -> None:
    index = _pairs(("p1", "Primera"), ("t3", "Tercera"), ("da", "Disposición adicional cuarta"))

    assert _found_id(index, "Tercera") == "t3"
    assert _found_id(index, "tercera.") == "t3"
    assert _found_id(index, "PRIMERA") == "p1"


def test_bare_ordinal_query_does_not_match_a_full_provision_title() -> None:
    index = _pairs(("da", "Disposición adicional tercera"))

    assert locate(index, "tercera").outcome == "not_found"


def test_paragraph_sign_titles_match_by_exact_title_and_are_not_articles() -> None:
    index = _pairs(("s1", "§ 1"), ("s2", "§ 2"), ("a2", "Artículo 2"))

    assert _found_id(index, "§ 2") == "s2"
    assert _found_id(index, "§2") == "s2"
    assert _found_id(index, "2") == "a2"


@pytest.mark.parametrize(
    "query",
    ["preámbulo", "Exposicion de Motivos"],
)
@pytest.mark.parametrize(
    "preamble",
    [("pr", ""), ("preambulo", ""), ("x1", "[preambulo]"), ("x2", "Preámbulo")],
    ids=["id-pr", "id-preambulo", "bracketed-title", "plain-title"],
)
def test_preamble_is_found_by_id_or_bracketed_title(query: str, preamble: tuple[str, str]) -> None:
    index = _pairs(("a1", "Artículo 1"), preamble, ("fi", ""))

    assert _found_id(index, query) == preamble[0]


def test_missing_preamble_is_not_found() -> None:
    result = locate(_pairs(("a1", "Artículo 1")), "preámbulo")

    assert result.outcome == "not_found"


# --- Synthetic: titles that continue, spacing, case, punctuation -----------------------

CONTINUING_INDEX = _pairs(
    ("a5", "Artículo 5. Definiciones"),
    ("a50", "Artículo 50. Otras definiciones"),
    ("a6", "Art. 6.- Objeto"),
    ("a7bis", "Artículo 7 bis. Ámbito"),
    ("df1", "Disposición final primera. Título competencial"),
    ("an1", "Anexo I. Modelos"),
)


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("5", "a5"),
        ("artículo 5", "a5"),
        ("6", "a6"),
        ("7 bis", "a7bis"),
        ("DF 1", "df1"),
        ("disposición final primera", "df1"),
        ("anexo 1", "an1"),
    ],
)
def test_only_the_leading_designation_of_a_title_is_compared(query: str, expected_id: str) -> None:
    assert _found_id(CONTINUING_INDEX, query) == expected_id


@pytest.mark.parametrize(
    "query",
    [
        "artículo\u00a046\u00a0bis",
        "art.  46\tbis",
        "  46 bis  ",
        "46\u00a0bis",
        "art.46 bis",
        "art 46bis",
    ],
    ids=repr,
)
def test_odd_whitespace_in_queries_is_ignored(query: str) -> None:
    index = _pairs(("x", "Artículo 460"), ("t", "Artículo 46 bis"))

    assert _found_id(index, query) == "t"


def test_non_breaking_and_repeated_spaces_in_raw_titles_are_ignored() -> None:
    # ``IndexEntry`` normalises titles on validation; ``model_construct`` skips that, so this
    # checks that the locator tolerates a raw title too.
    raw = IndexEntry.model_construct(
        block_id="t", title="Artículo\u00a0 46\u00a0bis", updated_on=None
    )
    decoy = IndexEntry(block_id="d", title="Artículo 47")

    assert _found_id([decoy, raw], "artículo 46 bis") == "t"


CASE_CASES = [
    ("ARTÍCULO 5", "articulo 5"),
    ("artículo 5", "ARTÍCULO 5"),
    ("DISPOSICIÓN ADICIONAL PRIMERA", "disposicion adicional primera"),
    ("Disposición adicional séptima", "DISPOSICION ADICIONAL SEPTIMA"),
    ("Disposición transitoria única", "Disposición Transitoria Única"),
    ("ANEXO", "Anexo"),
]


@pytest.mark.parametrize(("title", "query"), CASE_CASES)
def test_case_and_accents_do_not_matter(title: str, query: str) -> None:
    index = _pairs(("decoy", "Artículo 99"), ("t", title))

    assert _found_id(index, query) == "t"


@pytest.mark.parametrize("suffix", [".", ",", ";", " ."])
def test_trailing_punctuation_in_the_query_is_ignored(suffix: str) -> None:
    index = _pairs(("t", "Artículo 46 bis"), ("d", "Artículo 46"))

    assert _found_id(index, f"artículo 46 bis{suffix}") == "t"


def test_trailing_punctuation_in_the_title_is_ignored() -> None:
    index = _pairs(("t", "Artículo 51 ter."), ("d", "Artículo 51"))

    assert _found_id(index, "51 ter") == "t"


# --- Exactness: no prefix or substring matching --------------------------------------


@pytest.mark.parametrize("query", ["artículo 1", "1", "art. 1", "Art 1"])
def test_article_one_matches_neither_ten_eleven_nor_one_bis(query: str) -> None:
    index = _pairs(
        ("a10", "Artículo 10"),
        ("a11", "Artículo 11"),
        ("a1bis", "Artículo 1 bis"),
        ("a21", "Artículo 21"),
        ("a100", "Artículo 100"),
    )

    result = locate(index, query)

    assert result.outcome == "not_found"
    assert result.entry is None


def test_article_one_is_found_next_to_its_lookalikes() -> None:
    index = _pairs(("a10", "Artículo 10"), ("a1", "Artículo 1"), ("a1bis", "Artículo 1 bis"))

    assert _found_id(index, "1") == "a1"


def test_forty_six_and_forty_six_bis_are_told_apart() -> None:
    index = _pairs(("bis", "Artículo 46 bis"), ("plain", "Artículo 46"))

    assert _found_id(index, "46") == "plain"
    assert _found_id(index, "46 bis") == "bis"


# --- Block ids -------------------------------------------------------------------------


def test_a_designation_beats_a_block_id_that_looks_like_it() -> None:
    # The query "2" is the id of the first block and also the designation of the second: the
    # designation wins in ``locate``, and ``locate_block_id`` is the way to ask for the id.
    index = _pairs(("2", "Artículo 1"), ("a2", "Artículo 2"))

    assert _found_id(index, "2") == "a2"
    assert locate_block_id(index, "2").entry == index[0]


def test_block_id_match_is_case_sensitive() -> None:
    index = _pairs(("A1", "Artículo 7"), ("a1", "Artículo 8"))

    assert _found_id(index, "a1") == "a1"
    assert _found_id(index, "A1") == "A1"


def test_block_id_in_another_case_is_not_a_match() -> None:
    index = _pairs(("A1", "Artículo 7"))

    result = locate(index, "a1")

    assert result.outcome == "not_found"


def test_block_id_of_an_untitled_block_is_found() -> None:
    index = _pairs(("no", ""), ("a1", "Artículo 1"))

    assert _found_id(index, "no") == "no"


# --- Not found: candidates ---------------------------------------------------------------


def test_nearest_number_comes_first_on_both_sides() -> None:
    index = _entries("Artículo 10", "Artículo 20", "Artículo 30", "Artículo 40")

    result = locate(index, "26")

    assert result.outcome == "not_found"
    assert [c.title for c in result.candidates] == [
        "Artículo 30",
        "Artículo 20",
        "Artículo 40",
        "Artículo 10",
    ]


def test_candidates_are_capped_at_eight_and_nearest_first() -> None:
    index = _entries(*(f"Artículo {n}" for n in range(1, 31)))

    result = locate(index, "artículo 40")

    assert len(result.candidates) == 8
    assert [c.title for c in result.candidates[:3]] == ["Artículo 30", "Artículo 29", "Artículo 28"]


def test_same_type_candidates_precede_other_types() -> None:
    index = _entries("Disposición adicional primera", "Artículo 50", "Anexo I")

    result = locate(index, "artículo 49")

    assert result.candidates[0].title == "Artículo 50"


def test_untitled_blocks_are_never_candidates() -> None:
    index = _pairs(("no", ""), ("pr", ""), ("a5", "Artículo 5"), ("fi", ""), ("a9", "Artículo 9"))

    for query in ("artículo 7", "texto cualquiera"):
        result = locate(index, query)
        assert result.outcome == "not_found"
        assert result.candidates
        assert {c.block_id for c in result.candidates} <= {"a5", "a9"}


def test_unrelated_free_text_is_not_found_with_candidates() -> None:
    index = _entries("Artículo 1", "Artículo 2", "Disposición final primera")

    result = locate(index, "régimen sancionador de las entidades locales")

    assert result.outcome == "not_found"
    assert result.entry is None
    assert 0 < len(result.candidates) <= 8
    assert all(c in index for c in result.candidates)


def test_index_with_only_untitled_blocks_gives_no_candidates() -> None:
    index = _pairs(("no", ""), ("pr", ""), ("fi", ""))

    for query in ("artículo 1", "algo"):
        result = locate(index, query)
        assert result.outcome == "not_found"
        assert result.entry is None
        assert result.candidates == ()


def test_empty_index_is_not_found() -> None:
    result = locate([], "artículo 1")

    assert result.outcome == "not_found"
    assert result.entry is None
    assert result.candidates == ()


# --- Ambiguity -------------------------------------------------------------------------


def test_ambiguous_candidates_follow_index_order_not_id_order() -> None:
    index = _pairs(
        ("z", "Artículo 9"), ("x", "Artículo 4"), ("m", "Artículo 9"), ("a", "Artículo 9")
    )

    result = locate(index, "9")

    assert result.outcome == "ambiguous"
    assert result.entry is None
    assert _ids(result.candidates) == ["z", "m", "a"]


def test_ambiguous_candidates_are_capped_at_ten() -> None:
    index = _pairs(*((f"d{i:02d}", "Artículo 3") for i in range(12)), ("o", "Artículo 4"))

    result = locate(index, "artículo 3")

    assert result.outcome == "ambiguous"
    assert _ids(result.candidates) == [f"d{i:02d}" for i in range(10)]


def test_ambiguity_is_never_resolved_by_picking_the_first() -> None:
    index = _pairs(("first", "Artículo 1"), ("second", "Artículo 1"))

    result = locate(index, "artículo 1")

    assert result.entry is None
    assert result.outcome == "ambiguous"


def test_equivalent_spellings_of_the_same_designation_are_ambiguous() -> None:
    # "Artículo 1" and "Art 1" (Código Civil style) are the same designation.
    index = _pairs(("p", "Artículo 1"), ("q", "Art 1"))

    result = locate(index, "1")

    assert result.outcome == "ambiguous"
    assert _ids(result.candidates) == ["p", "q"]


# --- Input handling and the result type ---------------------------------------------------


@pytest.mark.parametrize("query", ["", " ", "   ", "\t\n", "\u00a0", " \u00a0 "], ids=repr)
def test_blank_query_raises_value_error(query: str) -> None:
    with pytest.raises(ValueError):
        locate(_entries("Artículo 1"), query)


def test_blank_query_raises_even_for_an_empty_index() -> None:
    with pytest.raises(ValueError):
        locate([], "  ")


def test_locate_accepts_any_sequence() -> None:
    index = tuple(_entries("Artículo 1", "Artículo 2"))

    assert _found_id(index, "2") == "b1"


def test_found_result_shape() -> None:
    index = _entries("Artículo 1", "Artículo 2")

    result = locate(index, "artículo 2")

    assert isinstance(result, LocateResult)
    assert result.outcome == "found"
    assert result.entry == index[1]
    assert result.candidates == ()
    assert isinstance(result.candidates, tuple)


def test_not_found_and_ambiguous_results_have_no_entry() -> None:
    missing = locate(_entries("Artículo 1"), "artículo 5")
    duplicate = locate(_entries("Artículo 1", "Artículo 1"), "artículo 1")

    assert missing.entry is None
    assert duplicate.entry is None
    assert isinstance(missing.candidates, tuple)
    assert isinstance(duplicate.candidates, tuple)


def test_locate_result_is_frozen() -> None:
    result = locate(_entries("Artículo 1"), "1")

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = "not_found"  # type: ignore[misc]
    assert dataclasses.is_dataclass(result)


# === Review findings: suffix family, thousands, trailing context, block ids, speed ===========


_IVA_163_FAMILY = [
    "decies",
    "undecies",
    "duodecies",
    "terdecies",
    "quaterdecies",
    "quinquiesdecies",
    "sexiesdecies",
    "septiesdecies",
    "octiesdecies",
    "noniesdecies",
    "vicies",
    "unvicies",
    "duovicies",
    "tervicies",
    "quatervicies",
    "quinvicies",
    "septvicies",
    "octovicies",
]


def test_real_iva_bare_163_is_its_own_article_not_ambiguous_among_the_suffixed_ones(
    index_of: IndexLoader,
) -> None:
    index = index_of("iva")

    result = locate(index, "163")

    # There is no plain "Artículo 163" in the trimmed index: the suffixed blocks must not match.
    assert result.outcome == "not_found"


@pytest.mark.parametrize("suffix", _IVA_163_FAMILY)
def test_real_iva_every_latin_suffix_of_article_163_finds_its_own_block(
    index_of: IndexLoader, suffix: str
) -> None:
    index = index_of("iva")
    expected = next(e for e in index if e.title.split() == ["Artículo", "163", suffix]).block_id

    assert _found_id(index, f"163 {suffix}") == expected
    assert _found_id(index, f"artículo 163{suffix}") == expected


def test_real_iva_vicies_is_found_and_distinct_from_decies(index_of: IndexLoader) -> None:
    index = index_of("iva")

    assert _found_id(index, "163 vicies") != _found_id(index, "163 decies")


def test_the_source_typo_sexvivies_is_read_like_sexvicies() -> None:
    # The real IVA index spells "Artículo 163 sexvivies" (api-notes, "Index title patterns").
    index = _pairs(("t", "Artículo 163 sexvivies"), ("d", "Artículo 163 sexies"))

    assert _found_id(index, "163 sexvicies") == "t"
    assert _found_id(index, "163 sexvivies") == "t"


@pytest.mark.parametrize(
    ("suffix", "other"),
    [
        ("tricies", "vicies"),
        ("untricies", "unvicies"),
        ("noviesvicies", "vicies"),
        ("octiesdecies", "octies"),
    ],
)
def test_latin_suffixes_compose_from_units_and_tens(suffix: str, other: str) -> None:
    index = _pairs(("t", f"Artículo 9 {suffix}"), ("d", f"Artículo 9 {other}"))

    assert _found_id(index, f"9 {suffix}") == "t"


def test_an_unknown_trailing_word_in_a_title_prevents_the_bare_number_match() -> None:
    index = _pairs(("x", "Artículo 163 foobar"), ("a", "Artículo 164"))

    result = locate(index, "163")

    assert result.outcome == "not_found"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("artículo 1.240", "a1240"),
        ("art. 1.902", "a1902"),
        ("1.240", "a1240"),
        ("art 1.240 bis", "a1240bis"),
    ],
)
def test_thousands_dot_reads_as_one_number(query: str, expected: str) -> None:
    index = _pairs(
        ("a1240", "Artículo 1240"),
        ("a1902", "Art. 1902"),
        ("a1240bis", "Artículo 1240 bis"),
        ("a124", "Artículo 124"),
        ("a1", "Artículo 1"),
    )

    result = locate(index, query)

    assert result.outcome == "found"
    assert result.entry is not None and result.entry.block_id == expected
    assert result.ignored is None


@pytest.mark.parametrize(
    ("query", "ignored"),
    [
        ("46.2", ".2"),
        ("artículo 46.2", ".2"),
        ("artículo 46, apartado 2", ", apartado 2"),
        ("artículo 46 de la Constitución", "de la Constitución"),
        ("art. 46 CE", "CE"),
        ("artículo 46 de la Ley 39/2015", "de la Ley 39/2015"),
        ("46 bis.2", ".2"),
    ],
)
def test_trailing_paragraph_and_context_are_ignored_and_reported(query: str, ignored: str) -> None:
    index = _pairs(("p", "Artículo 46"), ("b", "Artículo 46 bis"), ("o", "Artículo 460"))

    result = locate(index, query)

    assert result.outcome == "found"
    assert result.entry is not None
    assert result.entry.block_id == ("b" if "bis" in query else "p")
    assert result.ignored == ignored


def test_constitution_paragraph_citation_locates_the_article(index_of: IndexLoader) -> None:
    result = locate(index_of("constitucion"), "artículo 149.1.18ª")

    assert result.outcome == "found"
    assert result.entry is not None and result.entry.block_id == "a149"
    assert result.ignored == ".1.18ª"


@pytest.mark.parametrize("query", ["46 bis", "artículo 46 bis.", "Art. 46bis", "46.", "46"])
def test_nothing_ignored_for_a_plain_designation(query: str) -> None:
    index = _pairs(("p", "Artículo 46"), ("b", "Artículo 46 bis"))

    assert locate(index, query).ignored is None


def test_ordinal_indicator_of_a_provision_is_not_reported_as_ignored() -> None:
    index = _pairs(("t", "Disposición transitoria tercera"))

    result = locate(index, "disposición transitoria 3.ª")

    assert result.outcome == "found"
    assert result.ignored is None


def test_a_query_without_a_leading_designation_is_compared_whole_as_before() -> None:
    index = _pairs(("a1", "Artículo 1"), ("x", "Libro Segundo"))

    result = locate(index, "libro segundo")

    assert (result.outcome, result.ignored) == ("found", None)
    assert locate(index, "régimen del libro segundo").outcome == "not_found"


@pytest.mark.parametrize("query", ["da", "dt", "df", "dd", "DT"])
def test_abbreviation_is_a_designation_not_a_block_id(query: str) -> None:
    # Two blocks of each type, and ids equal to the abbreviations: the id must not win silently.
    index = _pairs(
        ("da", "Disposición adicional primera"),
        ("dt", "Disposición transitoria primera"),
        ("df", "Disposición final primera"),
        ("dd", "Disposición derogatoria primera"),
        ("da-2", "Disposición adicional segunda"),
        ("dt-2", "Disposición transitoria segunda"),
        ("df-2", "Disposición final segunda"),
        ("dd-2", "Disposición derogatoria segunda"),
    )

    result = locate(index, query)

    assert result.outcome == "not_found"
    assert result.candidates


def test_bare_ordinal_in_another_case_is_as_ambiguous_as_the_capitalised_one() -> None:
    index = _pairs(("primera", "Primera"), ("primera-2", "Primera"))

    assert locate(index, "Primera").outcome == "ambiguous"
    assert locate(index, "primera").outcome == "ambiguous"


def test_block_id_that_is_not_a_designation_still_resolves() -> None:
    index = _pairs(("co", ""), ("no", ""), ("a1-2", "Artículo 1"), ("a1", "Artículo 1"))

    assert _found_id(index, "co") == "co"
    assert _found_id(index, "a1-2") == "a1-2"


def test_locate_block_id_finds_exact_ids_only() -> None:
    index = _pairs(("dt", "Disposición transitoria primera"), ("dt-2", "Disposición transitoria"))

    found = locate_block_id(index, "dt")
    missing = locate_block_id(index, "DT")

    assert (found.outcome, found.entry) == ("found", index[0])
    assert missing.outcome == "not_found"
    assert missing.entry is None
    assert missing.candidates


def test_locate_block_id_not_found_offers_titled_blocks() -> None:
    index = _pairs(("no", ""), ("a1", "Artículo 1"), ("a2", "Artículo 2"))

    result = locate_block_id(index, "zzz")

    assert result.outcome == "not_found"
    assert _ids(result.candidates) == ["a1", "a2"]


def _big_index(size: int = 2500) -> list[IndexEntry]:
    titles = [f"Art {n}" for n in range(1, size - 200)] + [f"Art {n} bis" for n in range(1, 200)]
    titles += ["Disposición adicional primera", "§ 1", "Capítulo I", "Primera", "Primera"]
    return [IndexEntry(block_id=f"b{i}", title=t) for i, t in enumerate(titles)]


def test_locate_stays_fast_on_a_code_sized_index() -> None:
    index = _big_index()
    queries = [
        "1",
        "artículo 1240",
        "art. 1.902",
        "46 bis",
        "artículo 46.2",
        "artículo 14 de la Constitución",
        "disposición adicional primera",
        "Primera",
        "da",
        "artículo 99999",
        "régimen sancionador de las entidades locales",
        "§ 1",
    ] * 2

    started = time.perf_counter()
    for query in queries[:20]:
        locate(index, query)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, elapsed


def test_a_two_hundred_character_query_is_still_fast() -> None:
    index = _big_index()

    started = time.perf_counter()
    locate(index, "x" * 200)
    locate(index, "artículo 5 " + "de la ley " * 18)

    assert time.perf_counter() - started < 1.0


def test_real_bare_abbreviations_no_longer_resolve_by_id(index_of: IndexLoader) -> None:
    # RD 1624/1992 has several transitional provisions, one of them with the id "dt".
    index = index_of("rd1624")

    assert locate(index, "dt").outcome == "not_found"
    assert locate_block_id(index, "dt").outcome == "found"
