"""Contract tests for ``boe_insights_mcp.citation``: quote matching and citation status.

The module is pure logic, so almost every test builds its inputs by hand to show one rule. The
fuzzy cases use synthetic unique words (``voz0``, ``voz1``...) because the expected similarity is
then unambiguous under the spec formula (matching tokens / max(segment length, window length));
the realistic cases use sentences of Ley 40/2015. The ``real article 48`` section at the end
parses the recorded two-version block fixture through ``parse_block``.

The spec clarifications K1 to K8 (end of ``inc4-citation.md``) are asserted throughout and cited
as "K<n>" in comments. Nothing in this file rests on an open ambiguity.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Callable, Collection, Sequence
from datetime import date, timedelta
from typing import Any

import pytest

from boe_insights_mcp.citation import (
    CitationMatch,
    QuoteMatch,
    VersionMatch,
    citation_status,
    comparison_tokens,
    match_quote,
    match_versions,
)
from boe_insights_mcp.models import Block, BlockVersion, Paragraph
from boe_insights_mcp.textparse import parse_block

NBSP = chr(0xA0)
SOFT_HYPHEN = chr(0xAD)


# --- factories ----------------------------------------------------------------------------------


def words(n: int, prefix: str = "voz") -> list[str]:
    """``n`` distinct single-token words, so alignment is unambiguous."""
    return [f"{prefix}{i}" for i in range(n)]


def substituted(source: Sequence[str], positions: Collection[int]) -> list[str]:
    """``source`` with the words at ``positions`` replaced by words found nowhere else."""
    return [f"ajeno{i}" if i in positions else w for i, w in enumerate(source)]


def spread(n: int, k: int) -> list[int]:
    """``k`` interior positions of an ``n``-word passage, evenly spread."""
    return [1 + (i * (n - 2)) // k for i in range(k)]


def surrounded(passage: Sequence[str], before: int = 6, after: int = 6) -> str:
    """A text holding ``passage`` between filler words that share nothing with it."""
    return " ".join([*words(before, "pre"), *passage, *words(after, "post")])


def para(text: str, kind: str = "parrafo", quoted: bool = False) -> Paragraph:
    return Paragraph(kind=kind, text=text, quoted=quoted)


def make_version(
    *paragraphs: Paragraph,
    effective: date | None = None,
    published: date | None = None,
    law: str = "BOE-A-2000-1",
) -> BlockVersion:
    return BlockVersion(
        source_law_id=law, published_on=published, effective_from=effective, paragraphs=paragraphs
    )


def text_version(text: str, effective: date | None) -> BlockVersion:
    """A one-paragraph version of ``text`` starting on ``effective``."""
    return make_version(para(text), effective=effective)


def make_block(*versions: BlockVersion) -> Block:
    return Block(block_id="a48", kind="precepto", title="Artículo 48", versions=tuple(versions))


def level_for(similarity: float) -> Any:
    """The level the spec thresholds give a similarity (used to build status inputs)."""
    if similarity >= 1.0:
        return "exact"
    if similarity >= 0.85:
        return "close"
    return "partial" if similarity >= 0.5 else "none"


def version_match(
    level: str,
    position: int = 1,
    similarity: float | None = None,
    relation: str = "earlier",
) -> VersionMatch:
    """A ``VersionMatch`` of the given level, for the pure ``citation_status`` tests."""
    defaults = {"exact": 1.0, "close": 0.9, "partial": 0.7, "none": 0.1}
    sim = defaults[level] if similarity is None else similarity
    quote_match = QuoteMatch(
        level=level,  # type: ignore[arg-type]
        similarity=sim,
        matched_text="" if level == "none" else "texto",
        differences="" if level in ("none", "exact") else "[-a-] {+b+}",
        segments=1,
    )
    return VersionMatch(
        position=position,
        starts_on=None,
        ends_on=None,
        match=quote_match,
        relation=relation,  # type: ignore[arg-type]
    )


def citation_match(
    current: str | None, other: str | None = None, relation: str = "earlier"
) -> CitationMatch:
    """A ``CitationMatch`` whose current and best other versions have the given levels."""
    return CitationMatch(
        current=None if current is None else version_match(current, 2, relation="current"),
        best_other=None if other is None else version_match(other, 1, relation=relation),
    )


# --- 1.1 comparison_tokens ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("   \n\t ", []),
        ("Los convenios se perfeccionan", ["los", "convenios", "se", "perfeccionan"]),
        ("convenio, sujeto; público: (privado).", ["convenio", "sujeto", "público", "privado"]),
        ("5.000 euros", ["5000", "euros"]),  # thousands separator is typography (A9)
        ("5.00 euros", ["5", "00", "euros"]),  # groups after the first need exactly 3 digits
        ("Ley 40/2015, de 1 de octubre", ["ley", "40", "2015", "de", "1", "de", "octubre"]),
        ("uno_dos", ["uno", "dos"]),
    ],
)
def test_tokens_are_runs_of_letters_and_digits(text: str, expected: list[str]) -> None:
    assert comparison_tokens(text) == expected


@pytest.mark.parametrize("space", [NBSP, "\u2009", "\u202f", "\u2003", "\u3000", NBSP + NBSP])
def test_nbsp_and_unicode_spaces_are_spaces(space: str) -> None:
    assert comparison_tokens(f"plazo{space}de{space}5") == ["plazo", "de", "5"]


def test_soft_hyphens_are_removed_so_the_word_stays_whole() -> None:
    soft = f"ad{SOFT_HYPHEN}mi{SOFT_HYPHEN}nis{SOFT_HYPHEN}tración"
    assert comparison_tokens(f"la {soft} pública") == ["la", "administración", "pública"]


@pytest.mark.parametrize(
    "text",
    [
        "«cláusula»",
        "\u201ccláusula\u201d",
        "\u2018cláusula\u2019",
        '"cláusula"',
        "'cláusula'",
        "«\u201c\u2018cláusula\u2019\u201d»",
    ],
)
def test_quotation_marks_are_dropped(text: str) -> None:
    assert comparison_tokens(text) == ["cláusula"]


def test_quotation_marks_between_words_do_not_leave_empty_tokens() -> None:
    assert comparison_tokens("en el «Boletín Oficial del Estado»") == [
        "en",
        "el",
        "boletín",
        "oficial",
        "del",
        "estado",
    ]


def test_quotation_mark_inside_a_word_is_dropped_not_a_separator() -> None:
    # K3: a quotation mark or apostrophe inside a word is dropped, not a separator.
    assert comparison_tokens("Consell d\u2019Aragó") == ["consell", "daragó"]
    assert comparison_tokens("l'avi") == ["lavi"]


@pytest.mark.parametrize("dash", ["-", "\u2013", "\u2014", "\u2010"])
def test_every_dash_is_a_separator(dash: str) -> None:
    assert comparison_tokens(f"contencioso{dash}administrativo") == [
        "contencioso",
        "administrativo",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A9: the dot between a word character and an ordinal indicator is typography.
        ("1.ª", ["1ª"]),
        ("2.º", ["2º"]),
        ("N.º 5", ["nº", "5"]),
        ("apartado 1.a)", ["apartado", "1", "a"]),
        ("art. 3.º bis", ["art", "3º", "bis"]),
        ("1ª edición", ["1ª", "edición"]),
        ("Tercera.ª", ["terceraª"]),
        ("1.°", ["1º"]),  # the degree sign stands for the masculine ordinal
        ("1°", ["1º"]),
    ],
)
def test_ordinal_indicators_are_token_characters(text: str, expected: list[str]) -> None:
    assert comparison_tokens(text) == expected


def test_case_is_folded() -> None:
    assert comparison_tokens("ARTÍCULO Ñandú Straße") == ["artículo", "ñandú", "strasse"]


def test_accents_are_kept() -> None:
    assert comparison_tokens("término") != comparison_tokens("termino")
    assert comparison_tokens("Término") == ["término"]


def test_decomposed_accents_are_normalised_to_nfc() -> None:
    decomposed = "término"
    assert comparison_tokens(decomposed) == ["término"]


# --- 1.2 match_quote: exact matches -------------------------------------------------------------

LAW_5 = (
    "5. Los convenios que incluyan compromisos financieros deberán ser financieramente "
    "sostenibles, debiendo quienes los suscriban tener capacidad para financiar los asumidos "
    "durante la vigencia del convenio."
)


def test_exact_fragment_inside_a_paragraph() -> None:
    result = match_quote("deberán ser financieramente sostenibles", LAW_5)
    assert result.level == "exact"
    assert result.similarity == 1.0
    assert result.differences == ""
    assert result.segments == 1
    assert result.matched_text == "deberán ser financieramente sostenibles"


def test_exact_ignores_case_quotes_dashes_and_spacing_of_the_quote() -> None:
    quote = f"«DEBERÁN  ser{NBSP}financieramente — sostenibles»"
    result = match_quote(quote, LAW_5)
    assert result.level == "exact"
    assert result.matched_text == "deberán ser financieramente sostenibles"


def test_matched_text_is_the_original_text_verbatim() -> None:
    text = "1. Las Administraciones Públicas, sus organismos públicos y entidades vinculadas."
    result = match_quote("administraciones públicas sus ORGANISMOS", text)
    assert result.level == "exact"
    # Original capitals and the comma are kept; the surrounding "1. " is not part of the match.
    assert result.matched_text == "Administraciones Públicas, sus organismos"


def test_matched_text_excludes_punctuation_around_the_first_and_last_matched_word() -> None:
    text = "(Derogado), y «las partes»."
    result = match_quote("derogado y las partes", text)
    assert result.level == "exact"
    assert result.matched_text == "Derogado), y «las partes"


def test_matched_text_keeps_the_original_accents_even_if_the_quote_has_other_spelling() -> None:
    result = match_quote("el término del plazo", "Vence el término del plazo legal.")
    assert result.matched_text == "el término del plazo"


def test_exact_across_paragraphs() -> None:
    text = "9. Fin del primer párrafo.\nInicio del segundo párrafo y su continuación."
    result = match_quote("primer párrafo inicio del segundo", text)
    assert result.level == "exact"
    assert result.matched_text == "primer párrafo.\nInicio del segundo"


def test_exact_returns_the_earliest_occurrence() -> None:
    text = "Alfa beta gamma delta, y luego alfa beta gamma delta!"
    result = match_quote("alfa beta gamma delta", text)
    assert result.matched_text == "Alfa beta gamma delta"


def test_the_quote_must_be_contiguous_in_the_text() -> None:
    result = match_quote("uno dos cuatro cinco", "uno dos tres cuatro cinco")
    assert result.level != "exact"


OMISSION = (
    "Las partes podrán suscribir convenios con sujetos de derecho público y privado, "
    "sin que ello suponga cesión de la titularidad."
)


@pytest.mark.parametrize(
    "mark",
    [
        " (...) ",
        "(...)",
        " [...] ",
        "[...]",
        " … ",
        "…",
        " ... ",
        "...",
        " .... ",
        "......",
    ],
)
def test_every_omission_mark_form_splits_the_quote_into_segments(mark: str) -> None:
    result = match_quote(f"Las partes podrán{mark}cesión de la titularidad", OMISSION)
    assert result.level == "exact"
    assert result.segments == 2
    # The text skipped between the segments is part of matched_text.
    assert result.matched_text == (
        "Las partes podrán suscribir convenios con sujetos de derecho público y privado, "
        "sin que ello suponga cesión de la titularidad"
    )


def test_two_dots_are_not_an_omission_mark() -> None:
    result = match_quote("uno dos.. tres cuatro", "uno dos tres cuatro")
    assert result.segments == 1
    assert result.level == "exact"


def test_three_segments_are_counted() -> None:
    result = match_quote(
        "Las partes (...) con sujetos de derecho (...) cesión de la titularidad", OMISSION
    )
    assert result.level == "exact"
    assert result.segments == 3


@pytest.mark.parametrize(
    "quote",
    [
        "(...) Las partes podrán suscribir convenios (...)",
        "[...] Las partes podrán suscribir convenios",
        "… Las partes podrán suscribir convenios …",
        "Las partes podrán (...) (...) suscribir convenios",
        "Las partes podrán (...)   [...] suscribir convenios",
    ],
)
def test_empty_segments_are_dropped(quote: str) -> None:
    result = match_quote(quote, OMISSION)
    assert result.level == "exact"
    assert result.segments in (1, 2)
    assert result.matched_text.startswith("Las partes podrán")


def test_leading_and_trailing_marks_leave_a_single_segment() -> None:
    assert match_quote("(...) alfa beta gamma delta (...)", "alfa beta gamma delta").segments == 1


def test_adjacent_segments_are_in_order_and_may_touch() -> None:
    result = match_quote("uno dos (...) tres cuatro", "uno dos tres cuatro")
    assert result.level == "exact"
    assert result.matched_text == "uno dos tres cuatro"


def test_segments_must_appear_in_order() -> None:
    text = "a1 a2 a3 a4 b1 b2 b3 b4"
    assert match_quote("a1 a2 a3 a4 (...) b1 b2 b3 b4", text).level == "exact"
    result = match_quote("b1 b2 b3 b4 (...) a1 a2 a3 a4", text)
    assert result.level != "exact"
    # Nothing of the text is left for the second segment after the first one is aligned: the
    # score is 0.5, under the partial threshold of 0.6 (A8).
    assert result.level == "none"


def test_segments_may_not_overlap() -> None:
    text = "a b c d e f"
    result = match_quote("a b c d (...) c d e f", text)
    assert result.level != "exact"
    # First segment 4/4; the second finds only "e f" left: 2 / max(4, 2); mean (4 + 2) / 8.
    assert result.similarity == pytest.approx(0.75)
    assert result.level == "partial"


def test_the_same_words_twice_need_two_occurrences() -> None:
    quote = "uno dos tres cuatro (...) uno dos tres cuatro"
    assert match_quote(quote, "uno dos tres cuatro").level != "exact"
    twice = "uno dos tres cuatro cinco uno dos tres cuatro"
    assert match_quote(quote, twice).level == "exact"


def test_exact_segments_take_the_earliest_start_after_the_previous_segment() -> None:
    text = "Alfa beta x alfa beta y gamma delta z Gamma delta"
    result = match_quote("alfa beta (...) gamma delta", text)
    assert result.level == "exact"
    assert result.matched_text == "Alfa beta x alfa beta y gamma delta"


def test_quote_with_the_whole_text_is_exact() -> None:
    result = match_quote(LAW_5, LAW_5)
    assert result.level == "exact"
    assert result.matched_text == LAW_5.removesuffix(".")


# --- 1.2 match_quote: errors, results are well formed -------------------------------------------


@pytest.mark.parametrize(
    "quote",
    ["", "   ", "(...)", "...", "…", "[...] (...) …", "«»", " — ", "?! ¿", "(...) ... [...]"],
)
def test_a_quote_without_tokens_raises_value_error(quote: str) -> None:
    with pytest.raises(ValueError):
        match_quote(quote, "cualquier texto del artículo")


def test_text_without_tokens_gives_none() -> None:
    result = match_quote("uno dos tres cuatro", "")
    assert result.level == "none"
    assert result.matched_text == ""
    assert result.differences == ""


def test_quote_match_is_a_frozen_dataclass_with_the_specified_fields() -> None:
    result = match_quote("uno dos tres cuatro", "uno dos tres cuatro")
    assert dataclasses.is_dataclass(result)
    assert [f.name for f in dataclasses.fields(QuoteMatch)] == [
        "level",
        "similarity",
        "matched_text",
        "differences",
        "segments",
        "omitted",
        "flags",
        "gave_up",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.level = "none"  # type: ignore[misc]


def test_match_quote_is_deterministic() -> None:
    quote = "uno dos tres cuatro cinco seis"
    text = "uno dos x tres cuatro cinco y seis uno dos tres cuatro cinco seis"
    assert match_quote(quote, text) == match_quote(quote, text)


# --- 1.2 match_quote: thresholds ----------------------------------------------------------------


def fuzzy(n: int, changed: int) -> QuoteMatch:
    """Quote of ``n`` unique words with ``changed`` substituted, against its passage in a text."""
    passage = words(n)
    quote = " ".join(substituted(passage, spread(n, changed)))
    return match_quote(quote, surrounded(passage))


@pytest.mark.parametrize(
    ("n", "changed", "level", "similarity"),
    [
        (100, 1, "close", 0.99),
        (20, 2, "close", 0.90),
        (20, 3, "close", 0.85),  # the boundary itself is close
        (100, 15, "close", 0.85),
        (100, 16, "partial", 0.84),  # one token below the boundary
        (25, 4, "partial", 0.84),
        (20, 4, "partial", 0.80),
        (20, 8, "partial", 0.60),
        (100, 40, "partial", 0.60),  # the boundary itself is partial (A8: 0.6, was 0.5)
        (100, 41, "none", None),  # one token below the boundary
        (8, 4, "none", None),  # 0.5 was partial before A8
        (100, 50, "none", None),
        (9, 5, "none", None),
        (20, 15, "none", None),
    ],
)
def test_levels_on_both_sides_of_each_threshold(
    n: int, changed: int, level: str, similarity: float | None
) -> None:
    result = fuzzy(n, changed)
    assert result.level == level
    if similarity is not None:
        assert result.similarity == pytest.approx(similarity)
    else:
        # Windows under the threshold are never searched for, so a "none" carries no score
        # worth reading; it only has to stay below the threshold.
        assert result.similarity < 0.6


def test_close_and_partial_report_the_aligned_passage() -> None:
    passage = words(20)
    quote = " ".join(substituted(passage, spread(20, 3)))
    result = match_quote(quote, surrounded(passage))
    assert result.level == "close"
    assert result.matched_text == " ".join(passage)
    assert result.differences != ""
    assert result.segments == 1


def test_none_has_empty_matched_text_and_differences() -> None:
    result = fuzzy(100, 60)
    assert result.level == "none"
    assert result.matched_text == ""
    assert result.differences == ""


def test_similarity_stays_within_zero_and_one() -> None:
    for changed in (0, 1, 7, 19, 20):
        assert 0.0 <= fuzzy(20, changed).similarity <= 1.0


def test_unrelated_text_has_a_low_similarity() -> None:
    result = match_quote("alfa beta gamma delta epsilon zeta", "uno dos tres cuatro cinco seis")
    assert result.level == "none"
    assert result.similarity == pytest.approx(0.0)


# --- 1.2 match_quote: the similarity formula ----------------------------------------------------


def test_words_inserted_in_the_text_lower_the_similarity_by_the_longer_window() -> None:
    quote_words = words(10)
    law = [*quote_words[:5], "ins1", "ins2", "ins3", *quote_words[5:]]
    result = match_quote(" ".join(quote_words), surrounded(law))
    # 10 matching tokens, window of 13: 10 / max(10, 13). Dividing by the quote alone gives 1.0.
    assert result.similarity == pytest.approx(10 / 13)
    assert result.level == "partial"
    assert result.matched_text == " ".join(law)


def test_words_missing_from_the_text_lower_the_similarity_by_the_longer_quote() -> None:
    quote_words = words(10)
    law = [w for i, w in enumerate(quote_words) if i not in (3, 6)]
    result = match_quote(" ".join(quote_words), surrounded(law))
    # 8 matching tokens, window of 8: 8 / max(10, 8). Dividing by the window gives 1.0.
    assert result.similarity == pytest.approx(0.8)
    assert result.level == "partial"


def test_substitution_in_the_middle_of_the_window() -> None:
    quote_words = words(10)
    law = substituted(quote_words, [4])
    result = match_quote(" ".join(quote_words), surrounded(law))
    assert result.similarity == pytest.approx(0.9)


def test_the_window_runs_from_the_first_to_the_last_matched_token() -> None:
    passage = words(20)
    law = ["ajenoinicio", *passage[:-1], "ajenofinal"]  # first and last words differ
    quote = " ".join(passage)
    result = match_quote(quote, surrounded(law))
    assert result.similarity == pytest.approx(19 / 20)
    # The window does not include the unmatched first and last words of the passage.
    assert result.matched_text == " ".join(passage[:-1])


def test_the_best_window_is_chosen_among_several_candidates() -> None:
    passage = words(20)
    worse = substituted(passage, spread(20, 8))
    better = substituted(passage, spread(20, 2))
    text = " ".join([*words(5, "pre"), *worse, *words(5, "mid"), *better, *words(5, "post")])
    result = match_quote(" ".join(passage), text)
    assert result.similarity == pytest.approx(0.9)
    assert result.matched_text == " ".join(better)


def test_ties_between_windows_go_to_the_earliest() -> None:
    passage = words(20)
    first = substituted(passage, [10])
    second = substituted(passage, [10])
    first[0], second[0] = "VOZ0", "Voz0"  # same token after case folding, told apart verbatim
    text = " ".join([*first, *words(5, "mid"), *second])
    result = match_quote(" ".join(passage), text)
    assert result.similarity == pytest.approx(0.95)
    assert result.matched_text.startswith("VOZ0 ")
    assert "Voz0" not in result.matched_text


def test_ties_between_equal_windows_with_a_distinguishing_tail() -> None:
    quote = "alfa beta gamma zeta delta"
    text = "Alfa beta gamma eta delta, alfa beta gamma theta delta"
    result = match_quote(quote, text)
    assert result.level == "partial"
    assert result.similarity == pytest.approx(0.8)
    assert result.matched_text == "Alfa beta gamma eta delta"


def test_accents_are_kept_when_matching() -> None:
    result = match_quote("el termino del plazo legal", "Vence el término del plazo legal.")
    assert result.level == "partial"
    assert result.similarity == pytest.approx(0.8)
    assert "[-termino-]" in result.differences
    assert "{+término+}" in result.differences


def test_case_does_not_matter_when_matching() -> None:
    assert match_quote("EL TÉRMINO DEL PLAZO LEGAL", "vence el término del plazo legal").level == (
        "exact"
    )


def test_multi_segment_similarity_is_weighted_by_segment_length() -> None:
    # Segment 1 has 6 words and matches fully; segment 2 has 2 words matching 1 of 2 words.
    text = "a1 a2 a3 a4 a5 a6 relleno b1 zz"
    quote = "a1 a2 a3 a4 a5 a6 (...) b1 b2"
    result = match_quote(quote, text)
    # (6 * 1.0 + 2 * 0.5) / 8 = 0.875, not the unweighted mean 0.75.
    assert result.similarity == pytest.approx(0.875)
    assert result.level == "close"
    assert result.segments == 2


def test_multi_segment_matched_text_spans_from_first_to_last_window() -> None:
    text = "ini a1 a2 a3 a4 relleno b1 b2 b3 zz fin"
    result = match_quote("a1 a2 a3 a4 (...) b1 b2 b3 b4", text)
    assert result.level == "close"
    assert result.matched_text == "a1 a2 a3 a4 relleno b1 b2 b3"


# --- 1.2 match_quote: short quotes --------------------------------------------------------------


def test_short_exact_quote_is_exact() -> None:
    assert match_quote("derecho público", "sujetos de derecho público y privado").level == "exact"
    assert match_quote("derecho", "sujetos de derecho público").level == "exact"


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("uno dos tres", "uno dos x tres"),  # 3/4 = 0.75 would be partial
        ("uno dos tres", "uno dos y"),  # 2/3 would be partial
        ("uno dos", "uno x"),
        ("uno", "otro"),
    ],
)
def test_a_short_inexact_quote_is_none(quote: str, text: str) -> None:
    result = match_quote(quote, text)
    assert result.level == "none"
    assert result.similarity == 0.0  # K3
    assert result.matched_text == ""
    assert result.differences == ""


def test_four_tokens_is_long_enough_to_be_partial() -> None:
    result = match_quote("uno dos x tres", "uno dos y tres")
    assert result.level == "partial"
    assert result.similarity == pytest.approx(0.75)


def test_the_short_quote_rule_counts_the_tokens_of_the_whole_quote() -> None:
    # K3: the short-quote rule counts tokens over all segments together, so two 2-token segments
    # make a 4-token quote that may be partial.
    result = match_quote("alfa beta (...) gamma delta", "alfa beta uno gamma epsilon")
    assert result.level == "partial"
    assert result.similarity == pytest.approx(0.75)


# --- 1.2 match_quote: differences ---------------------------------------------------------------


def test_differences_notation_direction() -> None:
    result = match_quote("uno dos TRES cuatro cinco seis", "uno dos tres cuatro cinco seis")
    assert result.level == "exact"
    assert result.differences == ""
    result = match_quote("uno dos tres PERO cinco seis", "uno dos tres cuatro cinco seis")
    # "[-...-]" is a word of the QUOTE only, "{+...+}" a word of the LAW only.
    assert result.differences == "uno dos tres [-PERO-] {+cuatro+} cinco seis"


def test_differences_group_adjacent_words() -> None:
    result = match_quote("uno dos aa bb cc seis siete ocho", "uno dos xx seis siete ocho")
    assert result.differences == "uno dos [-aa bb cc-] {+xx+} seis siete ocho"


def test_differences_for_words_only_in_the_quote_or_only_in_the_law() -> None:
    only_quote = match_quote("uno dos tres sobra cuatro cinco", "uno dos tres cuatro cinco")
    assert only_quote.differences == "uno dos tres [-sobra-] cuatro cinco"
    only_law = match_quote("uno dos tres cuatro cinco", "uno dos tres nueva cuatro cinco")
    assert only_law.differences == "uno dos tres {+nueva+} cuatro cinco"


def test_differences_use_the_original_unfolded_words() -> None:
    # K2: words are displayed in their original spelling.
    quote = "Las Partes podrán SUSCRIBIR convenios con Sujetos de Derecho"
    law = "Las Partes podrán celebrar convenios con Sujetos de Derecho"
    result = match_quote(quote, law)
    assert (
        result.differences
        == "Las Partes podrán [-SUSCRIBIR-] {+celebrar+} convenios con Sujetos de Derecho"
    )


def test_multi_segment_differences_are_joined_with_the_omission_mark() -> None:
    text = "a1 a2 a3 zz a4 relleno b1 b2 b3 b4 b5"
    result = match_quote("a1 a2 a3 yy a4 (...) b1 b2 b3 b4 b5", text)
    assert result.level == "close"
    assert result.differences == "a1 a2 a3 [-yy-] {+zz+} a4 (...) b1 b2 b3 b4 b5"


def test_differences_are_capped_at_600_characters_with_an_ellipsis() -> None:
    passage = [f"palabra{i:03d}" for i in range(100)]
    law_words = substituted(passage, set(range(2, 100, 3)))
    result = match_quote(" ".join(passage), " ".join(law_words))
    assert result.level == "partial"
    assert result.differences.endswith("…")
    # K2: the text before the trailing ellipsis is at most 600 characters.
    assert 300 < len(result.differences) - 1 <= 600
    assert result.differences.startswith("palabra000 palabra001 [-palabra002-] {+ajeno2+}")


def test_short_differences_are_not_truncated() -> None:
    result = fuzzy(30, 3)
    assert not result.differences.endswith("…")
    assert len(result.differences) < 600


def test_exact_and_none_have_no_differences() -> None:
    assert match_quote("uno dos tres cuatro", "uno dos tres cuatro").differences == ""
    assert fuzzy(20, 15).differences == ""


# --- 1.2 realistic misquotes of Ley 40/2015 -----------------------------------------------------

ART_48_5 = (
    "Los convenios que incluyan compromisos financieros deberán ser financieramente sostenibles, "
    "debiendo quienes los suscriban tener capacidad para financiar los asumidos durante la "
    "vigencia del convenio."
)
ART_48_8 = (
    "Los convenios suscritos por la Administración General del Estado o alguno de sus "
    "organismos públicos o entidades de derecho público vinculados o dependientes resultarán "
    "eficaces una vez inscritos, en el plazo de 5 días hábiles desde su formalización, en el "
    "Registro Electrónico estatal de Órganos e Instrumentos de Cooperación del sector público "
    "estatal."
)


def test_realistic_one_word_changed_is_close() -> None:
    quote = ART_48_5.replace("deberán", "podrán")
    result = match_quote(quote, f"5. {ART_48_5}")
    assert result.level == "close"
    assert result.similarity == pytest.approx(21 / 22, abs=0.01)
    assert result.matched_text == ART_48_5.removesuffix(".")
    assert "[-podrán-] {+deberán+}" in result.differences


def test_realistic_changed_number_is_close() -> None:
    quote = ART_48_8.replace("5 días", "15 días")
    result = match_quote(quote, ART_48_8)
    assert result.level == "close"
    assert result.similarity > 0.95
    assert "[-15-] {+5+}" in result.differences


def test_realistic_dropped_clause_is_partial() -> None:
    quote = (
        "Los convenios que incluyan compromisos financieros deberán ser financieramente "
        "sostenibles durante la vigencia del convenio."
    )
    result = match_quote(quote, ART_48_5)
    # Aligning the whole sentence would give 15 / 24 (nine law words in the middle). Stopping at
    # "sostenibles" gives 10 / 15, which is higher, so that window is the best one.
    assert result.level == "partial"
    assert result.similarity == pytest.approx(10 / 15)
    assert result.matched_text.endswith("financieramente sostenibles")
    assert "[-durante la vigencia" in result.differences


def test_realistic_inserted_clause_in_a_short_quote_is_partial() -> None:
    quote = (
        "resultarán eficaces una vez inscritos en el Registro Electrónico estatal de Órganos "
        "e Instrumentos de Cooperación del sector público estatal"
    )
    result = match_quote(quote, ART_48_8)
    # The law adds nine words inside this 20-word quote. The whole window would score 20 / 29;
    # dropping the first five quoted words and aligning only the second half scores 15 / 20.
    assert result.level == "partial"
    assert result.similarity == pytest.approx(0.75)
    assert result.matched_text.startswith("en el Registro Electrónico")


def test_realistic_paraphrase_is_none() -> None:
    quote = (
        "Los convenios con compromisos financieros han de ser sostenibles desde el punto de "
        "vista financiero y los firmantes deben poder financiarlos mientras dure el convenio."
    )
    result = match_quote(quote, ART_48_5)
    assert result.level == "none"
    assert result.similarity < 0.4


def test_realistic_unrelated_sentence_is_none() -> None:
    quote = "Las Cortes Generales representan al pueblo español y ejercen la potestad legislativa."
    result = match_quote(quote, ART_48_5)
    assert result.level == "none"
    assert result.similarity < 0.4


# --- 1.2 performance ----------------------------------------------------------------------------


def test_a_300_token_quote_against_20000_tokens_is_fast_and_finds_the_passage() -> None:
    rng = random.Random(20261121)
    vocabulary = [f"t{i}" for i in range(6000)]
    text_words = [rng.choice(vocabulary) for _ in range(20_000)]
    start = 12_345
    passage = text_words[start : start + 300]
    quote_words = substituted(passage, {40, 130, 131, 250})
    text = " ".join(text_words)
    began = time.perf_counter()
    result = match_quote(" ".join(quote_words), text)
    elapsed = time.perf_counter() - began
    assert elapsed < 3.0
    assert result.level == "close"
    assert result.similarity == pytest.approx(296 / 300)
    assert result.matched_text == " ".join(passage)
    assert "[-ajeno40-] {+" in result.differences


def test_a_300_token_exact_quote_against_20000_tokens_is_fast() -> None:
    rng = random.Random(7)
    text_words = [rng.choice([f"t{i}" for i in range(500)]) for _ in range(20_000)]
    passage = text_words[19_000:19_300]
    began = time.perf_counter()
    result = match_quote(" ".join(passage), " ".join(text_words))
    assert time.perf_counter() - began < 3.0
    assert result.level == "exact"


# --- 1.3 match_versions -------------------------------------------------------------------------

D1 = date(2015, 10, 2)
D2 = date(2018, 1, 1)
D3 = date(2021, 1, 1)
QUOTE_10 = " ".join(words(10))


def three_versions(t1: str, t2: str, t3: str) -> Block:
    return make_block(text_version(t1, D1), text_version(t2, D2), text_version(t3, D3))


def test_current_is_the_version_in_effect_on_the_date() -> None:
    block = three_versions("alfa uno", "alfa dos", "alfa tres")
    expectations = [
        (date(2015, 10, 2), 1),
        (date(2017, 12, 31), 1),
        (date(2018, 1, 1), 2),
        (date(2020, 12, 31), 2),
        (date(2021, 1, 1), 3),
        (date(2099, 1, 1), 3),
    ]
    for on, position in expectations:
        found = match_versions("alfa", block, on)
        assert found.current is not None
        assert found.current.position == position, on


def test_current_carries_dates_and_the_quote_match() -> None:
    block = three_versions("uno dos tres cuatro", "otro texto distinto aquí", "y otro más allá")
    found = match_versions("uno dos tres cuatro", block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.current.starts_on == D1
    assert found.current.match == match_quote("uno dos tres cuatro", "uno dos tres cuatro")
    assert found.current.match.level == "exact"


def test_ends_on_is_the_day_before_the_next_version_starts() -> None:
    block = three_versions("uno dos tres cuatro", "uno dos tres cuatro", "uno dos tres cuatro")
    first = match_versions("uno dos tres cuatro", block, D1)
    assert first.current is not None
    assert first.current.ends_on == D2 - timedelta(days=1) == date(2017, 12, 31)
    second = match_versions("uno dos tres cuatro", block, D2)
    assert second.current is not None
    assert second.current.starts_on == D2
    assert second.current.ends_on == date(2020, 12, 31)


def test_the_latest_version_has_no_end_date() -> None:
    block = three_versions("uno dos tres cuatro", "otro", "uno dos tres cuatro")
    found = match_versions("uno dos tres cuatro", block, date(2022, 1, 1))
    assert found.current is not None
    assert found.current.position == 3
    assert found.current.ends_on is None


def test_a_version_dated_only_by_publication_uses_that_date() -> None:
    published = make_version(para("uno dos tres cuatro"), published=date(2010, 5, 5))
    found = match_versions("uno dos tres cuatro", make_block(published), date(2011, 1, 1))
    assert found.current is not None
    assert found.current.starts_on == date(2010, 5, 5)


def test_a_single_version_has_no_other_version() -> None:
    block = make_block(text_version("uno dos tres cuatro", D1))
    found = match_versions("uno dos tres cuatro", block, date(2020, 1, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.current.ends_on is None
    assert found.best_other is None


def test_a_single_inexact_version_has_no_other_version_either() -> None:
    block = make_block(text_version("uno dos tres cuatro", D1))
    found = match_versions("cinco seis siete ocho", block, date(2020, 1, 1))
    assert found.current is not None
    assert found.current.match.level == "none"
    assert found.best_other is None


def test_no_version_in_effect_means_no_current() -> None:
    block = three_versions("uno dos tres cuatro", "otro", "otro más")
    found = match_versions("uno dos tres cuatro", block, date(2000, 1, 1))
    assert found.current is None
    # K5: with no version in effect every version is a candidate for best_other.
    assert found.best_other is not None
    assert found.best_other.position == 1
    assert found.best_other.relation == "later"
    assert found.best_other.match.level == "exact"


def test_no_version_in_effect_and_nothing_matches() -> None:
    block = three_versions("uno dos tres cuatro", "otro", "otro más")
    found = match_versions("zzz yyy xxx www", block, date(2000, 1, 1))
    assert found.current is None
    assert found.best_other is None


def test_block_without_versions() -> None:
    found = match_versions("uno dos tres cuatro", make_block(), date(2020, 1, 1))
    assert found == CitationMatch(current=None, best_other=None)


def test_version_text_joins_paragraphs_with_newlines() -> None:
    version = make_version(para("Fin del uno."), para("Inicio del dos."), effective=D1)
    found = match_versions("fin del uno inicio del dos", make_block(version), D1)
    assert found.current is not None
    assert found.current.match.level == "exact"
    assert found.current.match.matched_text == "Fin del uno.\nInicio del dos"


def test_footnotes_and_quoted_paragraphs_are_not_the_version_text() -> None:
    # A3: only the article's own words are compared; the notes the BOE adds are kept apart.
    version = make_version(
        para("Artículo 8. Texto del artículo.", "articulo"),
        para("Se modifica por la disposición final segunda.", "nota_pie", quoted=True),
        para("Texto de una cita.", "parrafo", quoted=True),
        para("Se corrige por la disposición final tercera.", "nota_pie"),
        effective=D1,
    )
    block = make_block(version)
    for quote in (
        "se modifica por la disposición final",
        "segunda. Texto de una cita",
        "se corrige por la disposición final tercera",
    ):
        found = match_versions(quote, block, D1)
        assert found.current is not None
        assert found.current.match.level == "none"
        assert found.editorial is True
    own = match_versions("texto del artículo", block, D1)
    assert own.current is not None
    assert own.current.match.level == "exact"
    assert own.editorial is False


def test_editorial_is_not_set_when_a_version_text_matches_closely() -> None:
    version = make_version(
        para("uno dos tres cuatro cinco seis siete ocho"),
        para("uno dos tres cuatro cinco seis siete ocho", "nota_pie"),
        effective=D1,
    )
    found = match_versions("uno dos tres cuatro cinco seis siete ocho", make_block(version), D1)
    assert found.editorial is False


# --- 1.3 match_versions: which other version is reported ----------------------------------------


def variant(quote_words: Sequence[str], changed: int) -> str:
    """Text of ``len(quote_words)`` words with ``changed`` words replaced."""
    n = len(quote_words)
    return " ".join(substituted(quote_words, spread(n, changed)))


def test_older_exact_beats_a_current_partial() -> None:
    old, new = words(10), "voz0 voz1 xx voz3 yy voz5 zz voz7 ww voz9"
    block = make_block(text_version(" ".join(old), D1), text_version(new, D2))
    found = match_versions(" ".join(old), block, date(2019, 1, 1))
    assert found.current is not None
    assert found.current.position == 2
    assert found.current.match.level == "partial"
    assert found.best_other is not None
    assert found.best_other.position == 1
    assert found.best_other.starts_on == D1
    assert found.best_other.ends_on == D2 - timedelta(days=1)
    assert found.best_other.match.level == "exact"


def test_a_newer_version_can_be_the_better_other() -> None:
    quote_words = words(10)
    block = make_block(
        text_version(variant(quote_words, 5), D1),
        text_version(" ".join(quote_words), D2),
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.best_other is not None
    assert found.best_other.position == 2
    assert found.best_other.ends_on is None


def test_equal_exact_levels_do_not_make_the_other_better() -> None:
    same = "uno dos tres cuatro"
    block = three_versions(same, same, same)
    found = match_versions(same, block, date(2019, 1, 1))
    assert found.current is not None
    assert found.current.match.level == "exact"
    assert found.best_other is None


def test_equal_level_and_equal_similarity_is_not_better() -> None:
    quote_words = words(20)
    block = make_block(
        text_version(variant(quote_words, 4), D1),
        text_version(variant(quote_words, 4), D2),
    )
    found = match_versions(" ".join(quote_words), block, date(2019, 1, 1))
    assert found.current is not None
    assert found.current.match.similarity == pytest.approx(0.8)
    assert found.best_other is None


def test_an_other_version_that_matches_worse_is_not_reported() -> None:
    quote_words = words(20)
    block = make_block(
        text_version(variant(quote_words, 8), D1),
        text_version(variant(quote_words, 2), D2),
    )
    found = match_versions(" ".join(quote_words), block, date(2019, 1, 1))
    assert found.current is not None
    assert found.current.match.level == "close"
    assert found.best_other is None


@pytest.mark.parametrize(
    ("current_changed", "other_changed", "current_sim", "other_sim"),
    [
        (8, 7, 0.60, 0.65),  # the lowest "partial" pair; same level, margin exactly 0.05
        (7, 6, 0.65, 0.70),
        (5, 4, 0.75, 0.80),
        (3, 2, 0.85, 0.90),  # same level (close)
        (2, 1, 0.90, 0.95),
    ],
)
def test_same_level_needs_a_margin_of_005_in_similarity(
    current_changed: int, other_changed: int, current_sim: float, other_sim: float
) -> None:
    quote_words = words(20)
    # The current version is position 1; the other one is exactly 0.05 higher (K6 tolerance).
    block = make_block(
        text_version(variant(quote_words, current_changed), D1),
        text_version(variant(quote_words, other_changed), D2),
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.match.similarity == pytest.approx(current_sim)
    assert found.best_other is not None
    assert found.best_other.position == 2
    assert found.best_other.match.similarity == pytest.approx(other_sim)


def test_same_level_below_the_margin_is_not_reported() -> None:
    quote_words = words(25)
    # Current 19 / 25 = 0.76, other 20 / 25 = 0.80: only 0.04 higher.
    block = make_block(
        text_version(variant(quote_words, 6), D1),
        text_version(variant(quote_words, 5), D2),
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.match.similarity == pytest.approx(0.76)
    assert found.best_other is None


def test_same_level_clearly_above_the_margin_is_reported() -> None:
    quote_words = words(25)
    block = make_block(
        text_version(variant(quote_words, 8), D1),  # 17/25 = 0.68
        text_version(variant(quote_words, 5), D2),  # 20/25 = 0.80
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.best_other is not None
    assert found.best_other.position == 2


@pytest.mark.parametrize(
    ("current_changed", "other_changed", "current_level", "other_level"),
    [
        (16, 15, "partial", "close"),  # 0.84 vs 0.85: higher level, margin 0.01
        (1, 0, "close", "exact"),  # 0.99 vs 1.00
        (41, 40, "none", "partial"),  # 0.59 vs 0.60
    ],
)
def test_a_higher_level_wins_whatever_the_similarity_margin(
    current_changed: int, other_changed: int, current_level: str, other_level: str
) -> None:
    quote_words = words(100)
    block = make_block(
        text_version(variant(quote_words, current_changed), D1),
        text_version(variant(quote_words, other_changed), D2),
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.match.level == current_level
    assert found.best_other is not None
    assert found.best_other.position == 2
    assert found.best_other.match.level == other_level


def test_among_equal_other_versions_the_latest_wins() -> None:
    wording = " ".join(words(10))
    block = make_block(
        text_version("nada que ver con la cita aquí", D1),
        text_version(wording, D2),
        text_version(wording, date(2019, 1, 1)),
        text_version(wording, date(2020, 1, 1)),
    )
    found = match_versions(wording, block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.best_other is not None
    assert found.best_other.position == 4
    assert found.best_other.starts_on == date(2020, 1, 1)
    assert found.best_other.ends_on is None


def test_among_equal_close_others_the_latest_wins() -> None:
    quote_words = words(20)
    block = make_block(
        text_version("nada que ver con la cita aquí", D1),
        text_version(variant(quote_words, 2), D2),
        text_version(variant(quote_words, 2), date(2019, 1, 1)),
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.best_other is not None
    assert found.best_other.position == 3


def test_a_better_earlier_other_beats_a_later_worse_one() -> None:
    quote_words = words(20)
    block = make_block(
        text_version("nada que ver con la cita aquí", D1),
        text_version(" ".join(quote_words), D2),
        text_version(variant(quote_words, 2), date(2019, 1, 1)),
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.best_other is not None
    assert found.best_other.position == 2
    assert found.best_other.match.level == "exact"


def test_higher_similarity_beats_later_position_within_a_level() -> None:
    quote_words = words(25)
    block = make_block(
        text_version("nada que ver con la cita aquí", D1),
        text_version(variant(quote_words, 3), D2),  # 22/25 close
        text_version(variant(quote_words, 3 + 1), date(2019, 1, 1)),  # 21/25 partial
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.best_other is not None
    assert found.best_other.position == 2


def test_the_current_version_is_never_its_own_best_other() -> None:
    wording = " ".join(words(10))
    block = make_block(text_version(wording, D1), text_version("distinto", D2))
    found = match_versions(wording, block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.best_other is None


def test_a_version_that_matches_nothing_is_not_reported_as_best_other() -> None:
    # K4: a version at level "none" is never best_other, even when its similarity is 0.05 or
    # more above the current's.
    quote_words = words(20)
    current_text = " ".join(f"zz{i}" for i in range(20))  # shares nothing: similarity 0
    other_text = variant(quote_words, 14)  # 6 / 20 = 0.30: still "none"
    block = make_block(text_version(current_text, D1), text_version(other_text, D2))
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.match.level == "none"
    assert found.best_other is None


# --- 1.4 citation_status ------------------------------------------------------------------------


def status(**overrides: Any) -> str:
    """``citation_status`` with an in-force, found, exactly quoted citation by default."""
    arguments: dict[str, Any] = {
        "law_outcome": "found",
        "article_outcome": "found",
        "state": "in_force",
        "law_repealed": False,
        "quote_given": True,
        "match": citation_match("exact"),
    }
    arguments.update(overrides)
    return citation_status(**arguments)


@pytest.mark.parametrize("quote_given", [True, False])
@pytest.mark.parametrize("article_outcome", [None, "found", "ambiguous", "not_found"])
def test_law_not_found_beats_everything(quote_given: bool, article_outcome: str | None) -> None:
    assert (
        status(
            law_outcome="not_found",
            article_outcome=article_outcome,
            state=None,
            quote_given=quote_given,
            match=None,
        )
        == "law_not_found"
    )


@pytest.mark.parametrize("quote_given", [True, False])
@pytest.mark.parametrize("article_outcome", [None, "found", "ambiguous", "not_found"])
def test_law_ambiguous_beats_article_and_quote_outcomes(
    quote_given: bool, article_outcome: str | None
) -> None:
    assert (
        status(
            law_outcome="ambiguous",
            article_outcome=article_outcome,
            state=None,
            quote_given=quote_given,
            match=None,
        )
        == "law_ambiguous"
    )


def test_law_outcome_wins_even_with_a_perfect_match_and_a_repealed_law() -> None:
    assert status(law_outcome="not_found", law_repealed=True) == "law_not_found"
    assert status(law_outcome="ambiguous", law_repealed=True, state="repealed") == "law_ambiguous"


@pytest.mark.parametrize("quote_given", [True, False])
def test_article_not_found(quote_given: bool) -> None:
    assert (
        status(article_outcome="not_found", state=None, quote_given=quote_given, match=None)
        == "article_not_found"
    )


@pytest.mark.parametrize("quote_given", [True, False])
def test_article_ambiguous(quote_given: bool) -> None:
    assert (
        status(article_outcome="ambiguous", state=None, quote_given=quote_given, match=None)
        == "article_ambiguous"
    )


def test_article_outcomes_beat_state_repeal_and_match() -> None:
    assert status(article_outcome="not_found", law_repealed=True) == "article_not_found"
    assert status(article_outcome="ambiguous", state="repealed") == "article_ambiguous"
    assert status(article_outcome="not_found", match=citation_match("none", "exact")) == (
        "article_not_found"
    )


# Without a quote: only the state and the repeal of the law count.
@pytest.mark.parametrize(
    ("state", "law_repealed", "expected"),
    [
        ("in_force", False, "in_force"),  # A5: without a quote nothing can "match"
        ("in_force", True, "not_in_force"),
        ("repealed", False, "not_in_force"),
        ("repealed", True, "not_in_force"),
        ("not_yet_in_force", False, "not_in_force"),
        ("did_not_exist", False, "not_in_force"),
    ],
)
def test_without_a_quote(state: str, law_repealed: bool, expected: str) -> None:
    assert status(state=state, law_repealed=law_repealed, quote_given=False, match=None) == expected


def test_without_a_quote_any_match_is_ignored() -> None:
    assert status(quote_given=False, match=citation_match("none")) == "in_force"
    assert status(quote_given=False, match=citation_match("none", "exact")) == "in_force"
    assert status(quote_given=False, match=citation_match("partial", "exact")) == "in_force"


# With a quote: (current level, other level, state, law repealed) -> status.
QUOTED_ROWS: list[tuple[str, str | None, str | None, str, bool, str]] = [
    # id, current, other, state, law_repealed, expected
    # exact current, in force -> matches
    ("exact-in-force", "exact", None, "in_force", False, "matches"),
    # exact current but not in force -> not_in_force, never matches
    ("exact-repealed-state", "exact", None, "repealed", False, "not_in_force"),
    ("exact-not-yet", "exact", None, "not_yet_in_force", False, "not_in_force"),
    ("exact-did-not-exist", "exact", None, "did_not_exist", False, "not_in_force"),
    ("exact-law-repealed", "exact", None, "in_force", True, "not_in_force"),
    ("exact-both", "exact", None, "repealed", True, "not_in_force"),
    # A2: only an EXACT other version is "matches_older_version"; a merely close one is a near
    # miss and the status is text_differs.
    ("older-exact-current-close", "close", "exact", "in_force", False, "matches_older_version"),
    ("older-exact-current-partial", "partial", "exact", "in_force", False, "matches_older_version"),
    ("older-exact-current-none", "none", "exact", "in_force", False, "matches_older_version"),
    ("older-close-current-partial", "partial", "close", "in_force", False, "text_differs"),
    ("older-close-current-none", "none", "close", "in_force", False, "text_differs"),
    # A4: with no version in effect on the date the citation is not in force, whatever other
    # versions say (they are reported as other_version).
    ("older-exact-no-current", None, "exact", "in_force", False, "not_in_force"),
    ("older-close-no-current", None, "close", "in_force", False, "not_in_force"),
    # ... an exact other version is checked before not_in_force and before quote_not_found
    ("older-over-repealed", "partial", "exact", "repealed", False, "matches_older_version"),
    ("older-over-law-repealed", "none", "close", "in_force", True, "text_differs"),
    ("older-over-not-yet", None, "exact", "not_yet_in_force", False, "not_in_force"),
    ("older-over-did-not-exist", None, "close", "did_not_exist", False, "not_in_force"),
    # An exact current version cannot have a strictly better other version (match_versions never
    # builds it), so a close other next to an exact current is ignored.
    ("older-over-exact-current", "exact", "close", "repealed", False, "not_in_force"),
    # nothing found: current none, no other version exact or close
    ("none-none", "none", None, "in_force", False, "quote_not_found"),
    ("none-not-in-force", "none", None, "repealed", False, "quote_not_found"),
    ("none-law-repealed", "none", None, "in_force", True, "quote_not_found"),
    ("no-current-no-other", None, None, "did_not_exist", False, "not_in_force"),
    ("no-current-not-yet", None, None, "not_yet_in_force", False, "not_in_force"),
    # K7: a PARTIAL best_other is not exact/close, so a current "none" is quote_not_found even
    # though some version partly matches.
    ("none-other-partial", "none", "partial", "in_force", False, "quote_not_found"),
    ("no-current-other-partial", None, "partial", "did_not_exist", False, "not_in_force"),
    # current close or partial, no better older version -> text_differs
    ("close-only", "close", None, "in_force", False, "text_differs"),
    ("partial-only", "partial", None, "in_force", False, "text_differs"),
    ("partial-other-partial", "partial", "partial", "in_force", False, "text_differs"),
    ("close-other-partial", "close", "partial", "in_force", False, "text_differs"),
    # K7: not_in_force is reserved for an EXACT current match, so a misquote of an article that
    # is repealed or of a repealed law is text_differs, not not_in_force.
    ("close-repealed", "close", None, "repealed", False, "text_differs"),
    ("partial-law-repealed", "partial", None, "in_force", True, "text_differs"),
    ("partial-not-yet", "partial", None, "not_yet_in_force", False, "text_differs"),
]


@pytest.mark.parametrize(
    ("current", "other", "state", "law_repealed", "expected"),
    [row[1:] for row in QUOTED_ROWS],
    ids=[row[0] for row in QUOTED_ROWS],
)
def test_status_with_a_quote(
    current: str | None, other: str | None, state: str, law_repealed: bool, expected: str
) -> None:
    result = status(state=state, law_repealed=law_repealed, match=citation_match(current, other))
    assert result == expected


def test_a_quote_with_no_match_object_matches_nothing() -> None:
    assert status(match=None) == "quote_not_found"
    assert status(match=None, state="repealed") == "quote_not_found"


def test_older_version_is_checked_before_not_in_force_and_quote_not_found() -> None:
    # The orderings between the branches, one assertion per adjacent pair.
    older = citation_match("none", "exact")
    assert status(match=older, state="repealed", law_repealed=True) == "matches_older_version"
    missing = citation_match("none")
    assert status(match=missing, state="repealed", law_repealed=True) == "quote_not_found"
    exact = citation_match("exact")
    assert status(match=exact, state="repealed", law_repealed=True) == "not_in_force"
    assert status(match=exact) == "matches"
    assert status(match=citation_match("partial")) == "text_differs"


def test_a_quote_found_only_in_an_editorial_note_is_not_found() -> None:
    match = CitationMatch(current=version_match("none", 2, relation="current"), best_other=None)
    assert status(match=match) == "quote_not_found"
    noted = dataclasses.replace(match, editorial=True)
    assert status(match=noted) == "quote_not_found"
    partial = CitationMatch(
        current=version_match("partial", 2, relation="current"), best_other=None, editorial=True
    )
    assert status(match=partial) == "quote_not_found"  # the note explains the words better


def test_citation_status_takes_keyword_arguments_only() -> None:
    with pytest.raises(TypeError):
        citation_status("found", "found", "in_force", False, True, None)  # type: ignore[misc]


# --- end to end on the real article 48 of Ley 40/2015 -------------------------------------------


@pytest.fixture
def article_48(load_fixture: Callable[[str], bytes]) -> Block:
    """Article 48 of Ley 40/2015: the 2016 text and the 2021 text that adds a 5-day term."""
    return parse_block(load_fixture("texto_bloque_a48_two_versions.xml"))


OLD_WORDING = (
    "resultarán eficaces una vez inscritos en el Registro Electrónico estatal de Órganos e "
    "Instrumentos de Cooperación del sector público estatal"
)
NEW_WORDING = (
    "resultarán eficaces una vez inscritos, en el plazo de 5 días hábiles desde su "
    "formalización, en el Registro Electrónico estatal de Órganos e Instrumentos de "
    "Cooperación del sector público estatal"
)


def test_real_block_has_the_two_expected_versions(article_48: Block) -> None:
    assert [v.starts_on for v in article_48.versions] == [date(2016, 10, 2), date(2021, 1, 1)]


def test_real_new_wording_is_exact_in_the_current_version_only(article_48: Block) -> None:
    found = match_versions(NEW_WORDING, article_48, date(2022, 1, 1))
    assert found.current is not None
    assert found.current.position == 2
    assert found.current.match.level == "exact"
    assert found.current.match.matched_text == NEW_WORDING
    assert found.best_other is None
    assert (
        citation_status(
            law_outcome="found",
            article_outcome="found",
            state="in_force",
            law_repealed=False,
            quote_given=True,
            match=found,
        )
        == "matches"
    )


def test_real_old_wording_matches_the_older_version_on_a_recent_date(article_48: Block) -> None:
    found = match_versions(OLD_WORDING, article_48, date(2022, 1, 1))
    assert found.current is not None
    assert found.current.position == 2
    # The 2021 text inserts nine words inside the quoted clause; the best window is the second
    # half of the quote: 15 / 20.
    assert found.current.match.level == "partial"
    assert found.current.match.similarity == pytest.approx(0.75)
    assert "[-resultarán eficaces una vez inscritos-]" in found.current.match.differences
    assert found.best_other is not None
    assert found.best_other.position == 1
    assert found.best_other.starts_on == date(2016, 10, 2)
    assert found.best_other.ends_on == date(2020, 12, 31)
    assert found.best_other.match.level == "exact"
    assert found.best_other.relation == "earlier"
    assert (
        citation_status(
            law_outcome="found",
            article_outcome="found",
            state="in_force",
            law_repealed=False,
            quote_given=True,
            match=found,
        )
        == "matches_older_version"
    )


def test_real_old_wording_is_current_and_exact_during_its_own_period(article_48: Block) -> None:
    found = match_versions(OLD_WORDING, article_48, date(2018, 6, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.current.match.level == "exact"
    # The newer text only matches partially, which is not better than exact.
    assert found.best_other is None


def test_real_quote_with_omission_across_the_clause(article_48: Block) -> None:
    quote = "resultarán eficaces una vez inscritos (...) en el Registro Electrónico estatal"
    for on in (date(2018, 6, 1), date(2022, 1, 1)):
        found = match_versions(quote, article_48, on)
        assert found.current is not None
        assert found.current.match.level == "exact"
        assert found.current.match.segments == 2
        assert found.best_other is None


def test_real_misquote_matches_no_version(article_48: Block) -> None:
    quote = "Las Cortes Generales representan al pueblo español y ejercen la potestad legislativa"
    found = match_versions(quote, article_48, date(2022, 1, 1))
    assert found.current is not None
    assert found.current.match.level == "none"
    assert found.best_other is None
    assert (
        citation_status(
            law_outcome="found",
            article_outcome="found",
            state="in_force",
            law_repealed=False,
            quote_given=True,
            match=found,
        )
        == "quote_not_found"
    )


def test_real_changed_number_is_text_differs(article_48: Block) -> None:
    quote = NEW_WORDING.replace("5 días", "15 días")
    found = match_versions(quote, article_48, date(2022, 1, 1))
    assert found.current is not None
    assert found.current.match.level == "close"
    assert "[-15-] {+5+}" in found.current.match.differences
    assert found.best_other is None
    assert (
        citation_status(
            law_outcome="found",
            article_outcome="found",
            state="in_force",
            law_repealed=False,
            quote_given=True,
            match=found,
        )
        == "text_differs"
    )


def test_real_new_wording_before_it_took_effect_matches_the_newer_version(
    article_48: Block,
) -> None:
    found = match_versions(NEW_WORDING, article_48, date(2018, 6, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.best_other is not None
    assert found.best_other.position == 2
    assert found.best_other.starts_on == date(2021, 1, 1)
    assert found.best_other.ends_on is None
    assert found.best_other.match.level == "exact"
    assert found.best_other.relation == "later"
    assert (
        citation_status(
            law_outcome="found",
            article_outcome="found",
            state="in_force",
            law_repealed=False,
            quote_given=True,
            match=found,
        )
        == "matches_later_version"
    )


def test_real_footnote_of_the_amending_law_is_an_editorial_note_not_the_text(
    article_48: Block,
) -> None:
    # A3: the footnote is the BOE's note about the article, so the article's text does not match
    # it and the match says the words are found only in a note.
    found = match_versions(
        "Se modifica el apartado 8 por la disposición final 2.1 del Real Decreto-ley 36/2020",
        article_48,
        date(2022, 1, 1),
    )
    assert found.current is not None
    assert found.current.match.level == "none"
    assert found.editorial is True
    assert (
        citation_status(
            law_outcome="found",
            article_outcome="found",
            state="in_force",
            law_repealed=False,
            quote_given=True,
            match=found,
        )
        == "quote_not_found"
    )


# --- clarifications: K1, K2, K5, K7 --------------------------------------------------------------


def test_k1_tie_on_the_start_goes_to_the_shorter_window() -> None:
    # Window "alfa beta gamma epsilon" (4 words, 4 matches) and "... epsilon delta" (5 words,
    # 4 matches) both score 4 / 5 = 0.8 and start at the same word: the shorter wins.
    result = match_quote("alfa beta gamma delta epsilon", "alfa beta gamma epsilon delta")
    assert result.level == "partial"
    assert result.similarity == pytest.approx(0.8)
    assert result.matched_text == "alfa beta gamma epsilon"


def test_k1_the_best_window_need_not_cover_every_quote_word() -> None:
    # The unmatched first word is not part of the window: 7 matches over max(8, 7) words.
    result = match_quote(
        "xx alfa beta gamma delta epsilon zeta eta", "alfa beta gamma delta epsilon zeta eta"
    )
    assert result.similarity == pytest.approx(7 / 8)
    assert result.matched_text == "alfa beta gamma delta epsilon zeta eta"


def test_k2_case_punctuation_and_typography_are_not_differences() -> None:
    quote = "los convenios se perfeccionan por la manifestación del consentimiento de las partes"
    law = "8. Los convenios se perfeccionan por la prestación del consentimiento de las partes."
    result = match_quote(quote, law)
    assert result.level == "close"
    assert result.differences.count("[-") == 1
    assert result.differences.count("{+") == 1
    assert "[-manifestación-] {+prestación+}" in result.differences
    # Only the case of the unchanged words may differ from the lower-case quote.
    assert result.differences.casefold() == (
        "los convenios se perfeccionan por la [-manifestación-] {+prestación+} del "
        "consentimiento de las partes"
    )


def test_k2_a_comma_in_the_law_is_not_a_difference() -> None:
    quote = (
        "resultarán eficaces una vez inscritos en el plazo de 10 días hábiles desde su "
        "formalización en el Registro Electrónico estatal"
    )
    law = (
        "resultarán eficaces una vez inscritos, en el plazo de 5 días hábiles desde su "
        "formalización, en el Registro Electrónico estatal"
    )
    result = match_quote(quote, law)
    assert result.level == "close"
    assert result.differences.count("[-") == 1
    assert "[-10-] {+5+}" in result.differences
    assert "formalización-]" not in result.differences
    assert "{+formalización" not in result.differences


def test_k2_guillemets_and_dashes_are_not_differences() -> None:
    quote = "recurso contencioso-administrativo ante la sala distinta del Boletín Oficial"
    law = "recurso contencioso\u2013administrativo ante la sala competente del «Boletín Oficial»"
    result = match_quote(quote, law)
    assert result.differences.count("[-") == 1
    assert "[-distinta-] {+competente+}" in result.differences
    assert "«" not in result.differences
    assert "\u2013" not in result.differences


def test_k2_a_difference_only_in_the_punctuation_is_exact() -> None:
    assert match_quote("uno, dos; tres cuatro.", "uno dos tres cuatro").level == "exact"


def test_k2_accent_differences_still_appear() -> None:
    result = match_quote("el termino del plazo legal", "el término del plazo legal")
    assert "[-termino-] {+término+}" in result.differences


def test_k2_multi_segment_differences_ignore_punctuation_too() -> None:
    text = "A1 a2, a3 zz a4 relleno b1 b2. b3 b4 b5"
    result = match_quote("a1 a2 a3 yy a4 (...) b1 b2 b3 b4 b5", text)
    assert result.differences.casefold() == "a1 a2 a3 [-yy-] {+zz+} a4 (...) b1 b2 b3 b4 b5"


def starts(*dates: date) -> Block:
    """A block whose versions start on ``dates`` and carry the same quoted wording."""
    return make_block(*(text_version("uno dos tres cuatro", d) for d in dates))


def test_k5_an_earlier_other_version_is_earlier() -> None:
    block = three_versions("uno dos tres cuatro", "otro texto distinto aquí", "y otro más allá")
    found = match_versions("uno dos tres cuatro", block, date(2022, 1, 1))
    assert found.current is not None
    assert found.current.relation == "current"
    assert found.best_other is not None
    assert found.best_other.position == 1
    assert found.best_other.relation == "earlier"


def test_k5_a_later_other_version_is_later_and_may_be_best_other() -> None:
    block = three_versions("otro texto distinto aquí", "y otro más allá", "uno dos tres cuatro")
    found = match_versions("uno dos tres cuatro", block, date(2016, 1, 1))
    assert found.current is not None
    assert found.current.position == 1
    assert found.best_other is not None
    assert found.best_other.position == 3
    assert found.best_other.relation == "later"
    assert found.best_other.starts_on == D3


def test_k5_later_versions_are_candidates_alongside_earlier_ones() -> None:
    # Current is version 2; an equally exact earlier and later version tie: the latest wins.
    block = make_block(
        text_version("uno dos tres cuatro", D1),
        text_version("otro texto distinto aquí", D2),
        text_version("uno dos tres cuatro", D3),
    )
    found = match_versions("uno dos tres cuatro", block, date(2019, 1, 1))
    assert found.best_other is not None
    assert found.best_other.position == 3
    assert found.best_other.relation == "later"


def test_k5_no_version_in_effect_every_version_is_later() -> None:
    block = starts(D1, D2, D3)
    found = match_versions("uno dos tres cuatro", block, date(2000, 1, 1))
    assert found.current is None
    assert found.best_other is not None
    assert found.best_other.relation == "later"
    assert found.best_other.position == 3  # equal versions: the latest wins


def test_k5_a_one_version_block_that_starts_after_the_date() -> None:
    block = starts(D2)
    found = match_versions("uno dos tres cuatro", block, D1)
    assert found.current is None
    assert found.best_other is not None
    assert found.best_other.position == 1
    assert found.best_other.relation == "later"
    assert found.best_other.starts_on == D2
    assert found.best_other.ends_on is None
    assert found.best_other.match.level == "exact"


def test_k5_a_one_version_block_in_the_future_with_no_match_has_no_other() -> None:
    found = match_versions("cinco seis siete ocho", starts(D2), D1)
    assert found.current is None
    assert found.best_other is None


def test_k6_tolerance_keeps_the_exact_margin_pair_reported() -> None:
    # 0.60 - 0.55 is 0.04999999999999993 in floating point; K6 says it still counts as 0.05.
    assert 0.60 - 0.55 < 0.05
    quote_words = words(20)
    block = make_block(
        text_version(variant(quote_words, 9), D1), text_version(variant(quote_words, 8), D2)
    )
    found = match_versions(" ".join(quote_words), block, date(2016, 1, 1))
    assert found.best_other is not None


@pytest.mark.parametrize(
    ("current", "other", "relation", "expected"),
    [
        ("partial", "exact", "later", "matches_later_version"),
        ("partial", "close", "later", "text_differs"),  # A2: close is not exact
        ("none", "exact", "later", "matches_later_version"),
        ("close", "exact", "later", "matches_later_version"),
        (None, "exact", "later", "not_in_force"),  # A4: nothing in effect on the date
        (None, "close", "later", "not_in_force"),
        ("partial", "exact", "earlier", "matches_older_version"),
        (None, "exact", "earlier", "not_in_force"),
        ("partial", "partial", "later", "text_differs"),
        ("none", "partial", "later", "quote_not_found"),
        (None, "partial", "later", "not_in_force"),
    ],
)
@pytest.mark.parametrize(
    ("state", "law_repealed"),
    [("in_force", False), ("repealed", False), ("in_force", True), ("not_yet_in_force", False)],
)
def test_k7_later_version_status(
    current: str | None,
    other: str,
    relation: str,
    expected: str,
    state: str,
    law_repealed: bool,
) -> None:
    result = status(
        state=state, law_repealed=law_repealed, match=citation_match(current, other, relation)
    )
    assert result == expected


@pytest.mark.parametrize("quote_given", [True, False])
def test_k7_a_found_law_needs_an_article_outcome(quote_given: bool) -> None:
    with pytest.raises(ValueError):
        status(article_outcome=None, quote_given=quote_given)


def test_k7_a_missing_article_outcome_is_fine_when_the_law_is_not_usable() -> None:
    assert status(law_outcome="not_found", article_outcome=None) == "law_not_found"
    assert status(law_outcome="ambiguous", article_outcome=None) == "law_ambiguous"


# --- A1: a quote that is not in the article must not take long ------------------------------------


@pytest.fixture(scope="module")
def ley40_blocks() -> dict[str, Block]:
    """The blocks of the recorded Ley 40/2015 text, by block id."""
    from pathlib import Path

    from boe_insights_mcp.textparse import parse_full_text

    raw = Path(__file__).parent / "fixtures" / "boe" / "texto_full_ley40_2015_trimmed.xml"
    return {block.block_id: block for block in parse_full_text(raw.read_bytes())}


def own_text(block: Block) -> str:
    """The article's own words in its latest version."""
    return " ".join(p.text for p in block.versions[-1].own_paragraphs)


