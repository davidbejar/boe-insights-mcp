"""Contract tests for ``boe_insights_mcp.versions``: version selection, repeal, diff, history.

Most tests build the models directly with the small factories below, so a parser bug cannot be
mistaken for a versions bug. The last section runs a few checks on real fixture data parsed through
``parse_block``; it is clearly separated.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import date

import pytest
from pydantic import ValidationError

from boe_insights_mcp.models import Block, BlockVersion, Paragraph
from boe_insights_mcp.textparse import parse_block
from boe_insights_mcp.versions import (
    ParagraphChange,
    VersionDiff,
    VersionEvent,
    diff_versions,
    history,
    is_repealed,
    state_on,
    version_on,
)

Loader = Callable[[str], bytes]

HEADING = "Artículo 40. Potestad de inspección."


# --- factories ----------------------------------------------------------------------------------


def para(text: str, kind: str = "parrafo", quoted: bool = False) -> Paragraph:
    return Paragraph(kind=kind, text=text, quoted=quoted)


def make_version(
    *paragraphs: Paragraph,
    law: str = "BOE-A-2000-1",
    effective: date | None = None,
    published: date | None = None,
) -> BlockVersion:
    return BlockVersion(
        source_law_id=law,
        published_on=published,
        effective_from=effective,
        paragraphs=paragraphs,
    )


def texts_version(texts: Sequence[str], notes: Sequence[str] = ()) -> BlockVersion:
    """A version whose body paragraphs are ``texts`` followed by one footnote per ``notes``."""
    body = [para(t) for t in texts]
    footnotes = [para(n, "nota_pie", quoted=True) for n in notes]
    return make_version(*body, *footnotes)


def make_block(*versions: BlockVersion) -> Block:
    return Block(block_id="a40", kind="precepto", title="Artículo 40", versions=tuple(versions))


def law_version(law: str, effective: date | None, published: date | None = None) -> BlockVersion:
    """A version with distinguishable identity, for selection tests."""
    return make_version(para(f"text of {law}"), law=law, effective=effective, published=published)


def repeal_version(marker: str, *, heading: bool = True, footnote: bool = False) -> BlockVersion:
    paragraphs = [para(HEADING, "articulo")] if heading else []
    paragraphs.append(para(marker))
    if footnote:
        paragraphs.append(para("Se deroga por la disposición derogatoria única.", "nota_pie", True))
    return make_version(*paragraphs)


def changes_of(diff: VersionDiff, op: str) -> list[ParagraphChange]:
    return [c for c in diff.changes if c.op == op]


# --- version_on ---------------------------------------------------------------------------------

V1 = law_version("v1", date(2020, 1, 1))
V2 = law_version("v2", date(2022, 6, 1))


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(2019, 12, 31), None),
        (date(2020, 1, 1), "v1"),
        (date(2022, 5, 31), "v1"),
        (date(2022, 6, 1), "v2"),
        (date(2099, 1, 1), "v2"),
    ],
)
def test_version_on_picks_the_latest_version_effective_by_that_day(
    on: date, expected: str | None
) -> None:
    found = version_on(make_block(V1, V2), on)
    assert (found.source_law_id if found else None) == expected


def test_version_on_returns_the_version_object_itself() -> None:
    assert version_on(make_block(V1, V2), date(2021, 1, 1)) is V1


def test_version_without_effective_date_counts_from_the_beginning() -> None:
    undated = law_version("undated", None)
    block = make_block(undated, V2)
    assert version_on(block, date(1900, 1, 1)) is undated
    assert version_on(block, date(2022, 6, 1)) is V2


def test_version_on_is_none_when_every_version_starts_in_the_future() -> None:
    assert version_on(make_block(V1, V2), date(2000, 1, 1)) is None


def test_version_on_is_none_for_a_block_without_versions() -> None:
    assert version_on(make_block(), date(2020, 1, 1)) is None


def test_version_on_prefers_the_later_of_two_versions_with_the_same_date() -> None:
    first = law_version("first", date(2021, 1, 1))
    second = law_version("second", date(2021, 1, 1))
    assert version_on(make_block(first, second), date(2021, 1, 1)) is second


# --- is_repealed: marker variants ---------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "(Derogado).",
        "Derogado",
        "(Derogada)",
        "(Suprimido).",
        "(Sin contenido).",
        "(Anulado).",
        "(DEROGADO).",
        "(sin contenido)",
    ],
)
def test_repeal_marker_variants_are_detected_after_a_heading(marker: str) -> None:
    assert is_repealed(repeal_version(marker)) is True


@pytest.mark.parametrize("marker", ["(Derogado).", "Sin contenido"])
def test_repeal_marker_is_detected_without_a_heading_paragraph(marker: str) -> None:
    assert is_repealed(repeal_version(marker, heading=False)) is True


def test_footnotes_are_ignored_when_detecting_a_repeal() -> None:
    assert is_repealed(repeal_version("(Derogado).", footnote=True)) is True


def test_a_quoted_footnote_variant_class_is_ignored_too() -> None:
    version = make_version(
        para("(Derogado)."), para("nota", "nota_pie_2", quoted=True), para("otra", "nota_pie")
    )
    assert is_repealed(version) is True


# --- is_repealed: things that are not repeals ---------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(["El reglamento ha sido derogado por la ley."], id="mentions-derogado"),
        pytest.param(["Queda derogado el artículo 5 de la Ley 1/2000."], id="starts-with-queda"),
        pytest.param(["1. Texto vigente.", "(Derogado)."], id="marker-after-body-text"),
        pytest.param(["(Derogado).", "2. Texto vigente del apartado."], id="body-after-marker"),
        pytest.param(["Derogado parcialmente por la ley 3/2010."], id="marker-with-extra-words"),
        pytest.param(["Texto sin contenido alguno relevante."], id="sin-contenido-in-sentence"),
    ],
)
def test_text_that_merely_mentions_a_repeal_is_not_repealed(body: list[str]) -> None:
    version = make_version(para(HEADING, "articulo"), *(para(t) for t in body))
    assert is_repealed(version) is False


def test_a_heading_without_a_body_is_not_repealed() -> None:
    assert is_repealed(make_version(para(HEADING, "articulo"))) is False


def test_a_version_without_paragraphs_is_not_repealed() -> None:
    assert is_repealed(make_version()) is False


def test_a_version_with_only_footnotes_is_not_repealed() -> None:
    assert is_repealed(make_version(para("nota", "nota_pie", True))) is False


# --- state_on -----------------------------------------------------------------------------------


def _lifecycle_block() -> Block:
    original = law_version("orig", date(2018, 1, 1), published=date(2017, 12, 1))
    repeal = make_version(
        para(HEADING, "articulo"),
        para("(Derogado)."),
        law="repeal",
        effective=date(2020, 1, 1),
        published=date(2019, 12, 1),
    )
    return make_block(original, repeal)


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(2017, 11, 30), "did_not_exist"),
        (date(2017, 12, 1), "not_yet_in_force"),
        (date(2018, 1, 1), "in_force"),
        (date(2020, 1, 1), "repealed"),
        (date(2030, 1, 1), "repealed"),
    ],
)
def test_state_on_walks_through_the_whole_life_of_an_article(on: date, expected: str) -> None:
    assert state_on(_lifecycle_block(), on) == expected


def test_state_on_the_day_before_publication_is_did_not_exist() -> None:
    block = make_block(law_version("only", date(2027, 7, 1), published=date(2026, 11, 20)))
    assert state_on(block, date(2026, 11, 19)) == "did_not_exist"


def test_state_on_the_publication_day_is_not_yet_in_force() -> None:
    block = make_block(law_version("only", date(2027, 7, 1), published=date(2026, 11, 20)))
    assert state_on(block, date(2026, 11, 20)) == "not_yet_in_force"


def test_state_on_the_day_before_effect_is_still_not_yet_in_force() -> None:
    block = make_block(law_version("only", date(2027, 7, 1), published=date(2026, 11, 20)))
    assert state_on(block, date(2027, 6, 30)) == "not_yet_in_force"


def test_without_a_publication_date_a_pending_version_means_did_not_exist() -> None:
    block = make_block(law_version("only", date(2027, 7, 1), published=None))
    assert state_on(block, date(2026, 11, 20)) == "did_not_exist"


def test_the_earliest_versions_publication_date_decides_not_a_later_ones() -> None:
    early = law_version("early", date(2030, 1, 1), published=date(2029, 5, 1))
    late = law_version("late", date(2031, 1, 1), published=date(2030, 6, 1))
    block = make_block(early, late)
    assert state_on(block, date(2029, 4, 30)) == "did_not_exist"
    assert state_on(block, date(2029, 6, 1)) == "not_yet_in_force"


def test_a_block_without_versions_did_not_exist() -> None:
    assert state_on(make_block(), date(2026, 1, 1)) == "did_not_exist"


def test_a_version_without_effective_date_is_in_force_at_any_date() -> None:
    block = make_block(law_version("undated", None))
    assert state_on(block, date(1950, 1, 1)) == "in_force"


def test_an_article_amended_after_a_repeal_is_in_force_again() -> None:
    repeal = make_version(
        para(HEADING, "articulo"), para("(Derogado)."), effective=date(2020, 1, 1)
    )
    restored = law_version("restored", date(2023, 1, 1))
    block = make_block(law_version("orig", date(2018, 1, 1)), repeal, restored)
    assert state_on(block, date(2021, 1, 1)) == "repealed"
    assert state_on(block, date(2023, 1, 1)) == "in_force"


# --- diff_versions: paragraph-level operations --------------------------------------------------

PARAS = ["Primero.", "Segundo párrafo con varias palabras.", "Tercero.", "Cuarto."]


def test_identical_versions_have_only_unchanged_paragraphs() -> None:
    diff = diff_versions(texts_version(PARAS), texts_version(PARAS))
    assert diff.identical is True
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (0, 0, 0, 4)
    assert [c.op for c in diff.changes] == ["unchanged"] * 4
    assert diff.unified == ""


def test_unchanged_entries_carry_the_same_text_on_both_sides_and_no_inline() -> None:
    diff = diff_versions(texts_version(["Igual."]), texts_version(["Igual."]))
    (only,) = diff.changes
    assert (only.old_text, only.new_text, only.inline) == ("Igual.", "Igual.", None)


def test_pure_additions_are_reported_as_added() -> None:
    old = texts_version(["A uno.", "C tres."])
    new = texts_version(["A uno.", "B dos nuevo.", "C tres.", "D cuatro nuevo."])
    diff = diff_versions(old, new)
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (2, 0, 0, 2)
    assert [(c.op, c.old_text, c.new_text) for c in changes_of(diff, "added")] == [
        ("added", None, "B dos nuevo."),
        ("added", None, "D cuatro nuevo."),
    ]
    assert diff.identical is False


def test_pure_removals_are_reported_as_removed() -> None:
    old = texts_version(["A uno.", "B dos viejo.", "C tres.", "D cuatro viejo."])
    new = texts_version(["A uno.", "C tres."])
    diff = diff_versions(old, new)
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (0, 2, 0, 2)
    assert [(c.old_text, c.new_text) for c in changes_of(diff, "removed")] == [
        ("B dos viejo.", None),
        ("D cuatro viejo.", None),
    ]


def test_a_reworded_paragraph_is_changed_with_both_texts() -> None:
    old = texts_version(["Inicio.", "El plazo será de diez días hábiles.", "Fin."])
    new = texts_version(["Inicio.", "El plazo será de veinte días hábiles.", "Fin."])
    diff = diff_versions(old, new)
    (change,) = changes_of(diff, "changed")
    assert change.old_text == "El plazo será de diez días hábiles."
    assert change.new_text == "El plazo será de veinte días hábiles."
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (0, 0, 1, 2)


def test_changes_list_every_paragraph_in_reading_order() -> None:
    old = texts_version(["Uno.", "Dos con texto antiguo bastante largo.", "Tres."])
    new = texts_version(["Uno.", "Dos con texto nuevo bastante largo.", "Tres.", "Cuatro."])
    diff = diff_versions(old, new)
    assert [c.op for c in diff.changes] == ["unchanged", "changed", "unchanged", "added"]
    assert [c.new_text for c in diff.changes][0::2] == ["Uno.", "Tres."]


def test_an_equal_length_replaced_run_pairs_paragraphs_as_changed_even_when_dissimilar() -> None:
    old = texts_version(["Inicio.", "Aaaa bbbb", "Fin."])
    new = texts_version(["Inicio.", "Zzzz yyyy", "Fin."])
    diff = diff_versions(old, new)
    assert (diff.added, diff.removed, diff.changed) == (0, 0, 1)


def test_an_equal_length_run_of_two_pairs_each_by_position() -> None:
    old = texts_version(["Inicio.", "Alfa uno dos.", "Beta tres cuatro.", "Fin."])
    new = texts_version(["Inicio.", "Alfa uno nuevo.", "Beta tres otro.", "Fin."])
    pairs = [(c.old_text, c.new_text) for c in changes_of(diff_versions(old, new), "changed")]
    assert pairs == [("Alfa uno dos.", "Alfa uno nuevo."), ("Beta tres cuatro.", "Beta tres otro.")]


def test_unequal_run_pairs_by_similarity_and_reports_the_remainder_as_removed() -> None:
    similar_a = "El plazo será de diez días hábiles"
    similar_b = "Los contribuyentes deberán presentar la declaración"
    old = texts_version(["Inicio", similar_a, "Zzz qqq www", similar_b, "Fin"])
    new = texts_version(
        [
            "Inicio",
            "El plazo será de veinte días hábiles",
            "Los contribuyentes deberán presentar la declaración anual",
            "Fin",
        ]
    )
    diff = diff_versions(old, new)
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (0, 1, 2, 2)
    assert [c.old_text for c in changes_of(diff, "removed")] == ["Zzz qqq www"]
    assert {(c.old_text, c.new_text) for c in changes_of(diff, "changed")} == {
        (similar_a, "El plazo será de veinte días hábiles"),
        (similar_b, "Los contribuyentes deberán presentar la declaración anual"),
    }


def test_unequal_run_pairs_by_similarity_and_reports_the_remainder_as_added() -> None:
    old = texts_version(["Inicio", "El plazo será de diez días hábiles", "Fin"])
    new = texts_version(
        [
            "Inicio",
            "Texto nuevo totalmente distinto",
            "El plazo será de veinte días hábiles",
            "Otro añadido sin relación alguna",
            "Fin",
        ]
    )
    diff = diff_versions(old, new)
    assert (diff.added, diff.removed, diff.changed) == (2, 0, 1)
    (changed,) = changes_of(diff, "changed")
    assert changed.new_text == "El plazo será de veinte días hábiles"
    assert {c.new_text for c in changes_of(diff, "added")} == {
        "Texto nuevo totalmente distinto",
        "Otro añadido sin relación alguna",
    }


def test_unequal_run_without_similar_paragraphs_is_removed_and_added() -> None:
    old = texts_version(["Inicio", "Qwerty asdfg zxcvb", "Fin"])
    new = texts_version(["Inicio", "Mnbvc lkjhg poiuy", "Ploki mjuhy tgbnr", "Fin"])
    diff = diff_versions(old, new)
    assert (diff.added, diff.removed, diff.changed) == (2, 1, 0)


def test_diff_from_an_empty_version_shows_everything_as_added() -> None:
    diff = diff_versions(make_version(), texts_version(["Uno.", "Dos."]))
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (2, 0, 0, 0)


def test_diff_of_two_empty_versions_is_identical() -> None:
    diff = diff_versions(make_version(), make_version())
    assert diff.identical is True
    assert diff.changes == ()


# --- diff_versions: footnotes, counts, flags ----------------------------------------------------


def test_footnotes_are_excluded_from_the_comparison() -> None:
    old = texts_version(["Cuerpo."], notes=["Nota vieja."])
    new = texts_version(["Cuerpo."], notes=["Nota nueva.", "Otra nota."])
    diff = diff_versions(old, new)
    assert diff.identical is True
    assert [c.op for c in diff.changes] == ["unchanged"]
    assert diff.unified == ""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["a b c", "d e f"], ["a b c", "d e f"]),
        (["a b c", "x y z", "u v w"], ["a b c"]),
        (
            ["Inicio", "El plazo será de diez días", "Fin"],
            ["Inicio", "El plazo será de veinte días", "Fin"],
        ),
    ],
)
def test_counts_are_consistent_with_the_changes_tuple(old: list[str], new: list[str]) -> None:
    diff = diff_versions(texts_version(old), texts_version(new))
    ops = [c.op for c in diff.changes]
    assert diff.added == ops.count("added")
    assert diff.removed == ops.count("removed")
    assert diff.changed == ops.count("changed")
    assert diff.unchanged == ops.count("unchanged")
    assert len(diff.changes) == diff.added + diff.removed + diff.changed + diff.unchanged
    assert diff.unchanged + diff.removed + diff.changed == len(old)
    assert diff.unchanged + diff.added + diff.changed == len(new)


@pytest.mark.parametrize(
    ("old", "new", "identical"),
    [
        (["a"], ["a"], True),
        (["a", "b"], ["a"], False),
        (["a b"], ["a c"], False),
    ],
)
def test_identical_flag_means_no_added_removed_or_changed(
    old: list[str], new: list[str], identical: bool
) -> None:
    assert diff_versions(texts_version(old), texts_version(new)).identical is identical


def test_diff_is_deterministic() -> None:
    old = texts_version(["Inicio", "Uno dos tres cuatro", "Cinco seis", "Fin"])
    new = texts_version(["Inicio", "Uno dos tres nuevo", "Siete ocho", "Nueve", "Fin"])
    assert diff_versions(old, new) == diff_versions(old, new)


# --- diff_versions: unified diff ----------------------------------------------------------------


def test_unified_is_empty_when_identical() -> None:
    assert diff_versions(texts_version(["a"]), texts_version(["a"])).unified == ""


def test_unified_has_minus_and_plus_lines_for_a_change() -> None:
    old_line = "El plazo será de diez días hábiles."
    new_line = "El plazo será de veinte días hábiles."
    diff = diff_versions(texts_version(["Inicio.", old_line]), texts_version(["Inicio.", new_line]))
    lines = diff.unified.splitlines()
    assert any(
        line.startswith("-") and not line.startswith("---") and old_line in line for line in lines
    )
    assert any(
        line.startswith("+") and not line.startswith("+++") and new_line in line for line in lines
    )


def test_unified_for_a_pure_addition_has_a_plus_line_and_no_minus_line_of_content() -> None:
    diff = diff_versions(texts_version(["Uno."]), texts_version(["Uno.", "Dos nuevo."]))
    lines = diff.unified.splitlines()
    assert any(line.startswith("+") and "Dos nuevo." in line for line in lines)
    assert not any(line.startswith("-") and not line.startswith("---") for line in lines)


# --- diff_versions: inline word-level marks -----------------------------------------------------


def _strip_marks(inline: str, *, keep: str) -> str:
    """Rebuild the old (``keep="old"``) or new (``keep="new"``) text from an inline rendering."""
    if keep == "old":
        rebuilt = re.sub(r"\{\+.*?\+\}", "", inline)
        rebuilt = rebuilt.replace("[-", "").replace("-]", "")
    else:
        rebuilt = re.sub(r"\[-.*?-\]", "", inline)
        rebuilt = rebuilt.replace("{+", "").replace("+}", "")
    return " ".join(rebuilt.split())


def _inline_of(old: str, new: str) -> str:
    diff = diff_versions(texts_version([old]), texts_version([new]))
    (change,) = changes_of(diff, "changed")
    assert change.inline is not None
    return change.inline


def test_inline_marks_the_removed_and_added_words() -> None:
    inline = _inline_of(
        "El plazo será de diez días hábiles.", "El plazo será de veinte días hábiles."
    )
    assert "[-diez-]" in inline
    assert "{+veinte+}" in inline


def test_inline_leaves_unchanged_words_outside_the_marks() -> None:
    inline = _inline_of(
        "El plazo será de diez días hábiles.", "El plazo será de veinte días hábiles."
    )
    outside = re.sub(r"\[-.*?-\]|\{\+.*?\+\}", "", inline)
    assert outside.split() == ["El", "plazo", "será", "de", "días", "hábiles."]


def test_inline_keeps_punctuation_attached_to_its_word() -> None:
    inline = _inline_of("El plazo de diez, días", "El plazo de veinte, días")
    assert "[-diez,-]" in inline
    assert "{+veinte,+}" in inline


def test_inline_marks_a_pure_word_addition_without_a_removal() -> None:
    inline = _inline_of("uno dos tres", "uno dos nuevo tres")
    assert "{+nuevo+}" in inline
    assert "[-" not in inline


def test_inline_marks_a_pure_word_removal_without_an_addition() -> None:
    inline = _inline_of("uno dos sobra tres", "uno dos tres")
    assert "[-sobra-]" in inline
    assert "{+" not in inline


def test_inline_can_be_unmarked_back_to_both_texts() -> None:
    old = "Los sujetos pasivos deberán presentar la declaración en el plazo de diez días."
    new = (
        "Los obligados tributarios deberán presentar la autoliquidación en el plazo de veinte días."
    )
    inline = _inline_of(old, new)
    assert _strip_marks(inline, keep="old") == old
    assert _strip_marks(inline, keep="new") == new


def test_inline_is_only_present_for_changed_paragraphs() -> None:
    old = texts_version(["Igual.", "Quitar texto viejo.", "Mismo texto aquí con cambio pequeño."])
    new = texts_version(["Igual.", "Mismo texto aquí con cambio chico.", "Añadir texto nuevo."])
    diff = diff_versions(old, new)
    for change in diff.changes:
        if change.op == "changed":
            assert change.inline
        else:
            assert change.inline is None


# --- diff models are frozen ---------------------------------------------------------------------


def test_version_diff_and_changes_are_frozen() -> None:
    diff = diff_versions(texts_version(["a"]), texts_version(["b"]))
    with pytest.raises(ValidationError):
        diff.added = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        diff.changes[0].op = "added"  # type: ignore[misc]


# --- history ------------------------------------------------------------------------------------


def _three_version_block() -> Block:
    original = make_version(
        para(HEADING, "articulo"),
        para("Primer párrafo del texto original con detalle."),
        para("Segundo párrafo del texto original con otro detalle."),
        law="orig",
        effective=date(2018, 1, 1),
        published=date(2017, 12, 1),
    )
    amended = make_version(
        para(HEADING, "articulo"),
        para("Primer párrafo del texto original con detalle."),
        para("Segundo párrafo del texto modificado con otro detalle."),
        para("Tercer párrafo añadido."),
        law="amend",
        effective=date(2019, 1, 1),
        published=date(2018, 12, 1),
    )
    repealed = make_version(
        para(HEADING, "articulo"),
        para("(Derogado)."),
        para("Se deroga por una ley.", "nota_pie", True),
        law="repeal",
        effective=date(2020, 1, 1),
        published=date(2019, 12, 1),
    )
    return make_block(original, amended, repealed)


def test_history_has_one_event_per_version_with_one_based_index() -> None:
    events = history(_three_version_block())
    assert [e.index for e in events] == [1, 2, 3]


def test_history_kinds_are_original_amended_repealed() -> None:
    assert [e.kind for e in history(_three_version_block())] == [
        "original",
        "amended",
        "repealed",
    ]


def test_history_events_carry_the_version_identity() -> None:
    events = history(_three_version_block())
    assert [e.source_law_id for e in events] == ["orig", "amend", "repeal"]
    assert [e.effective_from for e in events] == [
        date(2018, 1, 1),
        date(2019, 1, 1),
        date(2020, 1, 1),
    ]
    assert [e.published_on for e in events] == [
        date(2017, 12, 1),
        date(2018, 12, 1),
        date(2019, 12, 1),
    ]


def test_history_original_event_has_zero_counts() -> None:
    first = history(_three_version_block())[0]
    assert (first.added, first.removed, first.changed) == (0, 0, 0)


def test_history_counts_are_versus_the_previous_version() -> None:
    _, amended, repealed = history(_three_version_block())
    assert (amended.added, amended.removed, amended.changed) == (1, 0, 1)
    assert (repealed.added, repealed.removed, repealed.changed) == (1, 3, 0)


def test_history_counts_match_diff_versions_for_each_step() -> None:
    block = _three_version_block()
    events = history(block)
    for previous, current, event in zip(
        block.versions, block.versions[1:], events[1:], strict=False
    ):
        diff = diff_versions(previous, current)
        assert (event.added, event.removed, event.changed) == (
            diff.added,
            diff.removed,
            diff.changed,
        )


def test_history_of_a_single_version_block_is_one_original_event() -> None:
    block = make_block(law_version("only", date(2020, 1, 1)))
    (event,) = history(block)
    assert (event.index, event.kind, event.source_law_id) == (1, "original", "only")


def test_history_of_a_block_without_versions_is_empty() -> None:
    assert history(make_block()) == ()


def test_history_first_version_is_original_even_when_it_is_a_repeal_marker() -> None:
    block = make_block(repeal_version("(Derogado)."))
    assert [e.kind for e in history(block)] == ["original"]


def test_history_later_non_marker_version_after_a_repeal_is_amended() -> None:
    block = make_block(
        law_version("orig", date(2018, 1, 1)),
        repeal_version("(Derogado)."),
        law_version("restored", date(2023, 1, 1)),
    )
    assert [e.kind for e in history(block)] == ["original", "repealed", "amended"]


def test_history_is_a_tuple_of_frozen_events() -> None:
    events = history(_three_version_block())
    assert isinstance(events, tuple)
    assert all(isinstance(e, VersionEvent) for e in events)
    with pytest.raises(ValidationError):
        events[0].kind = "amended"  # type: ignore[misc]


# === Real fixture data, parsed through textparse (a failure here may be a parser bug) ===========


@pytest.fixture
def repealed_block(load_fixture: Loader) -> Block:
    return parse_block(load_fixture("texto_bloque_a40_repealed_article_DERIVED.xml"))


@pytest.fixture
def two_version_block(load_fixture: Loader) -> Block:
    return parse_block(load_fixture("texto_bloque_a48_two_versions.xml"))


def test_fixture_repealed_article_history_is_original_then_repealed(repealed_block: Block) -> None:
    events = history(repealed_block)
    assert [(e.index, e.kind) for e in events] == [(1, "original"), (2, "repealed")]
    assert [e.source_law_id for e in events] == ["BOE-A-1999-23750", "BOE-A-2018-10751"]
    assert events[1].effective_from == date(2018, 7, 31)


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(1999, 12, 14), "not_yet_in_force"),
        (date(2018, 7, 30), "in_force"),
        (date(2018, 7, 31), "repealed"),
    ],
)
def test_fixture_repealed_article_states(repealed_block: Block, on: date, expected: str) -> None:
    assert state_on(repealed_block, on) == expected


def test_fixture_repeal_replaces_the_body_by_the_marker(repealed_block: Block) -> None:
    diff = diff_versions(*repealed_block.versions)
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (1, 4, 0, 1)


def test_fixture_two_version_article_selects_versions_by_date(two_version_block: Block) -> None:
    on = two_version_block
    assert version_on(on, date(2016, 10, 1)) is None
    first = version_on(on, date(2016, 10, 2))
    second = version_on(on, date(2021, 1, 1))
    assert first is not None and first.source_law_id == "BOE-A-2015-10566"
    assert second is not None and second.source_law_id == "BOE-A-2020-17340"


def test_fixture_two_version_article_states_around_publication(two_version_block: Block) -> None:
    assert state_on(two_version_block, date(2015, 10, 1)) == "did_not_exist"
    assert state_on(two_version_block, date(2016, 1, 1)) == "not_yet_in_force"
    assert state_on(two_version_block, date(2017, 1, 1)) == "in_force"


def test_fixture_two_version_article_diff_finds_the_one_reworded_paragraph(
    two_version_block: Block,
) -> None:
    diff = diff_versions(*two_version_block.versions)
    assert (diff.added, diff.removed, diff.changed, diff.unchanged) == (0, 0, 1, 11)
    (change,) = changes_of(diff, "changed")
    assert change.inline is not None
    assert "[-" in change.inline
    assert "{+" in change.inline


def test_fixture_future_version_is_not_yet_in_force(load_fixture: Loader) -> None:
    block = parse_block(load_fixture("texto_bloque_future_fecha_vigencia.xml"))
    assert state_on(block, date(2026, 11, 21)) == "not_yet_in_force"
    assert state_on(block, date(2026, 11, 19)) == "did_not_exist"
    assert state_on(block, date(2027, 7, 1)) == "in_force"
    assert version_on(block, date(2026, 11, 21)) is None
