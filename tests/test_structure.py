"""Contract tests for ``boe_insights_mcp.structure`` (spec A2): the table of contents of a law.

Most tests build small ``Block`` objects with the factories below, so each test shows one rule and
a parser bug cannot be mistaken for a structure bug. The last sections run end-to-end checks on the
real ``texto_full_*`` fixtures parsed with ``parse_full_text``. Rules cited as C1 to C8
are the "Clarifications to Part A" of the spec.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date

import pytest

from boe_insights_mcp.models import Block, BlockVersion, Paragraph
from boe_insights_mcp.structure import (
    GazettePublication,
    StructureEntry,
    build_structure,
    parse_publication_note,
)
from boe_insights_mcp.textparse import parse_full_text

Loader = Callable[[str], bytes]

ON = date(2026, 11, 21)
LONG_AGO = date(2000, 1, 1)

ASTURIAS = "texto_full_asturias_ley8_2026_not_yet_in_force_trimmed.xml"
LEY40 = "texto_full_ley40_2015_trimmed.xml"
CODIGO_CIVIL = "texto_full_codigo_civil_trimmed.xml"


# --- factories ---------------------------------------------------------------------------------


def para(kind: str, text: str) -> Paragraph:
    return Paragraph(kind=kind, text=text)


def version(
    *paragraphs: Paragraph,
    effective: date | None = LONG_AGO,
    published: date | None = None,
) -> BlockVersion:
    return BlockVersion(
        source_law_id="BOE-A-2000-1",
        published_on=published or effective,
        effective_from=effective,
        paragraphs=paragraphs,
    )


def block(
    block_id: str,
    kind: str,
    *paragraphs: Paragraph,
    title: str = "",
    versions: tuple[BlockVersion, ...] | None = None,
    expires_on: date | None = None,
    expired_marker: bool = False,
) -> Block:
    return Block(
        block_id=block_id,
        kind=kind,
        title=title,
        versions=versions if versions is not None else (version(*paragraphs),),
        expires_on=expires_on,
        expired_marker=expired_marker,
    )


def heading(block_id: str, *paragraphs: Paragraph, **kwargs: object) -> Block:
    return block(block_id, "encabezado", *paragraphs, **kwargs)  # type: ignore[arg-type]


def article(block_id: str, text: str = "Artículo 1. Objeto.", **kwargs: object) -> Block:
    return block(block_id, "precepto", para("articulo", text), **kwargs)  # type: ignore[arg-type]


def title_h(block_id: str, num: str = "TÍTULO I", tit: str = "Título") -> Block:
    return heading(block_id, para("titulo_num", num), para("titulo_tit", tit))


def chapter_h(block_id: str, num: str = "CAPÍTULO I", tit: str = "Capítulo") -> Block:
    return heading(block_id, para("capitulo_num", num), para("capitulo_tit", tit))


def book_h(block_id: str) -> Block:
    return heading(block_id, para("libro_num", "LIBRO I"), para("libro_tit", "Libro"))


def section_h(block_id: str, text: str = "Sección 1.ª Sección") -> Block:
    return heading(block_id, para("seccion", text))


def subsection_h(block_id: str, text: str = "Subsección 1.ª Subsección") -> Block:
    return heading(block_id, para("subseccion", text))


def group_h(block_id: str, text: str = "DISPOSICIONES ADICIONALES") -> Block:
    return heading(block_id, para("capitulo", text))


def annex_h(block_id: str, text: str = "ANEXO") -> Block:
    return heading(block_id, para("anexo", text))


def disposition(block_id: str, text: str = "Disposición final primera. Título.") -> Block:
    return article(block_id, text)


def build(*blocks: Block, on: date = ON) -> list[StructureEntry]:
    return build_structure(list(blocks), on)


def ids(entries: list[StructureEntry]) -> list[str]:
    return [e.block_id for e in entries]


def levels(entries: list[StructureEntry]) -> list[int]:
    return [e.level for e in entries]


def kinds(entries: list[StructureEntry]) -> list[str]:
    return [e.kind for e in entries]


def label_heading(entries: list[StructureEntry]) -> list[tuple[str, str]]:
    return [(e.label, e.heading) for e in entries]


# --- StructureEntry ------------------------------------------------------------------------------


def test_entry_is_a_frozen_dataclass_with_the_spec_fields() -> None:
    entry = StructureEntry(
        block_id="a1",
        kind="article",
        level=1,
        label="Artículo 1",
        heading="Objeto",
        state="in_force",
    )
    assert dataclasses.is_dataclass(entry)
    assert [f.name for f in dataclasses.fields(entry)] == [
        "block_id",
        "kind",
        "level",
        "label",
        "heading",
        "state",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.level = 2  # type: ignore[misc]


def test_build_structure_returns_entries_with_the_spec_values() -> None:
    (entry,) = build(article("a27", "Artículo 27. Garantía del derecho a la protección."))
    assert entry == StructureEntry(
        block_id="a27",
        kind="article",
        level=1,
        label="Artículo 27",
        heading="Garantía del derecho a la protección",
        state="in_force",
    )


def test_empty_input_gives_no_entries() -> None:
    assert build_structure([], ON) == []


def test_input_is_not_modified_and_the_result_is_a_list() -> None:
    blocks = [article("a1"), title_h("t")]
    result = build_structure(blocks, ON)
    assert isinstance(result, list)
    assert [b.block_id for b in blocks] == ["a1", "t"]


# --- which blocks produce entries ------------------------------------------------------------


def test_one_entry_per_block_in_input_order() -> None:
    entries = build(title_h("t"), chapter_h("c"), article("a1"), article("a2", "Artículo 2. B."))
    assert ids(entries) == ["t", "c", "a1", "a2"]


def test_firma_blocks_are_skipped() -> None:
    entries = build(article("a1"), block("fi", "firma", para("parrafo_2", "Madrid, 1 de enero.")))
    assert ids(entries) == ["a1"]


def test_nota_inicial_blocks_are_skipped() -> None:
    note = block("co", "nota_inicial", para("textoCompleto", "Incluye la corrección de errores."))
    assert ids(build(note, article("a1"))) == ["a1"]


def test_block_without_any_version_is_skipped() -> None:
    assert ids(build(article("a1", versions=()), article("a2", "Artículo 2. B."))) == ["a2"]


def test_block_with_a_version_in_effect_is_included() -> None:
    assert ids(build(article("a1"))) == ["a1"]


def test_block_whose_only_version_starts_later_is_included_as_not_yet_in_force() -> None:
    future = version(para("articulo", "Artículo 1. Objeto."), effective=date(2027, 1, 1))
    future = future.model_copy(update={"published_on": date(2026, 10, 1)})
    (entry,) = build(article("a1", versions=(future,)))
    assert (entry.block_id, entry.label, entry.heading) == ("a1", "Artículo 1", "Objeto")
    assert entry.state == "not_yet_in_force"


def test_not_yet_in_force_block_uses_its_earliest_version_for_the_texts() -> None:
    first = version(
        para("articulo", "Artículo 1. Primero."), effective=date(2027, 1, 1)
    ).model_copy(update={"published_on": date(2026, 10, 1)})
    second = version(para("articulo", "Artículo 1. Segundo."), effective=date(2028, 1, 1))
    (entry,) = build(article("a1", versions=(first, second)))
    assert entry.heading == "Primero"
    assert entry.state == "not_yet_in_force"


def test_in_force_block_with_a_later_version_uses_the_version_in_effect() -> None:
    old = version(para("articulo", "Artículo 1. Vigente."), effective=date(2000, 1, 1))
    later = version(para("articulo", "Artículo 1. Futuro."), effective=date(2030, 1, 1))
    (entry,) = build(article("a1", versions=(old, later)))
    assert entry.heading == "Vigente"
    assert entry.state == "in_force"


def test_block_not_yet_published_on_the_date_is_skipped() -> None:
    later = version(para("articulo", "Artículo 1. Objeto."), effective=date(2027, 1, 1))
    assert ids(build(article("a1", versions=(later,)), article("a2", "Artículo 2. B."))) == ["a2"]


def test_block_published_today_but_effective_later_is_not_yet_in_force() -> None:
    v = version(para("articulo", "Artículo 1. Objeto."), effective=date(2027, 1, 1))
    v = v.model_copy(update={"published_on": ON})
    (entry,) = build(article("a1", versions=(v,)))
    assert entry.state == "not_yet_in_force"


def test_not_yet_published_block_is_included_once_published() -> None:
    v = version(para("articulo", "Artículo 1. Objeto."), effective=date(2027, 1, 1))
    v = v.model_copy(update={"published_on": date(2026, 12, 1)})
    block_ = article("a1", versions=(v,))
    assert build(block_, on=date(2026, 11, 30)) == []
    assert ids(build(block_, on=date(2026, 12, 1))) == ["a1"]


def test_heading_not_yet_published_is_skipped_and_does_not_parent() -> None:
    later = version(para("capitulo_num", "CAPÍTULO I"), effective=date(2030, 1, 1))
    entries = build(title_h("t"), heading("c", versions=(later,)), article("a1"))
    assert ids(entries) == ["t", "a1"]
    assert levels(entries) == [1, 2]


# --- expiry --------------------------------------------------------------------------------------


def test_block_expired_in_the_past_is_skipped() -> None:
    expired = article("a1", expires_on=date(2003, 7, 10), expired_marker=True)
    assert ids(build(expired, article("a2", "Artículo 2. B."))) == ["a2"]


def test_block_expiring_on_the_date_itself_is_skipped() -> None:
    expired = article("a1", expires_on=ON, expired_marker=True)
    assert build(expired) == []


def test_block_expiring_the_day_after_is_kept() -> None:
    alive = article("a1", expires_on=date(2026, 11, 22), expired_marker=True)
    assert ids(build(alive)) == ["a1"]


def test_expiry_is_compared_with_the_requested_date() -> None:
    old = version(para("articulo", "Artículo 307."), effective=date(1889, 8, 16))
    block_1983 = article(
        "art307", versions=(old,), expires_on=date(1983, 11, 15), expired_marker=True
    )
    assert ids(build(block_1983, on=date(1983, 11, 14))) == ["art307"]
    assert build(block_1983, on=date(1983, 11, 15)) == []
    assert build(block_1983, on=date(2020, 1, 1)) == []


def test_marker_without_a_date_is_skipped_whatever_the_date() -> None:
    # The sentinel ``00000001`` parses to expires_on=None with expired_marker=True.
    sentinel = article("art.1873bis", expired_marker=True)
    assert build(sentinel) == []
    assert build(sentinel, on=date(1950, 1, 1)) == []


def test_expired_blocks_with_later_versions_are_still_skipped() -> None:
    # Known data oddity (FINDINGS D.4): versions dated after the expiry say nothing.
    later = version(para("articulo", "Artículo 1. B."), effective=date(2010, 1, 1))
    expired = article("a1", versions=(later,), expires_on=date(2003, 7, 10), expired_marker=True)
    assert build(expired) == []


def test_expired_heading_is_skipped_and_does_not_act_as_a_parent() -> None:
    gone = chapter_h("c", "CAPÍTULO II", "Del matrimonio canónico")
    gone = gone.model_copy(update={"expires_on": date(1981, 7, 20), "expired_marker": True})
    entries = build(title_h("t"), gone, article("a1"))
    assert ids(entries) == ["t", "a1"]
    assert levels(entries) == [1, 2]


def test_skipped_firma_does_not_change_the_levels_around_it() -> None:
    fi = block("fi", "firma", para("parrafo", "Firma."))
    assert levels(build(title_h("t"), fi, article("a1"))) == [1, 2]


# --- heading classes: num / tit ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "kind", "num", "tit"),
    [
        ("libro", "book", "LIBRO PRIMERO", "De las personas"),
        ("titulo", "title", "TÍTULO I", "Disposiciones generales"),
        ("capitulo", "chapter", "CAPÍTULO II", "Objeto, ámbito de aplicación y principios"),
    ],
)
def test_num_tit_headings(prefix: str, kind: str, num: str, tit: str) -> None:
    (entry,) = build(heading("h", para(f"{prefix}_num", num), para(f"{prefix}_tit", tit)))
    assert (entry.kind, entry.label, entry.heading) == (kind, num, tit)


@pytest.mark.parametrize(
    ("prefix", "kind"), [("libro", "book"), ("titulo", "title"), ("capitulo", "chapter")]
)
def test_num_only_heading_has_an_empty_heading_text(prefix: str, kind: str) -> None:
    (entry,) = build(heading("h", para(f"{prefix}_num", "TÍTULO X")))
    assert (entry.kind, entry.label, entry.heading) == (kind, "TÍTULO X", "")


@pytest.mark.parametrize(
    ("prefix", "kind"), [("libro", "book"), ("titulo", "title"), ("capitulo", "chapter")]
)
def test_tit_only_heading_has_an_empty_label(prefix: str, kind: str) -> None:
    (entry,) = build(heading("h", para(f"{prefix}_tit", "Disposición general")))
    assert (entry.kind, entry.label, entry.heading) == (kind, "", "Disposición general")


def test_heading_paragraph_order_does_not_matter_and_other_paragraphs_are_ignored() -> None:
    h = heading(
        "h",
        para("parrafo", "Téngase en cuenta que..."),
        para("titulo_tit", "Disposiciones generales"),
        para("titulo_num", "TÍTULO I"),
        para("nota_pie", "Nota."),
    )
    (entry,) = build(h)
    assert (entry.label, entry.heading) == ("TÍTULO I", "Disposiciones generales")


# --- heading classes: single paragraph -------------------------------------------------------


def test_section_is_split_after_the_ordinal() -> None:
    (entry,) = build(section_h("s", "Sección 1.ª De la guarda"))
    assert (entry.kind, entry.label, entry.heading) == ("section", "Sección 1.ª", "De la guarda")


def test_subsection_is_split_after_the_ordinal() -> None:
    (entry,) = build(subsection_h("s", "Subsección 2.ª De los órganos colegiados"))
    assert (entry.kind, entry.label, entry.heading) == (
        "subsection",
        "Subsección 2.ª",
        "De los órganos colegiados",
    )


def test_two_digit_ordinal_is_recognised() -> None:
    (entry,) = build(section_h("s", "Sección 12.ª Competencia"))
    assert (entry.label, entry.heading) == ("Sección 12.ª", "Competencia")


def test_ordinal_only_section_has_an_empty_heading() -> None:
    (entry,) = build(section_h("s", "Sección 3.ª"))
    assert (entry.kind, entry.label, entry.heading) == ("section", "Sección 3.ª", "")


def test_section_heading_text_with_further_periods_is_kept_whole() -> None:
    (entry,) = build(section_h("s", "Sección 1.ª Del art. 5.º y siguientes"))
    assert (entry.label, entry.heading) == ("Sección 1.ª", "Del art. 5.º y siguientes")


def test_section_without_an_ordinal_is_all_label() -> None:
    (entry,) = build(section_h("s", "Disposiciones generales"))
    assert (entry.kind, entry.label, entry.heading) == ("section", "Disposiciones generales", "")


def test_group_heading_is_all_label() -> None:
    (entry,) = build(group_h("g", "DISPOSICIONES ADICIONALES"))
    assert (entry.kind, entry.label, entry.heading) == ("group", "DISPOSICIONES ADICIONALES", "")


def test_annex_heading_is_all_label() -> None:
    (entry,) = build(annex_h("an", "CÓDIGO CIVIL"))
    assert (entry.kind, entry.label, entry.heading) == ("annex", "CÓDIGO CIVIL", "")


def test_unrecognised_heading_block_is_kind_other_with_the_block_title_as_label() -> None:
    (entry,) = build(heading("h", para("parrafo", "Algo raro."), title="Cabecera rara"))
    assert (entry.kind, entry.label, entry.heading) == ("other", "Cabecera rara", "")


def test_unrecognised_heading_block_without_title_uses_its_first_paragraph() -> None:
    (entry,) = build(heading("h", para("parrafo", "Primera línea."), para("parrafo", "Segunda.")))
    assert (entry.kind, entry.label, entry.heading) == ("other", "Primera línea.", "")


# --- C2: ordinal splitting ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "label", "heading"),
    [
        (
            "Sección primera. De la tutela de los menores",
            "Sección primera",
            "De la tutela de los menores",
        ),
        ("Sección Primera De la tutela", "Sección Primera", "De la tutela"),
        ("Sección segunda. Otra", "Sección segunda", "Otra"),
        ("Sección tercera Algo", "Sección tercera", "Algo"),
        ("Sección cuarta. Algo", "Sección cuarta", "Algo"),
        ("Sección séptima. Algo", "Sección séptima", "Algo"),
        ("Sección septima. Algo", "Sección septima", "Algo"),
        ("Sección undécima. Algo", "Sección undécima", "Algo"),
        ("Sección decimo Algo", "Sección decimo", "Algo"),
        ("Sección duodécima. Algo", "Sección duodécima", "Algo"),
        ("Sección única. Algo", "Sección única", "Algo"),
        ("Sección unica Algo", "Sección unica", "Algo"),
        ("Sección primer Algo", "Sección primer", "Algo"),
        ("SECCIÓN SEGUNDA. ALGO", "SECCIÓN SEGUNDA", "ALGO"),
        ("Sección IV Del régimen", "Sección IV", "Del régimen"),
        ("Sección IV. Del régimen", "Sección IV", "Del régimen"),
        ("Sección 2.º De otra", "Sección 2.º", "De otra"),
        ("Sección 3 De otra", "Sección 3", "De otra"),
        ("Sección 1.ª De la guarda.", "Sección 1.ª", "De la guarda"),
        ("  Sección 1.ª De la guarda", "Sección 1.ª", "De la guarda"),
    ],
)
def test_section_ordinal_forms(text: str, label: str, heading: str) -> None:
    (entry,) = build(section_h("s", text))
    assert (entry.kind, entry.label, entry.heading) == ("section", label, heading)


def test_real_subsection_with_a_leading_space() -> None:
    (entry,) = build(subsection_h("ss", " Subsección 1.ª De la autocontratación"))
    assert (entry.kind, entry.label, entry.heading) == (
        "subsection",
        "Subsección 1.ª",
        "De la autocontratación",
    )


def test_real_subsection_with_a_paragraph_sign_ordinal() -> None:
    (entry,) = build(subsection_h("ss", "§ 2.º Del saneamiento por los vicios ocultos"))
    assert (entry.kind, entry.label, entry.heading) == (
        "subsection",
        "§ 2.º",
        "Del saneamiento por los vicios ocultos",
    )


def test_subsection_with_only_a_title_is_all_label() -> None:
    (entry,) = build(subsection_h("ss", "Del pago por cesión de bienes"))
    assert (entry.label, entry.heading) == ("Del pago por cesión de bienes", "")


def test_spelled_ordinal_only_has_no_heading() -> None:
    (entry,) = build(section_h("s", "Sección primera."))
    assert (entry.label, entry.heading) == ("Sección primera", "")


@pytest.mark.parametrize(
    "text", ["Disposición general", "Disposición preliminar", "Sección Comunes"]
)
def test_section_without_a_recognised_ordinal_is_all_label(text: str) -> None:
    (entry,) = build(section_h("s", text))
    assert (entry.label, entry.heading) == (text, "")


# --- C3: capitulo paragraphs ---------------------------------------------------------------------


def test_capitulo_unico_paragraph_is_a_real_chapter() -> None:
    entries = build(title_h("t"), heading("cu", para("capitulo", "CAPÍTULO ÚNICO")), article("a1"))
    assert kinds(entries) == ["title", "chapter", "article"]
    assert levels(entries) == [1, 2, 3]
    assert (entries[1].label, entries[1].heading) == ("CAPÍTULO ÚNICO", "")


@pytest.mark.parametrize(
    ("text", "label", "rest"),
    [
        ("Capítulo único. Disposiciones", "Capítulo único", "Disposiciones"),
        ("CAPITULO II Algo", "CAPITULO II", "Algo"),
        ("capítulo primero", "capítulo primero", ""),
    ],
)
def test_capitulo_paragraph_starting_with_capitulo_is_split_like_a_chapter(
    text: str, label: str, rest: str
) -> None:
    (entry,) = build(heading("c", para("capitulo", text)))
    assert (entry.kind, entry.label, entry.heading) == ("chapter", label, rest)


@pytest.mark.parametrize(
    "text", ["DISPOSICIONES ADICIONALES", "DISPOSICIÓN FINAL", "DISPOSICIONES TRANSITORIAS"]
)
def test_capitulo_paragraph_not_starting_with_capitulo_is_a_group(text: str) -> None:
    (entry,) = build(group_h("g", text))
    assert (entry.kind, entry.level, entry.label, entry.heading) == ("group", 1, text, "")


def test_single_capitulo_chapter_is_a_sibling_of_a_num_tit_chapter() -> None:
    entries = build(
        title_h("t"), chapter_h("c1"), heading("c2", para("capitulo", "CAPÍTULO ÚNICO"))
    )
    assert levels(entries) == [1, 2, 2]


def test_single_capitulo_chapter_does_not_reset_the_nesting() -> None:
    entries = build(title_h("t"), heading("c", para("capitulo", "CAPÍTULO ÚNICO")), article("a1"))
    assert levels(entries) == [1, 2, 3]


# --- C4: precedence inside a heading block ----------------------------------------------------


def test_num_tit_pair_wins_over_a_single_paragraph() -> None:
    h = heading(
        "h",
        para("titulo_num", "TÍTULO III"),
        para("titulo_tit", "De las sucesiones"),
        para("seccion", "Disposiciones generales"),
    )
    (entry,) = build(h)
    assert (entry.kind, entry.label, entry.heading) == ("title", "TÍTULO III", "De las sucesiones")


def test_book_pair_wins_over_title_pair_and_title_over_chapter() -> None:
    both = heading("h", para("titulo_num", "TÍTULO I"), para("libro_num", "LIBRO I"))
    assert kinds(build(both)) == ["book"]
    both = heading("h", para("capitulo_num", "CAPÍTULO I"), para("titulo_num", "TÍTULO I"))
    assert kinds(build(both)) == ["title"]


def test_first_single_paragraph_decides() -> None:
    h = heading("h", para("seccion", "Sección 1.ª Uno"), para("anexo", "ANEXO"))
    assert kinds(build(h)) == ["section"]
    h = heading("h", para("anexo", "ANEXO"), para("seccion", "Sección 1.ª Uno"))
    assert kinds(build(h)) == ["annex"]


def test_body_text_and_quoted_notes_in_a_heading_block_are_ignored() -> None:
    h = heading(
        "h",
        para("capitulo", "DISPOSICIONES TRANSITORIAS"),
        para("parrafo_2", "Las variaciones introducidas por este Código."),
        Paragraph(kind="nota_pie", text="Nota.", quoted=True),
    )
    (entry,) = build(h)
    assert (entry.kind, entry.label, entry.heading) == ("group", "DISPOSICIONES TRANSITORIAS", "")


# --- C5: levels of articles, provisions and other entries -------------------------------------


def test_disposicion_directly_after_a_non_group_heading_is_level_one() -> None:
    entries = build(title_h("t"), chapter_h("c"), disposition("da"))
    assert levels(entries) == [1, 2, 1]


def test_disposicion_under_a_group_is_one_level_below_the_group() -> None:
    entries = build(article("a1"), group_h("g"), disposition("da"))
    assert levels(entries) == [1, 1, 2]


def test_disposicion_under_an_annex_is_level_one() -> None:
    assert levels(build(annex_h("an"), disposition("da"))) == [1, 1]


def test_provision_not_starting_with_disposicion_is_nested_under_the_nearest_heading() -> None:
    entries = build(title_h("t"), chapter_h("c"), article("p", "Primera."))
    assert (kinds(entries)[-1], levels(entries)) == ("provision", [1, 2, 3])


def test_provision_from_the_block_title_is_nested_under_the_nearest_heading() -> None:
    b = block("s", "precepto", para("seccion", "Sección primera. X"), title="Sección primera")
    entries = build(title_h("t"), b)
    assert (kinds(entries)[-1], levels(entries)) == ("provision", [1, 2])


def test_unrecognised_heading_block_is_kind_other_placed_like_an_article() -> None:
    entries = build(title_h("t"), heading("h", para("parrafo", "Algo."), title="Rara"))
    assert kinds(entries) == ["title", "other"]
    assert levels(entries) == [1, 2]


def test_kind_other_never_acts_as_a_parent() -> None:
    entries = build(
        title_h("t"), heading("h", para("parrafo", "Algo."), title="Rara"), article("a1")
    )
    assert levels(entries) == [1, 2, 2]


def test_kind_other_does_not_change_the_nesting_of_later_headings() -> None:
    other = heading("h", para("parrafo", "Algo."), title="Rara")
    entries = build(title_h("t"), chapter_h("c1"), other, chapter_h("c2", "CAPÍTULO II"))
    assert levels(entries) == [1, 2, 3, 2]


def test_unknown_tipo_is_kind_other_placed_like_an_article_and_not_a_parent() -> None:
    odd = block("x", "tipo_nuevo", para("parrafo", "Texto."), title="Nuevo")
    entries = build(title_h("t"), odd, article("a1"))
    assert kinds(entries) == ["title", "other", "article"]
    assert levels(entries) == [1, 2, 2]


# --- the version a heading is read from -----------------------------------------------------


def changing_title() -> Block:
    old = version(
        para("titulo_num", "TÍTULO PRELIMINAR"),
        para("titulo_tit", "De las Leyes, de sus efectos"),
        effective=date(1889, 8, 16),
    )
    new = version(
        para("titulo_num", "TÍTULO PRELIMINAR"),
        para("titulo_tit", "De las normas jurídicas"),
        effective=date(1974, 7, 29),
    )
    return heading("tp", versions=(old, new))


def test_heading_text_is_the_latest_version_in_effect_today() -> None:
    (entry,) = build(changing_title())
    assert entry.heading == "De las normas jurídicas"


def test_heading_text_as_of_a_past_date_comes_from_the_older_version() -> None:
    (entry,) = build(changing_title(), on=date(1900, 1, 1))
    assert entry.heading == "De las Leyes, de sus efectos"
    assert entry.state == "in_force"


def test_heading_version_switches_on_its_effective_date() -> None:
    assert (
        build(changing_title(), on=date(1974, 7, 28))[0].heading == "De las Leyes, de sus efectos"
    )
    assert build(changing_title(), on=date(1974, 7, 29))[0].heading == "De las normas jurídicas"


def test_heading_kind_and_label_come_from_the_selected_version_too() -> None:
    old = version(
        para("capitulo_num", "CAPÍTULO PRIMERO"),
        para("capitulo_tit", "Disposiciones generales"),
        effective=date(1889, 1, 1),
    )
    new = version(
        para("capitulo_num", "CAPÍTULO I"),
        para("capitulo_tit", "De la promesa de matrimonio"),
        effective=date(1981, 8, 9),
    )
    block_ = heading("c", versions=(old, new))
    assert label_heading(build(block_, on=date(1900, 1, 1))) == [
        ("CAPÍTULO PRIMERO", "Disposiciones generales")
    ]
    assert label_heading(build(block_)) == [("CAPÍTULO I", "De la promesa de matrimonio")]


def test_article_text_is_taken_from_the_version_in_effect_on_the_date() -> None:
    old = version(para("articulo", "Artículo 307."), effective=date(1889, 8, 16))
    new = version(para("articulo", "Artículo 307. (Sin contenido)"), effective=date(1983, 11, 15))
    block_ = article("a307", versions=(old, new))
    assert label_heading(build(block_, on=date(1900, 1, 1))) == [("Artículo 307", "")]
    assert label_heading(build(block_)) == [("Artículo 307", "(Sin contenido)")]


# --- articles and provisions -------------------------------------------------------------------


def test_article_with_heading() -> None:
    (entry,) = build(
        article("a1", "Artículo 27. Garantía del derecho a la protección de la salud.")
    )
    assert (entry.kind, entry.label, entry.heading) == (
        "article",
        "Artículo 27",
        "Garantía del derecho a la protección de la salud",
    )


def test_article_without_heading_text_and_with_trailing_period() -> None:
    (entry,) = build(article("a1", "Artículo 1."))
    assert (entry.kind, entry.label, entry.heading) == ("article", "Artículo 1", "")


def test_article_without_heading_text_and_without_period() -> None:
    (entry,) = build(article("a27", "Artículo 27"))
    assert (entry.kind, entry.label, entry.heading) == ("article", "Artículo 27", "")


def test_article_designation_with_a_suffix() -> None:
    (entry,) = build(article("a46", "Artículo 46 bis. Medidas cautelares."))
    assert (entry.label, entry.heading) == ("Artículo 46 bis", "Medidas cautelares")


def test_article_is_split_at_the_first_period_not_the_last() -> None:
    (entry,) = build(article("a9", "Artículo 9. Régimen del art. 3 de la ley."))
    assert (entry.label, entry.heading) == ("Artículo 9", "Régimen del art. 3 de la ley")


def test_only_the_final_period_of_the_heading_is_removed() -> None:
    (entry,) = build(article("a9", "Artículo 9. Uno. Dos. Tres."))
    assert (entry.label, entry.heading) == ("Artículo 9", "Uno. Dos. Tres")


def test_repealed_marker_heading_is_kept_as_the_heading() -> None:
    (entry,) = build(article("a1231", "Artículo 1231. (Derogado)"))
    assert (entry.label, entry.heading) == ("Artículo 1231", "(Derogado)")


def test_article_kind_test_is_case_insensitive() -> None:
    (entry,) = build(article("a5", "ARTÍCULO 5. DEL OBJETO."))
    assert (entry.kind, entry.label, entry.heading) == ("article", "ARTÍCULO 5", "DEL OBJETO")


def test_the_first_articulo_paragraph_is_used_wherever_it_sits() -> None:
    b = block(
        "a1",
        "precepto",
        para("parrafo", "Texto previo."),
        para("articulo", "Artículo 1. Uno."),
        para("articulo", "Artículo 2. Dos."),
    )
    (entry,) = build(b)
    assert (entry.label, entry.heading) == ("Artículo 1", "Uno")


def test_disposition_is_a_provision_with_label_and_heading() -> None:
    (entry,) = build(disposition("df", "Disposición final cuarta. Entrada en vigor."))
    assert (entry.kind, entry.label, entry.heading) == (
        "provision",
        "Disposición final cuarta",
        "Entrada en vigor",
    )


def test_bare_ordinal_provision_has_no_heading_and_no_trailing_period() -> None:
    (entry,) = build(article("p", "Primera."))
    assert (entry.kind, entry.label, entry.heading) == ("provision", "Primera", "")


def test_precepto_without_articulo_paragraph_uses_the_block_title() -> None:
    b = block(
        "s", "precepto", para("seccion", "Sección primera. De las clases"), title="Sección primera"
    )
    (entry,) = build(b)
    assert (entry.kind, entry.label, entry.heading) == ("provision", "Sección primera", "")


@pytest.mark.parametrize("title", ["Art. 1873 bis", "ART. 5", "Artículo 7"])
def test_title_fallback_starting_with_art_is_an_article(title: str) -> None:
    b = block("x", "precepto", para("parrafo", "Texto."), title=title)
    (entry,) = build(b)
    assert (entry.kind, entry.label) == ("article", title)


# --- preamble ------------------------------------------------------------------------------------


def test_preambulo_without_title_is_labelled_preambulo() -> None:
    (entry,) = build(block("preambulo", "preambulo", para("parrafo_2", "Texto.")))
    assert (entry.kind, entry.level, entry.label, entry.heading) == ("preamble", 1, "Preámbulo", "")


def test_parte_dispositiva_is_a_preamble_too() -> None:
    (entry,) = build(block("preambulo", "parte_dispositiva", para("parrafo", "Vengo en decretar.")))
    assert (entry.kind, entry.level, entry.heading) == ("preamble", 1, "")


@pytest.mark.parametrize("title", ["[Preámbulo]", "[preambulo]", "Algo distinto", ""])
def test_preambulo_label_is_fixed_and_ignores_the_block_title(title: str) -> None:
    (entry,) = build(block("pr", "preambulo", para("parrafo", "Texto."), title=title))
    assert entry.label == "Preámbulo"


@pytest.mark.parametrize("title", ["[Preámbulo]", "Parte", ""])
def test_parte_dispositiva_label_is_fixed_and_ignores_the_block_title(title: str) -> None:
    b = block("pd", "parte_dispositiva", para("parrafo", "Vengo en decretar."), title=title)
    (entry,) = build(b)
    assert (entry.kind, entry.label, entry.heading) == ("preamble", "Parte dispositiva", "")


def test_preamble_is_moved_to_the_front_and_nothing_else_is_reordered() -> None:
    pre = block("pr", "preambulo", para("parrafo", "Texto."))
    entries = build(
        title_h("t"), article("a1"), chapter_h("c"), article("a2", "Artículo 2. B."), pre
    )
    assert ids(entries) == ["pr", "t", "a1", "c", "a2"]


def test_several_preambles_keep_their_relative_order() -> None:
    p1 = block("p1", "parte_dispositiva", para("parrafo", "Uno."))
    p2 = block("p2", "preambulo", para("parrafo", "Dos."))
    entries = build(p1, article("a1"), p2)
    assert ids(entries) == ["p1", "p2", "a1"]


def test_preamble_is_always_level_one() -> None:
    pre = block("pr", "preambulo", para("parrafo", "Texto."))
    entries = build(title_h("t"), chapter_h("c"), article("a1"), pre)
    assert entries[0].kind == "preamble"
    assert entries[0].level == 1


# --- levels ----------------------------------------------------------------------------------


def test_title_chapter_article_are_levels_1_2_3() -> None:
    assert levels(build(title_h("t"), chapter_h("c"), article("a1"))) == [1, 2, 3]


def test_full_hierarchy_book_to_subsection() -> None:
    entries = build(
        book_h("b"),
        title_h("t"),
        chapter_h("c"),
        section_h("s"),
        subsection_h("ss"),
        article("a1"),
    )
    assert kinds(entries) == ["book", "title", "chapter", "section", "subsection", "article"]
    assert levels(entries) == [1, 2, 3, 4, 5, 6]


def test_levels_are_compacted_when_a_law_only_has_chapters() -> None:
    entries = build(chapter_h("c1"), article("a1"), chapter_h("c2", "CAPÍTULO II"), article("a2"))
    assert levels(entries) == [1, 2, 1, 2]


def test_levels_are_compacted_when_a_rank_is_skipped() -> None:
    entries = build(title_h("t"), section_h("s"), article("a1"))
    assert levels(entries) == [1, 2, 3]


def test_levels_are_compacted_for_sections_and_subsections_only() -> None:
    entries = build(section_h("s"), subsection_h("ss"), article("a1"))
    assert levels(entries) == [1, 2, 3]


def test_book_directly_above_a_section_is_level_one_and_two() -> None:
    assert levels(build(book_h("b"), section_h("s"))) == [1, 2]


def test_sibling_headings_share_a_level() -> None:
    entries = build(title_h("t"), chapter_h("c1"), chapter_h("c2", "CAPÍTULO II"))
    assert levels(entries) == [1, 2, 2]


def test_a_shallower_heading_closes_the_deeper_ones() -> None:
    entries = build(
        title_h("t1"),
        chapter_h("c1"),
        section_h("s1"),
        subsection_h("ss1"),
        section_h("s2", "Sección 2.ª Otra"),
        chapter_h("c2", "CAPÍTULO II"),
        title_h("t2", "TÍTULO II"),
    )
    assert levels(entries) == [1, 2, 3, 4, 3, 2, 1]


def test_a_section_after_a_subsection_goes_back_to_the_section_level() -> None:
    entries = build(chapter_h("c"), section_h("s1"), subsection_h("ss1"), section_h("s2"))
    assert levels(entries) == [1, 2, 3, 2]


def test_nesting_uses_rank_so_a_chapter_does_not_nest_under_a_section() -> None:
    entries = build(title_h("t"), chapter_h("c1"), section_h("s"), chapter_h("c2", "CAPÍTULO II"))
    assert levels(entries) == [1, 2, 3, 2]


def test_article_level_is_its_heading_level_plus_one() -> None:
    entries = build(title_h("t"), chapter_h("c"), section_h("s"), article("a1"))
    assert levels(entries)[-1] == levels(entries)[-2] + 1 == 4


def test_article_directly_under_a_title_is_level_two() -> None:
    assert levels(build(title_h("t"), article("a1"))) == [1, 2]


def test_article_after_a_deeper_heading_is_nested_under_that_one() -> None:
    entries = build(
        title_h("t"), chapter_h("c"), article("a1"), section_h("s"), article("a2", "Artículo 2. B.")
    )
    assert levels(entries) == [1, 2, 3, 3, 4]


def test_articles_before_any_heading_are_level_one() -> None:
    entries = build(article("a1"), article("a2", "Artículo 2. B."), title_h("t"), article("a3"))
    assert levels(entries) == [1, 1, 1, 2]


def test_group_heading_is_level_one_and_resets_the_nesting() -> None:
    entries = build(
        title_h("t"), chapter_h("c1"), article("a1"), group_h("g"), chapter_h("c2", "CAPÍTULO II")
    )
    assert kinds(entries) == ["title", "chapter", "article", "group", "chapter"]
    assert levels(entries) == [1, 2, 3, 1, 1]


def test_group_resets_even_when_a_title_follows() -> None:
    entries = build(title_h("t1"), group_h("g"), title_h("t2", "TÍTULO II"), chapter_h("c"))
    assert levels(entries) == [1, 1, 1, 2]


def test_annex_heading_is_level_one_and_resets_the_nesting() -> None:
    entries = build(
        title_h("t"), chapter_h("c1"), annex_h("an"), chapter_h("c2", "CAPÍTULO II"), article("a1")
    )
    assert levels(entries) == [1, 2, 1, 1, 2]


def test_group_is_level_one_after_deep_nesting() -> None:
    entries = build(book_h("b"), title_h("t"), chapter_h("c"), section_h("s"), group_h("g"))
    assert levels(entries)[-1] == 1


def test_articles_and_provisions_under_a_group_are_one_level_below_it() -> None:
    entries = build(
        group_h("dt", "DISPOSICIONES TRANSITORIAS"),
        article("p1", "Primera."),
        article("p2", "Segunda."),
        group_h("da", "DISPOSICIONES ADICIONALES"),
        article("p3", "Primera."),
    )
    assert kinds(entries) == ["group", "provision", "provision", "group", "provision"]
    assert levels(entries) == [1, 2, 2, 1, 2]


def test_article_under_an_annex_is_level_two() -> None:
    assert levels(build(annex_h("an"), article("a1"))) == [1, 2]


def test_provisions_after_articles_with_no_group_heading_are_level_one() -> None:
    # The Asturias law and Ley 40/2015 have no heading block for their disposiciones.
    entries = build(
        title_h("t"),
        chapter_h("c"),
        article("a1"),
        disposition("da", "Disposición adicional primera. Uno."),
        disposition("dt", "Disposición transitoria única. Dos."),
        disposition("df", "Disposición final primera. Tres."),
    )
    assert kinds(entries) == ["title", "chapter", "article", "provision", "provision", "provision"]
    assert levels(entries) == [1, 2, 3, 1, 1, 1]


def test_provision_under_a_deeper_heading_is_still_level_one_after_articles() -> None:
    entries = build(
        title_h("t"),
        chapter_h("c"),
        section_h("s"),
        article("a1"),
        disposition("da"),
    )
    assert levels(entries) == [1, 2, 3, 4, 1]


def test_provision_with_no_article_before_it_and_no_heading_is_level_one() -> None:
    assert levels(build(disposition("da"))) == [1]


# --- state ---------------------------------------------------------------------------------------


def test_state_in_force() -> None:
    assert build(article("a1"))[0].state == "in_force"


def test_state_repealed_when_the_version_in_effect_is_a_repeal_marker() -> None:
    repealed = block(
        "a1",
        "precepto",
        para("articulo", "Artículo 1. Objeto."),
        para("parrafo", "(Derogado)."),
    )
    (entry,) = build(repealed)
    assert entry.state == "repealed"
    assert (entry.label, entry.heading) == ("Artículo 1", "Objeto")


def test_state_follows_the_requested_date_across_versions() -> None:
    live = version(para("articulo", "Artículo 1. Objeto."), para("parrafo", "Texto."))
    gone = version(
        para("articulo", "Artículo 1. Objeto."),
        para("parrafo", "(Derogado)."),
        effective=date(2020, 1, 1),
    )
    block_ = article("a1", versions=(live, gone))
    assert build(block_, on=date(2010, 1, 1))[0].state == "in_force"
    assert build(block_, on=date(2026, 1, 1))[0].state == "repealed"


def test_state_of_a_heading_entry() -> None:
    assert build(title_h("t"))[0].state == "in_force"


def test_state_is_per_entry() -> None:
    future = version(para("articulo", "Artículo 2. B."), effective=date(2027, 1, 1)).model_copy(
        update={"published_on": date(2026, 10, 1)}
    )
    entries = build(article("a1"), article("a2", versions=(future,)))
    assert [e.state for e in entries] == ["in_force", "not_yet_in_force"]


# --- end to end: Asturias (new law, not yet in force) ------------------------------------------


def parsed(load_fixture: Loader, name: str) -> list[Block]:
    return parse_full_text(load_fixture(name))


def test_asturias_structure(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, ASTURIAS), ON)
    assert [(e.block_id, e.kind, e.level, e.label) for e in entries] == [
        ("pr", "preamble", 1, "Preámbulo"),
        ("ti", "title", 1, "TÍTULO I"),
        ("ci", "chapter", 2, "CAPÍTULO I"),
        ("a1", "article", 3, "Artículo 1"),
        ("a2", "article", 3, "Artículo 2"),
        ("ci-2", "chapter", 2, "CAPÍTULO II"),
        ("a4", "article", 3, "Artículo 4"),
        ("ti-2", "title", 1, "TÍTULO II"),
        ("ci-3", "chapter", 2, "CAPÍTULO I"),
        ("a9", "article", 3, "Artículo 9"),
        ("a1-2", "article", 3, "Artículo 10"),
        ("ci-11", "chapter", 2, "CAPÍTULO IV"),
        ("s1", "section", 3, "Sección 1.ª"),
        ("a6-7", "article", 4, "Artículo 65"),
        ("s2", "section", 3, "Sección 2.ª"),
        ("a6-11", "article", 4, "Artículo 69"),
        ("cv", "chapter", 2, "CAPÍTULO V"),
        ("s1-2", "section", 3, "Sección 1.ª"),
        ("a8-3", "article", 4, "Artículo 81"),
        ("da", "provision", 1, "Disposición adicional primera"),
        ("da-2", "provision", 1, "Disposición adicional segunda"),
        ("dt", "provision", 1, "Disposición transitoria única"),
        ("dd", "provision", 1, "Disposición derogatoria única"),
        ("df", "provision", 1, "Disposición final primera"),
        ("df-4", "provision", 1, "Disposición final cuarta"),
    ]


def test_asturias_headings(load_fixture: Loader) -> None:
    by_id = {e.block_id: e for e in build_structure(parsed(load_fixture, ASTURIAS), ON)}
    assert by_id["ti"].heading == "Disposiciones generales"
    assert by_id["ci-11"].heading == "De la guarda y del desamparo"
    assert by_id["s1"].heading == "De la guarda"
    assert by_id["s2"].heading == "Del desamparo"
    assert by_id["a1"].heading == "Objeto"
    assert by_id["a6-7"].heading == "Guarda provisional"
    assert by_id["df-4"].heading == "Entrada en vigor"
    assert by_id["da"].heading.startswith("Día de los Derechos de la Infancia")


def test_asturias_firma_is_skipped(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, ASTURIAS), ON)
    assert "fi" not in ids(entries)
    assert len(entries) == 25


def test_asturias_is_not_yet_in_force_before_the_effective_date(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, ASTURIAS), ON)
    assert {e.state for e in entries} == {"not_yet_in_force"}


def test_asturias_is_in_force_from_the_effective_date(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, ASTURIAS), date(2026, 11, 23))
    assert {e.state for e in entries} == {"in_force"}
    assert len(entries) == 25


def test_asturias_one_day_before_the_effective_date_is_still_not_in_force(
    load_fixture: Loader,
) -> None:
    entries = build_structure(parsed(load_fixture, ASTURIAS), date(2026, 11, 22))
    assert {e.state for e in entries} == {"not_yet_in_force"}


def test_asturias_preamble_label_ignores_the_bracketed_title(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, ASTURIAS), ON)
    assert (entries[0].kind, entries[0].label, entries[0].heading) == ("preamble", "Preámbulo", "")


# --- end to end: Ley 40/2015 ---------------------------------------------------------------------


def test_ley40_structure(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, LEY40), ON)
    assert [(e.block_id, e.kind, e.level) for e in entries] == [
        ("tpreliminar", "title", 1),
        ("ci", "chapter", 2),
        ("a1", "article", 3),
        ("a2", "article", 3),
        ("cii", "chapter", 2),
        ("s1", "section", 3),
        ("a5", "article", 4),
        ("s3", "section", 3),
        ("ss1", "subsection", 4),
        ("a15", "article", 5),
        ("ss2", "subsection", 4),
        ("a19", "article", 5),
        ("s4", "section", 3),
        ("a23", "article", 4),
        ("ciii", "chapter", 2),
        ("a48", "article", 3),
        ("a50", "article", 3),
        ("ti", "title", 1),
        ("ci-2", "chapter", 2),
        ("a54", "article", 3),
        ("dacuaa", "provision", 1),
        ("daseptima", "provision", 1),
    ]


def test_ley40_skips_the_initial_note_and_the_signature(load_fixture: Loader) -> None:
    entries = build_structure(parsed(load_fixture, LEY40), ON)
    assert "co" not in ids(entries)
    assert "firma" not in ids(entries)
    assert {e.state for e in entries} == {"in_force"}


def test_ley40_labels_collapse_the_non_breaking_spaces(load_fixture: Loader) -> None:
    by_id = {e.block_id: e for e in build_structure(parsed(load_fixture, LEY40), ON)}
    assert (by_id["a1"].label, by_id["a1"].heading) == ("Artículo 1", "Objeto")
    assert (by_id["s1"].label, by_id["s1"].heading) == (
        "Sección 1.ª",
        "De los órganos administrativos",
    )
    assert (by_id["ss1"].label, by_id["ss1"].heading) == ("Subsección 1.ª", "Funcionamiento")
    assert by_id["tpreliminar"].label == "TÍTULO PRELIMINAR"
    assert by_id["dacuaa"].label == "Disposición adicional cuarta"
    assert by_id["dacuaa"].heading.startswith("Adaptación")


# --- end to end: Código Civil (expiry, headings across versions, groups) -----------------------


def civil(load_fixture: Loader, on: date) -> dict[str, StructureEntry]:
    return {e.block_id: e for e in build_structure(parsed(load_fixture, CODIGO_CIVIL), on)}


def test_codigo_civil_drops_expired_blocks_today(load_fixture: Loader) -> None:
    got = civil(load_fixture, ON)
    for expired in ("sprimera", "cii-4", "art307", "disposicionpreliminar", "art.1873bis"):
        assert expired not in got
    assert {"a1", "art17", "cprimero-3", "tpreliminar"} <= set(got)


def test_codigo_civil_sentinel_block_is_skipped_at_any_date(load_fixture: Loader) -> None:
    assert "art.1873bis" not in civil(load_fixture, date(1950, 1, 1))
    assert "art.1873bis" not in civil(load_fixture, date(1900, 1, 1))


def test_codigo_civil_block_with_a_real_expiry_is_present_before_it(load_fixture: Loader) -> None:
    assert "art307" in civil(load_fixture, date(1983, 11, 14))
    assert "art307" not in civil(load_fixture, date(1983, 11, 15))
    assert "cii-4" in civil(load_fixture, date(1981, 7, 19))
    assert "cii-4" not in civil(load_fixture, date(1981, 7, 20))


def test_codigo_civil_parte_dispositiva_comes_first_and_the_signature_is_skipped(
    load_fixture: Loader,
) -> None:
    entries = build_structure(parsed(load_fixture, CODIGO_CIVIL), ON)
    assert (entries[0].block_id, entries[0].kind, entries[0].label) == (
        "preambulo",
        "preamble",
        "Parte dispositiva",
    )
    assert "fi" not in ids(entries)


def test_codigo_civil_annex_and_hierarchy(load_fixture: Loader) -> None:
    got = civil(load_fixture, ON)
    annex = got["codigocivil"]
    assert (annex.kind, annex.level, annex.label, annex.heading) == ("annex", 1, "CÓDIGO CIVIL", "")
    assert (got["lprimero"].kind, got["lprimero"].level) == ("book", 1)
    assert (got["tprimero"].kind, got["tprimero"].level) == ("title", 2)
    assert (got["art17"].kind, got["art17"].level) == ("article", 3)
    assert (got["tprimero"].label, got["tprimero"].heading) == (
        "TÍTULO I",
        "De los españoles y extranjeros",
    )
    assert got["art17"].label == "Artículo 17"


def test_codigo_civil_titles_before_any_book_use_the_compacted_levels(
    load_fixture: Loader,
) -> None:
    got = civil(load_fixture, ON)
    assert (got["tpreliminar"].level, got["ci"].level, got["a1"].level) == (1, 2, 3)


def test_codigo_civil_group_headings_and_bare_ordinal_provisions(load_fixture: Loader) -> None:
    got = civil(load_fixture, ON)
    assert (got["df"].kind, got["df"].level, got["df"].label) == ("group", 1, "DISPOSICIÓN FINAL")
    assert (got["art1976"].kind, got["art1976"].level) == ("article", 2)
    assert (got["dt"].kind, got["dt"].level) == ("group", 1)
    assert (got["primera"].kind, got["primera"].level, got["primera"].label) == (
        "provision",
        2,
        "Primera",
    )
    assert got["primera"].heading == ""
    assert (got["da"].kind, got["da"].level) == ("group", 1)
    assert (got["primera-2"].kind, got["primera-2"].level) == ("provision", 2)


def test_codigo_civil_group_with_body_paragraphs_is_still_a_group(load_fixture: Loader) -> None:
    # ``dt`` holds a ``capitulo`` paragraph followed by body text (C4: other paragraphs ignored).
    entry = civil(load_fixture, ON)["dt"]
    assert (entry.kind, entry.level, entry.label, entry.heading) == (
        "group",
        1,
        "DISPOSICIONES TRANSITORIAS",
        "",
    )


def test_codigo_civil_capitulo_unico_is_a_real_chapter_inside_the_title(
    load_fixture: Loader,
) -> None:
    got = civil(load_fixture, ON)
    assert (got["tviii-2"].kind, got["tviii-2"].level) == ("title", 2)
    cunico = got["cunico"]
    assert (cunico.kind, cunico.level, cunico.label, cunico.heading) == (
        "chapter",
        3,
        "CAPÍTULO ÚNICO",
        "",
    )
    assert (got["art605"].kind, got["art605"].level) == ("article", 4)


def test_codigo_civil_title_with_a_seccion_paragraph_is_a_title(load_fixture: Loader) -> None:
    # ``tiii-3``: titulo_num + titulo_tit + a ``seccion`` paragraph; the pair wins (C4).
    entry = civil(load_fixture, ON)["tiii-3"]
    assert (entry.kind, entry.label, entry.heading) == ("title", "TÍTULO III", "De las sucesiones")


def test_codigo_civil_skips_blocks_not_yet_published_in_1900(load_fixture: Loader) -> None:
    # C1: ``ci`` has only a 1974 version, so on 1900-01-01 it did not exist.
    assert "ci" not in civil(load_fixture, date(1900, 1, 1))
    assert "ci" in civil(load_fixture, ON)


def test_codigo_civil_article_without_period_after_the_number(load_fixture: Loader) -> None:
    got = civil(load_fixture, ON)
    assert (got["art27"].label, got["art27"].heading) == ("Artículo 27", "")


def test_codigo_civil_heading_text_follows_the_date(load_fixture: Loader) -> None:
    today = civil(load_fixture, ON)
    assert today["tpreliminar"].heading == "De las normas jurídicas, su aplicación y eficacia"
    ancient = civil(load_fixture, date(1900, 1, 1))
    assert ancient["tpreliminar"].heading == (
        "De las Leyes, de sus efectos y de las reglas generales para su aplicación"
    )
    assert today["tpreliminar"].label == ancient["tpreliminar"].label == "TÍTULO PRELIMINAR"


def test_codigo_civil_chapter_reusing_an_id_changes_label_and_heading(
    load_fixture: Loader,
) -> None:
    today = civil(load_fixture, ON)["cprimero-3"]
    ancient = civil(load_fixture, date(1900, 1, 1))["cprimero-3"]
    assert (ancient.label, ancient.heading) == ("CAPÍTULO PRIMERO", "Disposiciones generales")
    assert (today.label, today.heading) == ("CAPÍTULO I", "De la promesa de matrimonio")


def test_codigo_civil_book_heading_changes_on_the_amending_law_date(
    load_fixture: Loader,
) -> None:
    before = civil(load_fixture, date(2022, 1, 4))["lsegundo"]
    after = civil(load_fixture, date(2022, 1, 5))["lsegundo"]
    assert before.heading == "De los bienes, de la propiedad y de sus modificaciones"
    assert after.heading.startswith("De los animales, de los bienes")


def test_codigo_civil_section_heading_without_ordinal(load_fixture: Loader) -> None:
    entry = civil(load_fixture, ON)["disposicionpreliminar-2"]
    assert (entry.kind, entry.label, entry.heading) == ("section", "Disposición preliminar", "")


# --- parse_publication_note ----------------------------------------------------------------------

REAL_NOTES = [
    ("Publicada en el BOPA núm. 206, de 23 de octubre de 2026.", "BOPA", "206", date(2026, 10, 23)),
    ("Publicada en el BON núm. 35, de 20 de marzo de 1991.", "BON", "35", date(1991, 3, 20)),
    ("Publicada en el BOPV núm 129, de 10 de julio de 1998.", "BOPV", "129", date(1998, 7, 10)),
    ("Publicada en el DOG núm. 149, de 7 de agosto de 2019.", "DOG", "149", date(2019, 8, 7)),
    ("Publicada en el BOCM núm. 94, de 22 de abril de 2019.", "BOCM", "94", date(2019, 4, 22)),
    (
        "Publicada en el BOJA núm. 227, de 25 de noviembre de 2019.",
        "BOJA",
        "227",
        date(2019, 11, 25),
    ),
    ("Publicada en el DOE núm. 199, de 15 de octubre de 2019.", "DOE", "199", date(2019, 10, 15)),
    ("Publicada en el BOC núm. 94, de 17 de mayo de 2019", "BOC", "94", date(2019, 5, 17)),
]


@pytest.mark.parametrize(("note", "gazette", "number", "when"), REAL_NOTES)
def test_real_publication_notes(note: str, gazette: str, number: str, when: date) -> None:
    assert parse_publication_note([note]) == GazettePublication(gazette, number, when)


def test_result_is_a_frozen_dataclass_with_the_spec_fields() -> None:
    result = parse_publication_note(["Publicada en el BOPA núm. 206, de 23 de octubre de 2026."])
    assert result is not None
    assert dataclasses.is_dataclass(result)
    assert [f.name for f in dataclasses.fields(result)] == ["gazette", "number", "date"]
    assert isinstance(result.date, date)
    assert result.number == "206"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.gazette = "X"  # type: ignore[misc]


def test_list_of_notes_holding_unrelated_notes_real_basque_example() -> None:
    notes = [
        "Vigencia hasta el 31 de diciembre de 2022. Prorrogable.",
        "Esta disposición ha dejado de estar vigente",
        "Publicada en el BOPV núm. 129, de 9 de julio de 2019.",
    ]
    assert parse_publication_note(notes) == GazettePublication("BOPV", "129", date(2019, 7, 9))


def test_matching_note_can_come_first_or_in_the_middle() -> None:
    match = "Publicada en el DOG núm. 149, de 7 de agosto de 2019."
    assert parse_publication_note([match, "Otra nota."]) is not None
    assert parse_publication_note(["A.", match, "B."]) is not None


def test_the_first_matching_note_wins() -> None:
    notes = [
        "Nota previa sin fecha.",
        "Publicada en el DOG núm. 149, de 7 de agosto de 2019.",
        "Publicada en el BOC núm. 94, de 17 de mayo de 2019.",
    ]
    assert parse_publication_note(notes) == GazettePublication("DOG", "149", date(2019, 8, 7))


@pytest.mark.parametrize("notes", [[], [""], ["Vigencia hasta el 31 de diciembre de 2022."]])
def test_no_match_gives_none(notes: list[str]) -> None:
    assert parse_publication_note(notes) is None


@pytest.mark.parametrize(
    "note",
    [
        "Corregida en el BOPA núm. 206, de 23 de octubre de 2026.",
        "Publicada en el BOPA núm. 206, de 23 de octubre.",
        "Publicada en el BOPA núm. 206.",
        "Publicada en el BOPA núm. 206, de 23 de octubre de 26.",
        "Publicada en el BOPA núm. 206, de 23 de octubro de 2026.",
    ],
)
def test_near_misses_give_none(note: str) -> None:
    assert parse_publication_note([note]) is None


@pytest.mark.parametrize(
    "note",
    [
        "Publicada en el BOPA núm. 206, de 31 de febrero de 2026.",
        "Publicada en el BOPA núm. 206, de 29 de febrero de 2026.",
        "Publicada en el BOPA núm. 206, de 32 de enero de 2026.",
        "Publicada en el BOPA núm. 206, de 0 de enero de 2026.",
        "Publicada en el BOPA núm. 206, de 31 de abril de 2026.",
    ],
)
def test_impossible_date_gives_none(note: str) -> None:
    assert parse_publication_note([note]) is None


def test_leap_day_is_a_valid_date() -> None:
    result = parse_publication_note(["Publicada en el BOPA núm. 10, de 29 de febrero de 2024."])
    assert result is not None
    assert result.date == date(2024, 2, 29)


@pytest.mark.parametrize(
    ("month", "number"),
    [
        ("enero", 1),
        ("febrero", 2),
        ("marzo", 3),
        ("abril", 4),
        ("mayo", 5),
        ("junio", 6),
        ("julio", 7),
        ("agosto", 8),
        ("septiembre", 9),
        ("octubre", 10),
        ("noviembre", 11),
        ("diciembre", 12),
    ],
)
def test_every_spanish_month_name(month: str, number: int) -> None:
    result = parse_publication_note([f"Publicada en el BOPA núm. 1, de 15 de {month} de 2020."])
    assert result is not None
    assert result.date == date(2020, number, 15)


@pytest.mark.parametrize("month", ["Octubre", "OCTUBRE"])
def test_month_names_are_case_insensitive(month: str) -> None:
    result = parse_publication_note([f"Publicada en el BOPA núm. 1, de 5 de {month} de 2020."])
    assert result is not None
    assert result.date == date(2020, 10, 5)


@pytest.mark.parametrize("marker", ["núm.", "núm", "número", "NÚM."])
def test_number_marker_variants(marker: str) -> None:
    result = parse_publication_note([f"Publicada en el BOPV {marker} 129, de 10 de julio de 1998."])
    assert result == GazettePublication("BOPV", "129", date(1998, 7, 10))


@pytest.mark.parametrize("gazette", ["bopa", "Bopa"])
def test_the_gazette_abbreviation_is_matched_as_written_in_capitals(gazette: str) -> None:
    assert (
        parse_publication_note([f"Publicada en el {gazette} núm. 206, de 23 de octubre de 2026."])
        is None
    )


def test_final_period_is_optional() -> None:
    with_period = parse_publication_note(["Publicada en el BOC núm. 94, de 17 de mayo de 2019."])
    without = parse_publication_note(["Publicada en el BOC núm. 94, de 17 de mayo de 2019"])
    assert with_period == without
    assert with_period is not None


def test_whitespace_is_flexible() -> None:
    note = "Publicada   en  el\nBOPA  núm.  206,  de  23  de\noctubre   de  2026."
    assert parse_publication_note([note]) == GazettePublication("BOPA", "206", date(2026, 10, 23))


def test_the_wording_is_case_insensitive() -> None:
    result = parse_publication_note(["publicada en el BOPA núm. 206, de 23 de octubre de 2026."])
    assert result is not None
    assert result.gazette == "BOPA"


def test_day_with_a_leading_zero() -> None:
    result = parse_publication_note(["Publicada en el BOPA núm. 206, de 03 de octubre de 2026."])
    assert result is not None
    assert result.date == date(2026, 10, 3)


def test_the_note_may_have_text_around_it() -> None:
    note = "Aviso. Publicada en el BOPA núm. 206, de 23 de octubre de 2026. Más texto."
    assert parse_publication_note([note]) == GazettePublication("BOPA", "206", date(2026, 10, 23))


def test_accepts_a_tuple_of_notes() -> None:
    notes = ("Otra.", "Publicada en el BON núm. 35, de 20 de marzo de 1991.")
    assert parse_publication_note(notes) == GazettePublication("BON", "35", date(1991, 3, 20))


def test_impossible_date_in_one_note_does_not_hide_a_valid_later_note() -> None:
    # C7: an impossible date does not match; the scan goes on to the next note.
    notes = [
        "Publicada en el BOPA núm. 206, de 31 de febrero de 2026.",
        "Publicada en el BON núm. 35, de 20 de marzo de 1991.",
    ]
    assert parse_publication_note(notes) == GazettePublication("BON", "35", date(1991, 3, 20))