def test_a_350_word_quote_from_another_article_is_rejected_quickly(
    ley40_blocks: dict[str, Block],
) -> None:
    quote = " ".join(own_text(ley40_blocks["a19"]).split()[:350])
    started = time.monotonic()
    found = match_versions(quote, ley40_blocks["a48"], date(2026, 11, 21))
    assert time.monotonic() - started < 2.0
    assert found.current is not None
    assert found.current.match.level == "none"


def test_a_5000_character_quote_from_another_article_is_rejected_quickly(
    ley40_blocks: dict[str, Block],
) -> None:
    quote = own_text(ley40_blocks["a19"])[:5000]
    assert len(quote) == 5000
    started = time.monotonic()
    found = match_versions(quote, ley40_blocks["a50"], date(2026, 11, 21))
    assert time.monotonic() - started < 2.0
    assert found.current is not None
    assert found.current.match.level == "none"


def test_a_quote_that_nearly_appears_is_still_found_in_a_long_article(
    ley40_blocks: dict[str, Block],
) -> None:
    words_ = own_text(ley40_blocks["a19"]).split()[100:400]
    words_[40] = "zzzz"
    found = match_versions(" ".join(words_), ley40_blocks["a19"], date(2026, 11, 21))
    assert found.current is not None
    assert found.current.match.level == "close"


def test_a_short_segment_next_to_a_long_exact_one_still_counts() -> None:
    # The floor of a segment drops with the others' size: 4 exact words and 2 missing words
    # score (4 + 0) / 6 = 0.67, which is "partial", not "none".
    result = match_quote("uno dos tres cuatro (...) zz yy", "uno dos tres cuatro cinco seis")
    assert result.level == "partial"
    assert result.similarity == pytest.approx(4 / 6)


# --- A6 / A7: omissions and one-word changes that can alter the meaning --------------------------


def test_an_exact_multi_segment_match_reports_what_was_omitted() -> None:
    text = "Los firmantes no podrán superar los gastos. Otra frase. Fin del texto."
    result = match_quote("Los firmantes (...) superar los gastos (...) Fin del texto", text)
    assert result.level == "exact"
    # Verbatim between the segments' words, so the full stop that ended the sentence is included.
    assert result.omitted == ("no podrán", ". Otra frase.")
    assert result.flags == ("omitted_negation",)


def test_an_exact_single_segment_has_nothing_omitted() -> None:
    result = match_quote("uno dos tres cuatro", "uno dos tres cuatro cinco")
    assert result.omitted == ()
    assert result.flags == ()


def test_omitted_numbers_are_flagged() -> None:
    result = match_quote("plazo (...) días", "plazo de 15 días")
    assert result.omitted == ("de 15",)
    assert result.flags == ("omitted_number",)


def test_a_dropped_negation_is_flagged() -> None:
    law = "las aportaciones no podrán ser superiores a los gastos derivados del convenio"
    result = match_quote(
        "las aportaciones podrán ser superiores a los gastos derivados del convenio", law
    )
    assert result.level == "close"
    assert result.flags == ("negation",)


def test_a_changed_number_is_flagged_by_digit_and_by_word() -> None:
    law = "se publicará en el plazo de 5 días hábiles desde su formalización en el registro"
    digits = match_quote(law.replace("5", "15"), law)
    assert digits.level == "close"
    assert digits.flags == ("number",)
    spelled = match_quote(law.replace("5", "cinco"), law)
    assert spelled.flags == ("number",)


def test_other_differences_carry_no_flag() -> None:
    law = "los convenios se perfeccionan por la prestación del consentimiento de las partes"
    result = match_quote(law.replace("prestación", "manifestación"), law)
    assert result.level == "close"
    assert result.flags == ()


# --- A9: typography ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "law"),
    [
        ("el n.º 5 del artículo", "el nº 5 del artículo"),
        ("la 1.ª edición", "la 1ª edición"),
        ("hasta 3.000 euros", "hasta 3000 euros"),
        ("hasta 3000 euros", "hasta 3.000 euros"),
        ("hasta 1.200.000 euros", "hasta 1200000 euros"),
        ("el 1° apartado", "el 1º apartado"),
    ],
)
def test_typography_variants_are_the_same_words(quote: str, law: str) -> None:
    assert match_quote(quote, law).level == "exact"


def test_a_decimal_is_not_a_thousands_separator() -> None:
    assert comparison_tokens("2.50 euros") == ["2", "50", "euros"]
    assert comparison_tokens("3.0001") == ["3", "0001"]


def test_the_matched_text_keeps_the_original_typography() -> None:
    result = match_quote("hasta 3000 euros", "Multa de hasta 3.000 euros anuales")
    assert result.matched_text == "hasta 3.000 euros"


def test_a_long_quote_of_the_articles_own_words_shuffled_gives_up_in_bounded_time(
    ley40_blocks: dict[str, Block],
) -> None:
    # Every quote word occurs in the text, so no pruning bound helps: without a work budget this
    # search takes minutes. The budget makes it stop and say so.
    article = own_text(ley40_blocks["a19"])
    own_words = article.split()
    assert len(own_words) >= 600
    shuffled = random.Random(20261122).sample(own_words, 300)
    started = time.monotonic()
    found = match_quote(" ".join(shuffled), article)
    assert time.monotonic() - started < 5.0
    assert found.gave_up or found.level != "none"
    assert not found.gave_up or (found.level == "none" and found.matched_text == "")


def test_a_realistic_search_does_not_give_up(ley40_blocks: dict[str, Block]) -> None:
    quote = " ".join(own_text(ley40_blocks["a19"]).split()[:350])
    found = match_quote(quote, own_text(ley40_blocks["a48"]))
    assert found.level == "none"
    assert not found.gave_up
