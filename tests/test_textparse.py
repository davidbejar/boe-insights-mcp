"""Contract tests for the text models and ``boe_insights_mcp.textparse.parse_block``.

Grounded in the recorded ``texto_bloque_*`` fixtures; rules the fixtures do not exercise use small
inline XML documents built with the helpers below.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError

from boe_insights_mcp.models import Block, BlockVersion, IndexEntry, Paragraph
from boe_insights_mcp.textparse import parse_block

Loader = Callable[[str], bytes]

A48 = "texto_bloque_a48_two_versions.xml"
A4_2 = "texto_bloque_a4-2_articulo46bis.xml"
A68 = "texto_bloque_a68_irpf_many_versions_trimmed.xml"
FUTURE = "texto_bloque_future_fecha_vigencia.xml"
REPEALED = "texto_bloque_a40_repealed_article_DERIVED.xml"
LAW_REPEALED = "texto_bloque_a1_repealed_law_no_marker.xml"
ALL_FIXTURES = [A48, REPEALED]
DT_RD1624 = "texto_bloque_disposicion_multiversion_rd1624_dt.xml"
NOTE_LEY30 = "texto_bloque_no_ley30_1992_two_versions_empty_vigencia.xml"
ANEXO_IVA = "texto_bloque_anexo_iva_1992_thead_noDesde_trimmed.xml"
TABLE_LEY29 = "texto_bloque_table_ley29_1987_a21_trimmed.xml"
BLOCKQUOTE_A1_2 = "texto_bloque_blockquote_rd1624_a1-2.xml"
PREAMBULO = "texto_bloque_preambulo_ley39_2015.xml"
TITULO_TI = "texto_bloque_encabezado_titulo_ley39_2015.xml"


# --- helpers ---------------------------------------------------------------------------------


def version_xml(
    body: str = "",
    *,
    law: str | None = "BOE-A-2000-1",
    published: str | None = "20000101",
    effective: str | None = "20000102",
) -> str:
    """Build one ``<version>`` element; a ``None`` attribute value is left out entirely."""
    attrs = {"id_norma": law, "fecha_publicacion": published, "fecha_vigencia": effective}
    rendered = "".join(f' {name}="{value}"' for name, value in attrs.items() if value is not None)
    return f"<version{rendered}>{body}</version>"


def doc(
    *versions: str,
    block_id: str | None = "a1",
    kind: str | None = "precepto",
    title: str | None = "Artículo 1",
) -> str:
    """Wrap versions in the ``<response><data><bloque>`` envelope of a block response."""
    attrs = {"id": block_id, "tipo": kind, "titulo": title}
    rendered = "".join(f' {name}="{value}"' for name, value in attrs.items() if value is not None)
    return (
        "<response><status><code>200</code></status><data>"
        f"<bloque{rendered}>{''.join(versions)}</bloque></data></response>"
    )


def paragraphs_of(body: str) -> tuple[Paragraph, ...]:
    """Parse one version holding ``body`` and return its paragraphs."""
    return parse_block(doc(version_xml(body))).versions[0].paragraphs


def source_ids(block: Block) -> list[str]:
    return [v.source_law_id for v in block.versions]


def para(text: str, kind: str = "parrafo", quoted: bool = False) -> Paragraph:
    return Paragraph(kind=kind, text=text, quoted=quoted)


def version_of(*paragraphs: Paragraph) -> BlockVersion:
    return BlockVersion(
        source_law_id="BOE-A-2000-1",
        published_on=None,
        effective_from=None,
        paragraphs=paragraphs,
    )


# --- fixture: article with two versions (Ley 40/2015, a48) ------------------------------------


@pytest.fixture
def a48(load_fixture: Loader) -> Block:
    return parse_block(load_fixture(A48))


def test_two_version_block_reads_identity_attributes(a48: Block) -> None:
    assert (a48.block_id, a48.kind, a48.title) == ("a48", "precepto", "Artículo 48")


def test_two_version_block_has_two_versions_with_their_source_laws(a48: Block) -> None:
    assert source_ids(a48) == ["BOE-A-2015-10566", "BOE-A-2020-17340"]


def test_two_version_block_reads_dates(a48: Block) -> None:
    first, second = a48.versions
    assert (first.published_on, first.effective_from) == (date(2015, 10, 2), date(2016, 10, 2))
    assert (second.published_on, second.effective_from) == (date(2020, 12, 31), date(2021, 1, 1))


def test_first_paragraph_is_the_article_heading(a48: Block) -> None:
    heading = a48.versions[0].paragraphs[0]
    assert heading.kind == "articulo"
    assert heading.text == "Artículo 48. Requisitos de validez y eficacia de los convenios."
    assert heading.quoted is False


def test_paragraph_counts_per_version(a48: Block) -> None:
    first, second = a48.versions
    assert len(first.paragraphs) == 12
    assert len(second.paragraphs) == 13  # twelve body paragraphs plus one footnote


def test_second_version_footnote_is_quoted_and_flattens_the_link(a48: Block) -> None:
    footnote = a48.versions[1].paragraphs[-1]
    assert footnote.kind == "nota_pie"
    assert footnote.quoted is True
    assert footnote.text == (
        "Se modifica el apartado 8 por la disposición final 2.1 del Real Decreto-ley 36/2020, "
        "de 30 de diciembre. Ref. BOE-A-2020-17340#df-2"
    )


def test_two_version_block_body_text_excludes_the_footnote(a48: Block) -> None:
    second = a48.versions[1]
    assert "Se modifica el apartado 8" not in second.text
    assert len(second.text.split("\n\n")) == 12
    assert second.notes == (second.paragraphs[-1].text,)


def test_the_amended_paragraph_differs_between_versions(a48: Block) -> None:
    first, second = a48.versions
    assert "en el plazo de 5 días hábiles" in second.text
    assert "en el plazo de 5 días hábiles" not in first.text


# --- fixture: IRPF article, odd attribute --------------------------------------------------------


@pytest.fixture
def a68(load_fixture: Loader) -> Block:
    return parse_block(load_fixture(A68))


def test_irpf_versions_come_in_ascending_order(a68: Block) -> None:
    assert source_ids(a68) == ["BOE-A-2006-20764", "BOE-A-2026-20823"]
    dates = [v.effective_from for v in a68.versions]
    assert dates == [date(2007, 1, 1), date(2026, 10, 8)]


def test_irpf_version_with_the_odd_fpub_attribute_still_parses(a68: Block) -> None:
    odd = a68.versions[1]
    assert odd.published_on == date(2026, 10, 7)
    assert odd.effective_from == date(2026, 10, 8)


def test_irpf_paragraph_and_footnote_counts(a68: Block) -> None:
    first, second = a68.versions
    assert (len(first.paragraphs), len(first.notes)) == (75, 1)
    assert (len(second.paragraphs), len(second.notes)) == (84, 15)


# --- fixture: Artículo 46 bis (opaque block id, NBSP in the title) ------------------------------


@pytest.fixture
def a4_2(load_fixture: Loader) -> Block:
    return parse_block(load_fixture(A4_2))


def test_block_id_is_the_opaque_attribute_not_the_article_number(a4_2: Block) -> None:
    assert a4_2.block_id == "a4-2"


def test_title_non_breaking_space_is_normalised(a4_2: Block) -> None:
    assert a4_2.title == "Artículo 46 bis"
    assert "\xa0" not in a4_2.title


def test_block_with_two_footnote_classes_reports_both_as_notes(a4_2: Block) -> None:
    only = a4_2.versions[0]
    assert [p.kind for p in only.paragraphs[-2:]] == ["nota_pie", "nota_pie_2"]
    assert len(only.notes) == 2
    assert only.notes[1].startswith("Téngase en cuenta")


def test_footnote_variant_class_is_not_part_of_the_body_text(a4_2: Block) -> None:
    only = a4_2.versions[0]
    assert len(only.paragraphs) == 5
    assert len(only.text.split("\n\n")) == 3
    assert "Téngase en cuenta" not in only.text


# --- fixture: future-dated version -------------------------------------------------------------


def test_future_dated_version_keeps_its_future_effective_date(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(FUTURE))
    (only,) = block.versions
    assert only.effective_from == date(2027, 7, 1)
    assert only.published_on == date(2026, 11, 20)
    assert only.source_law_id == "BOE-A-2026-24302"


# --- fixture: repealed article -------------------------------------------------------------------


@pytest.fixture
def repealed(load_fixture: Loader) -> Block:
    return parse_block(load_fixture(REPEALED))


def test_repealed_article_has_original_then_repeal_version(repealed: Block) -> None:
    assert source_ids(repealed) == ["BOE-A-1999-23750", "BOE-A-2018-10751"]
    assert repealed.versions[1].effective_from == date(2018, 7, 31)


def test_repeal_marker_text_is_stripped_of_markup_and_indentation(repealed: Block) -> None:
    marker = repealed.versions[1].paragraphs[1]
    assert (marker.kind, marker.text, marker.quoted) == ("parrafo", "(Derogado).", False)


def test_repeal_version_has_heading_marker_and_footnote_in_order(repealed: Block) -> None:
    paragraphs = repealed.versions[1].paragraphs
    assert [p.kind for p in paragraphs] == ["articulo", "parrafo", "nota_pie"]
    assert paragraphs[2].quoted is True
    assert paragraphs[2].text.startswith("Se deroga por la disposición derogatoria única")


def test_repealed_version_body_text_excludes_the_footnote(repealed: Block) -> None:
    version = repealed.versions[1]
    assert version.text == "Artículo 40. Potestad de inspección.\n\n(Derogado)."
    assert len(version.notes) == 1


# --- fixture: a plain single-version block ------------------------------------------------------


def test_single_version_block_of_a_repealed_law_parses_without_a_marker(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(LAW_REPEALED))
    (only,) = block.versions
    assert only.effective_from == date(1993, 2, 27)
    assert only.notes == ()
    assert [p.kind for p in only.paragraphs] == ["articulo", "parrafo"]


# --- nota_inicial block (as in texto_full_lopd1999_trimmed.xml) ---------------------------------

NOTA_INICIAL = (
    '<response><data><bloque id="no" tipo="nota_inicial">'
    '<version id_norma="BOE-A-2018-16673" fecha_publicacion="20181206" '
    'fecha_vigencia="20181207"><p class="siempreSeVe">Norma derogada, con efectos de 7 de '
    "diciembre de 2018, sin perjuicio de lo previsto en la disposición adicional 14 de la Ley "
    "Orgánica 3/2018, de 5 de diciembre, según establece su disposición derogatoria única.1. "
    '<a class="refPost">Ref. BOE-A-2018-16673#dd</a></p></version></bloque></data></response>'
)


def test_initial_note_block_without_title_parses() -> None:
    block = parse_block(NOTA_INICIAL)
    assert (block.block_id, block.kind, block.title) == ("no", "nota_inicial", "")


def test_initial_note_paragraph_keeps_its_class_and_flattens_the_link() -> None:
    (only,) = parse_block(NOTA_INICIAL).versions
    (note,) = only.paragraphs
    assert note.kind == "siempreSeVe"
    assert note.text.startswith("Norma derogada, con efectos de 7 de diciembre de 2018")
    assert note.text.endswith("derogatoria única.1. Ref. BOE-A-2018-16673#dd")


def test_initial_note_is_body_text_not_a_footnote() -> None:
    (only,) = parse_block(NOTA_INICIAL).versions
    assert only.notes == ()
    assert only.text.startswith("Norma derogada")


# --- block attributes ---------------------------------------------------------------------------


def test_missing_kind_and_title_default_to_empty_strings() -> None:
    block = parse_block(doc(version_xml("<p>x</p>"), kind=None, title=None))
    assert (block.kind, block.title) == ("", "")


def test_title_whitespace_is_collapsed_and_stripped() -> None:
    block = parse_block(doc(title="  Artículo\u00a0  46\u00a0bis \u00a0"))
    assert block.title == "Artículo 46 bis"


def test_block_without_versions_is_valid_and_empty() -> None:
    block = parse_block(doc())
    assert block.versions == ()
    assert block.block_id == "a1"


def test_version_without_children_has_no_paragraphs() -> None:
    (only,) = parse_block(doc(version_xml(""))).versions
    assert only.paragraphs == ()
    assert only.text == ""
    assert only.notes == ()


def test_unknown_version_and_block_attributes_are_ignored() -> None:
    xml = (
        '<response><data><bloque id="a1" tipo="precepto" titulo="Artículo 1" extra="z">'
        '<version id_norma="X" fpub="" fecha_publicacion="20000101" fecha_vigencia="20000102" '
        'otro="1"><p class="parrafo">t</p></version></bloque></data></response>'
    )
    (only,) = parse_block(xml).versions
    assert only.paragraphs == (Paragraph(kind="parrafo", text="t"),)


# --- paragraph kinds ----------------------------------------------------------------------------


def test_paragraph_kind_is_the_class_attribute() -> None:
    body = '<p class="articulo">A</p><p class="parrafo_2">B</p><p class="siempreSeVe">C</p>'
    assert [p.kind for p in paragraphs_of(body)] == ["articulo", "parrafo_2", "siempreSeVe"]


def test_paragraph_without_class_has_empty_kind() -> None:
    (only,) = paragraphs_of("<p>Sin clase</p>")
    assert (only.kind, only.text) == ("", "Sin clase")


def test_paragraph_defaults_to_not_quoted() -> None:
    (only,) = paragraphs_of('<p class="parrafo">x</p>')
    assert only.quoted is False


# --- blockquote ---------------------------------------------------------------------------------


def test_blockquote_yields_one_quoted_paragraph_per_child_paragraph() -> None:
    body = '<blockquote><p class="nota_pie">uno</p><p class="nota_pie_2">dos</p></blockquote>'
    assert paragraphs_of(body) == (
        Paragraph(kind="nota_pie", text="uno", quoted=True),
        Paragraph(kind="nota_pie_2", text="dos", quoted=True),
    )


def test_blockquote_with_bare_text_yields_one_quoted_paragraph() -> None:
    (only,) = paragraphs_of("<blockquote>  Texto   citado\n sin párrafo </blockquote>")
    assert only.text == "Texto citado sin párrafo"
    assert only.quoted is True


def test_quoted_flag_applies_only_inside_the_blockquote() -> None:
    body = (
        '<p class="parrafo">antes</p>'
        '<blockquote><p class="parrafo">dentro</p></blockquote>'
        '<p class="parrafo">después</p>'
    )
    assert [(p.text, p.quoted) for p in paragraphs_of(body)] == [
        ("antes", False),
        ("dentro", True),
        ("después", False),
    ]


# --- tables -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        pytest.param(
            "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
            "A | B\n1 | 2",
            id="th-and-td-rows",
        ),
        pytest.param(
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>",
            "A | B\n1 | 2\n3 | 4",
            id="thead-and-tbody",
        ),
        pytest.param(
            "<table><tr><td><strong>x</strong> y</td><td>  a\n  b </td><td>z</td></tr></table>",
            "x y | a b | z",
            id="inline-markup-and-whitespace-in-cells",
        ),
    ],
)
def test_table_renders_rows_as_lines_and_cells_joined_by_pipes(table: str, expected: str) -> None:
    (only,) = paragraphs_of(table)
    assert only.kind == "table"
    assert only.text == expected
    assert only.quoted is False


def test_table_is_one_paragraph_between_its_neighbours() -> None:
    body = (
        '<p class="parrafo">antes</p>'
        "<table><tr><td>a</td><td>b</td></tr></table>"
        '<p class="parrafo">después</p>'
    )
    assert [(p.kind, p.text) for p in paragraphs_of(body)] == [
        ("parrafo", "antes"),
        ("table", "a | b"),
        ("parrafo", "después"),
    ]


# --- unknown elements ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("element", "tag", "text"),
    [
        pytest.param("<div>hola <b>mundo</b></div>", "div", "hola mundo", id="div-with-inline"),
        pytest.param('<h3 class="x">Título</h3>', "h3", "Título", id="class-is-ignored"),
    ],
)
def test_unknown_element_keeps_its_text_with_the_tag_name_as_kind(
    element: str, tag: str, text: str
) -> None:
    (only,) = paragraphs_of(element)
    assert (only.kind, only.text) == (tag, text)


def test_unknown_element_without_text_is_skipped() -> None:
    assert paragraphs_of("<hr/><div> </div>") == ()


# --- inline elements, entities and whitespace ----------------------------------------------------


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        pytest.param(
            'Ver <a class="refPost">Ref. BOE-A-2020-1</a> aquí',
            "Ver Ref. BOE-A-2020-1 aquí",
            id="a",
        ),
        pytest.param("<strong>(Derogado).</strong>", "(Derogado).", id="strong-only"),
        pytest.param("énfasis <em>suave</em>.", "énfasis suave.", id="em"),
        pytest.param("100 m<sup>2</sup>", "100 m2", id="sup-attached"),
        pytest.param("uno<br/>dos", "uno dos", id="br-between-words"),
    ],
)
def test_inline_tags_contribute_their_text_in_place(inner: str, expected: str) -> None:
    (only,) = paragraphs_of(f'<p class="parrafo">{inner}</p>')
    assert only.text == expected


def test_entities_are_decoded() -> None:
    (only,) = paragraphs_of('<p class="parrafo">Ley 3&amp;4 &lt;x&gt; &quot;q&quot; &#233;</p>')
    assert only.text == 'Ley 3&4 <x> "q" é'


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("a\u00a0b", id="literal-nbsp"),
        pytest.param("a&#160;b", id="numeric-entity-nbsp"),
        pytest.param("  a \n\t b  ", id="runs-of-whitespace"),
        pytest.param("a\u00a0\u00a0 \nb", id="mixed-nbsp-and-whitespace"),
    ],
)
def test_nbsp_and_whitespace_normalise_to_single_spaces(raw: str) -> None:
    (only,) = paragraphs_of(f'<p class="parrafo">{raw}</p>')
    assert only.text == "a b"
    assert "\xa0" not in only.text


def test_whitespace_across_inline_boundaries_collapses() -> None:
    (only,) = paragraphs_of('<p class="parrafo">a\n<strong>b</strong>\n  c</p>')
    assert only.text == "a b c"


@pytest.mark.parametrize(
    "empty",
    [
        pytest.param('<p class="parrafo"></p>', id="empty-element"),
        pytest.param('<p class="parrafo">   \n </p>', id="whitespace"),
        pytest.param('<p class="parrafo"><strong> </strong></p>', id="empty-inline"),
        pytest.param('<p class="parrafo"><br/></p>', id="only-a-break"),
    ],
)
def test_empty_paragraphs_are_skipped(empty: str) -> None:
    body = f'<p class="parrafo">antes</p>{empty}<p class="parrafo">después</p>'
    assert [p.text for p in paragraphs_of(body)] == ["antes", "después"]


# --- dates --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20240229", date(2024, 2, 29)), ("19991214", date(1999, 12, 14)), ("", None), (None, None)],
)
def test_dates_parse_yyyymmdd_and_blank_or_missing_means_none(
    raw: str | None, expected: date | None
) -> None:
    (only,) = parse_block(doc(version_xml("<p>x</p>", published=raw, effective=raw))).versions
    assert only.published_on == expected
    assert only.effective_from == expected


@pytest.mark.parametrize(
    ("bad", "attribute"),
    [
        ("2020-01-01", "effective"),
        ("20201301", "effective"),
        ("2020", "effective"),
        ("20200230", "published"),
        ("abcdefgh", "published"),
    ],
)
def test_malformed_date_raises_value_error(bad: str, attribute: str) -> None:
    version = version_xml("<p>x</p>", **{attribute: bad})
    with pytest.raises(ValueError):
        parse_block(doc(version))


# --- version ordering ---------------------------------------------------------------------------


def test_versions_in_document_order_are_sorted_by_effective_date() -> None:
    xml = doc(
        version_xml(law="C", effective="20220101"),
        version_xml(law="A", effective="20200101"),
        version_xml(law="B", effective="20210101"),
    )
    assert source_ids(parse_block(xml)) == ["A", "B", "C"]


def test_versions_with_equal_dates_keep_document_order() -> None:
    xml = doc(
        version_xml(law="late", effective="20230101"),
        version_xml(law="first", effective="20200101"),
        version_xml(law="second", effective="20200101"),
        version_xml(law="third", effective="20200101"),
    )
    assert source_ids(parse_block(xml)) == ["first", "second", "third", "late"]


def test_version_without_any_date_sorts_first() -> None:
    xml = doc(
        version_xml(law="dated", effective="20200101"),
        version_xml(law="undated", effective=None, published=None),
    )
    assert source_ids(parse_block(xml)) == ["undated", "dated"]


def test_versions_with_no_dates_at_all_keep_document_order_among_themselves() -> None:
    xml = doc(
        version_xml(law="dated", effective="20190101"),
        version_xml(law="blank", effective="", published=None),
        version_xml(law="missing", effective=None, published=None),
    )
    assert source_ids(parse_block(xml)) == ["blank", "missing", "dated"]


# --- errors -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("", id="empty-string"),
        pytest.param(b"", id="empty-bytes"),
        pytest.param("this is not xml", id="plain-text"),
        pytest.param("<response><data><bloque id='a1'>", id="unclosed"),
    ],
)
def test_malformed_xml_raises_value_error(bad: str | bytes) -> None:
    with pytest.raises(ValueError):
        parse_block(bad)


@pytest.mark.parametrize(
    "no_block",
    [
        pytest.param("<response><data></data></response>", id="empty-data"),
        pytest.param("<response><status><code>404</code></status></response>", id="error-body"),
    ],
)
def test_document_without_a_block_raises_value_error(no_block: str) -> None:
    with pytest.raises(ValueError):
        parse_block(no_block)


def test_block_without_an_id_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_block(doc(version_xml("<p>x</p>"), block_id=None))


# --- safe parsing -------------------------------------------------------------------------------

_ENTITY_DOC = (
    '<?xml version="1.0"?><!DOCTYPE response [<!ENTITY secret "TOPSECRETVALUE">]>'
    '<response><data><bloque id="a1"><version id_norma="X" fecha_vigencia="20200101">'
    '<p class="parrafo">antes &secret; después</p></version></bloque></data></response>'
)
_EXTERNAL_DOC = (
    '<?xml version="1.0"?><!DOCTYPE response [<!ENTITY ext SYSTEM "file:///etc/passwd">]>'
    '<response><data><bloque id="a1"><version id_norma="X" fecha_vigencia="20200101">'
    '<p class="parrafo">&ext;</p></version></bloque></data></response>'
)


def _all_text(xml: str | bytes) -> str | None:
    """Parse and return every paragraph text, or ``None`` when parsing is refused."""
    try:
        block = parse_block(xml)
    except ValueError:
        return None
    return "\n".join(p.text for v in block.versions for p in v.paragraphs)


def test_internal_entity_definition_is_not_expanded() -> None:
    text = _all_text(_ENTITY_DOC)
    assert text is None or "TOPSECRETVALUE" not in text


def test_external_entity_is_not_loaded() -> None:
    text = _all_text(_EXTERNAL_DOC)
    assert text is None or "root:" not in text


def test_entity_expansion_bomb_fails_fast_or_stays_unexpanded() -> None:
    levels = ['<!ENTITY lol0 "lollollollol">']
    levels += [
        f'<!ENTITY lol{n} "{"".join(f"&lol{n - 1};" for _ in range(10))}">' for n in range(1, 7)
    ]
    bomb = (
        f'<?xml version="1.0"?><!DOCTYPE response [{"".join(levels)}]>'
        '<response><data><bloque id="a1"><version id_norma="X" fecha_vigencia="20200101">'
        '<p class="parrafo">&lol6;</p></version></bloque></data></response>'
    )
    started = time.monotonic()
    text = _all_text(bomb)
    assert time.monotonic() - started < 2.0
    assert text is None or len(text) < 10_000


# --- str and bytes input ------------------------------------------------------------------------


def test_str_and_bytes_input_give_the_same_block() -> None:
    xml = doc(version_xml('<p class="parrafo">Ñandú: señal</p>'))
    assert parse_block(xml) == parse_block(xml.encode("utf-8"))


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_fixture_parses_identically_from_bytes_and_from_decoded_str(
    load_fixture: Loader, name: str
) -> None:
    raw = load_fixture(name)
    assert parse_block(raw) == parse_block(raw.decode("utf-8"))


def test_bytes_honour_the_declared_encoding() -> None:
    xml = (
        '<?xml version="1.0" encoding="iso-8859-1"?>'
        + doc(version_xml('<p class="parrafo">Sesión añadida</p>'))
    ).encode("iso-8859-1")
    (only,) = parse_block(xml).versions
    assert only.paragraphs[0].text == "Sesión añadida"


# --- derived properties of BlockVersion ---------------------------------------------------------


def test_text_joins_body_paragraphs_with_blank_lines() -> None:
    version = version_of(para("uno", "articulo"), para("dos"), para("tres", "parrafo_2"))
    assert version.text == "uno\n\ndos\n\ntres"


def test_text_prefixes_quoted_paragraphs_with_a_quote_marker() -> None:
    version = version_of(para("uno"), para("cita", quoted=True), para("tres"))
    assert version.text == "uno\n\n> cita\n\ntres"


@pytest.mark.parametrize("kind", ["nota_pie", "nota_pie_2"])
def test_text_excludes_footnotes_of_any_footnote_class(kind: str) -> None:
    version = version_of(para("cuerpo"), para("pie", kind), para("pie citado", kind, quoted=True))
    assert version.text == "cuerpo"


@pytest.mark.parametrize("kind", ["nota_inicial", "siempreSeVe", ""])
def test_text_keeps_non_footnote_kinds_even_when_named_like_notes(kind: str) -> None:
    assert version_of(para("contenido", kind)).text == "contenido"


def test_notes_returns_footnote_texts_in_order() -> None:
    version = version_of(
        para("cuerpo"),
        para("primera", "nota_pie", quoted=True),
        para("otro cuerpo"),
        para("segunda", "nota_pie_2"),
    )
    assert version.notes == ("primera", "segunda")


# --- IndexEntry ---------------------------------------------------------------------------------


def test_index_entry_reads_the_api_names_and_normalises_the_title() -> None:
    entry = IndexEntry.model_validate(
        {"id": "a4-2", "titulo": "Artículo\xa046  bis ", "fecha_actualizacion": "20191105"}
    )
    assert (entry.block_id, entry.title, entry.updated_on) == (
        "a4-2",
        "Artículo 46 bis",
        date(2019, 11, 5),
    )


def test_index_entries_of_a_recorded_index_validate(load_json: Callable[[str], Any]) -> None:
    raw = load_json("texto_indice_ley40_2015_trimmed.json")["data"][0]["bloque"]
    entries = [IndexEntry.model_validate(item) for item in raw]
    assert len(entries) == 45
    by_id = {e.block_id: e for e in entries}
    assert by_id["a4-2"].title == "Artículo 46 bis"


# --- immutability -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "attribute", "value"),
    [
        pytest.param(lambda: para("x"), "text", "y", id="paragraph"),
        pytest.param(lambda: version_of(para("x")), "source_law_id", "Z", id="version"),
        pytest.param(
            lambda: Block(block_id="a1", kind="precepto", title="t", versions=()),
            "title",
            "otro",
            id="block",
        ),
        pytest.param(
            lambda: IndexEntry.model_validate({"id": "a1", "titulo": "t"}),
            "title",
            "otro",
            id="index-entry",
        ),
    ],
)
def test_models_are_frozen(make: Callable[[], Any], attribute: str, value: str) -> None:
    model = make()
    with pytest.raises(ValidationError):
        setattr(model, attribute, value)


# === Version ordering by start date: effective_from, else published_on (spec section 8) ==========


def test_version_with_an_empty_effective_date_is_ordered_by_its_publication_date() -> None:
    xml = doc(
        version_xml(law="last", effective="", published="20260228"),
        version_xml(law="middle", effective="20260205", published="20260204"),
        version_xml(law="first", effective="20170528", published="20170527"),
    )
    assert source_ids(parse_block(xml)) == ["first", "middle", "last"]


def test_version_with_a_missing_effective_date_is_ordered_by_its_publication_date() -> None:
    xml = doc(
        version_xml(law="later", effective=None, published="20220601"),
        version_xml(law="earlier", effective="20200101", published="20191201"),
    )
    assert source_ids(parse_block(xml)) == ["earlier", "later"]


def test_version_without_effective_date_published_before_a_dated_one_sorts_before_it() -> None:
    xml = doc(
        version_xml(law="dated", effective="20200101", published="20191201"),
        version_xml(law="undated", effective="", published="20151002"),
    )
    assert source_ids(parse_block(xml)) == ["undated", "dated"]


def test_versions_sharing_a_start_date_keep_document_order_across_both_sources() -> None:
    xml = doc(
        version_xml(law="from-publication", effective="", published="20200101"),
        version_xml(law="from-effect", effective="20200101", published="20190101"),
    )
    assert source_ids(parse_block(xml)) == ["from-publication", "from-effect"]


def test_version_without_any_date_still_sorts_before_dated_versions_whatever_their_order() -> None:
    xml = doc(
        version_xml(law="dated", effective="20200101", published="20191201"),
        version_xml(law="nodate", effective=None, published=None),
    )
    assert source_ids(parse_block(xml)) == ["nodate", "dated"]


def test_parsed_versions_keep_their_own_dates_after_reordering() -> None:
    xml = doc(
        version_xml(law="last", effective="", published="20260228"),
        version_xml(law="first", effective="20170528", published="20170527"),
    )
    last = parse_block(xml).versions[1]
    assert (last.effective_from, last.published_on) == (None, date(2026, 2, 28))
    assert last.starts_on == date(2026, 2, 28)


def test_fixture_with_an_empty_last_vigencia_parses_in_chronological_order(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(DT_RD1624))
    assert source_ids(block) == [
        "BOE-A-2017-5857",
        "BOE-A-2019-13563",
        "BOE-A-2025-26458",
        "BOE-A-2026-2024",
        "BOE-A-2026-2547",
        "BOE-A-2026-4667",
    ]
    assert (block.block_id, block.kind, block.title) == (
        "dt",
        "precepto",
        "Disposición transitoria cuarta",
    )
    last = block.versions[-1]
    assert (last.effective_from, last.published_on) == (None, date(2026, 2, 28))


def test_fixture_with_the_repeal_note_listed_before_the_original_is_reordered(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(NOTE_LEY30))
    assert source_ids(block) == ["BOE-A-1992-2631", "BOE-A-2015-10565"]
    assert [v.starts_on for v in block.versions] == [date(1993, 2, 27), date(2015, 10, 2)]


def test_fixture_with_an_out_of_order_vigencia_is_sorted_ascending(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    assert source_ids(block) == [
        "BOE-A-1992-28740",
        "BOE-A-1994-28968",
        "BOE-A-2014-12329",
        "BOE-A-2022-22128",
    ]
    assert [v.effective_from for v in block.versions] == [
        date(1993, 1, 1),
        date(1995, 1, 1),
        date(2015, 1, 1),
        date(2023, 1, 1),
    ]


@pytest.mark.parametrize("fixture", [DT_RD1624, NOTE_LEY30, ANEXO_IVA, A68, A48])
def test_every_parsed_fixture_has_versions_in_ascending_start_order(
    load_fixture: Loader, fixture: str
) -> None:
    starts = [v.starts_on for v in parse_block(load_fixture(fixture)).versions]
    assert all(start is not None for start in starts)
    assert starts == sorted(starts)  # type: ignore[type-var]


# === Tables in real fixtures ======================================================================

TABLE_KINDS_NEVER_SEEN_AS_PARAGRAPHS = {
    "table_row",
    "tr",
    "td",
    "th",
    "thead",
    "tbody",
    "colgroup",
    "col",
}


def table_paragraphs(block: Block, law: str) -> list[Paragraph]:
    """The ``table`` paragraphs of the version introduced by ``law``."""
    (version,) = (v for v in block.versions if v.source_law_id == law)
    return [p for p in version.paragraphs if p.kind == "table"]


def xml_tables(fixture_bytes: bytes, law: str) -> list[ElementTree.Element]:
    """The ``<table>`` elements of one version, read with the standard parser as an oracle."""
    root = ElementTree.fromstring(fixture_bytes)
    (version,) = (v for v in root.iter("version") if v.get("id_norma") == law)
    return [child for child in version if child.tag == "table"]


def oracle_lines(table: ElementTree.Element) -> list[str]:
    """Expected table text: one line per ``<tr>`` in document order, cells joined by `` | ``."""
    lines = []
    for row in table.iter("tr"):
        cells = [" ".join(" ".join(c.itertext()).replace(chr(0xA0), " ").split()) for c in row]
        lines.append(" | ".join(cells))
    return lines


def test_ley29_table_with_th_headers_is_one_paragraph_with_one_line_per_row(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(TABLE_LEY29))
    (table,) = table_paragraphs(block, "BOE-A-1987-28141")
    lines = table.text.split("\n")
    assert len(lines) == 17
    assert lines[0] == (
        "Base liquidable hasta pesetas | Tipo medio - % | Cuota íntegra | "
        "Resto base - Millones | Tipo marginal - %"
    )
    assert lines[1] == "- | - | - | 1 | 7,65"
    assert lines[2] == "Un millón | 7,65 | 76,500 | 1 | 8,50"
    assert lines[-1] == "Cien millones | 24,99 | 24.990.000 | Exceso | 34,00"


def test_ley29_second_version_table_keeps_an_empty_cell_in_its_column(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(TABLE_LEY29))
    (table,) = table_paragraphs(block, "BOE-A-2001-24962")
    lines = table.text.split("\n")
    assert len(lines) == 17
    assert lines[0] == (
        "Base liquidable - Hasta euros | Cuota íntegra – Euros | "  # noqa: RUF001
        "Resto base liquidable – Hasta euros | Tipo aplicable – Porcentaje"  # noqa: RUF001
    )
    assert lines[1] == "0,00 |  | 7.993,46 | 7,65"
    assert lines[2] == "7.993,46 | 611,50 | 7.987,45 | 8,50"
    assert lines[-1] == "797.555,08 | 199.291,40 | en adelante | 34,00"


def test_table_sits_between_its_neighbouring_paragraphs_in_the_real_article(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(TABLE_LEY29))
    second = next(v for v in block.versions if v.source_law_id == "BOE-A-2001-24962")
    kinds = [p.kind for p in second.paragraphs]
    assert kinds[:5] == ["articulo", "parrafo", "parrafo", "table", "parrafo"]
    assert kinds[5:] == ["nota_pie"] * 4 + ["nota_pie_2"] + kinds[10:]
    assert all(p.quoted for p in second.paragraphs[5:])
    assert not second.paragraphs[3].quoted


@pytest.mark.parametrize(
    ("law", "tables"),
    [
        ("BOE-A-1992-28740", 0),
        ("BOE-A-1994-28968", 0),
        ("BOE-A-2014-12329", 4),
        ("BOE-A-2022-22128", 4),
    ],
)
def test_annex_yields_exactly_one_table_paragraph_per_table_element(
    load_fixture: Loader, law: str, tables: int
) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    assert len(table_paragraphs(block, law)) == tables
    assert len(xml_tables(load_fixture(ANEXO_IVA), law)) == tables


@pytest.mark.parametrize("law", ["BOE-A-2014-12329", "BOE-A-2022-22128"])
def test_annex_table_cells_never_leak_out_as_paragraphs_of_their_own(
    load_fixture: Loader, law: str
) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    (version,) = (v for v in block.versions if v.source_law_id == law)
    assert not {p.kind for p in version.paragraphs} & TABLE_KINDS_NEVER_SEEN_AS_PARAGRAPHS
    assert not any(p.quoted for p in version.paragraphs if p.kind == "table")


@pytest.mark.parametrize("fixture", [TABLE_LEY29, ANEXO_IVA])
def test_every_table_matches_one_line_per_row_with_cells_joined_by_pipes(
    load_fixture: Loader, fixture: str
) -> None:
    raw = load_fixture(fixture)
    block = parse_block(raw)
    for version in block.versions:
        expected = [oracle_lines(t) for t in xml_tables(raw, version.source_law_id)]
        parsed = [p.text.split("\n") for p in version.paragraphs if p.kind == "table"]
        assert parsed == expected


def test_th_header_row_in_tbody_is_included_as_the_first_line(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    tables = table_paragraphs(block, "BOE-A-2014-12329")
    assert [t.text.split("\n")[0] for t in tables] == [
        "Códi. NCE | Designación de la mercancía",
        "Cód. NCE | Designación de la mercancía",
        "12,5 | kilogramos.",  # a headerless table: its first row is data
        "Cód. NCE | Designación de la mercancía",
    ]
    assert [len(t.text.split("\n")) for t in tables] == [2, 24, 21, 11]


def test_thead_row_with_td_cells_is_included_before_the_body_rows(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    tables = table_paragraphs(block, "BOE-A-2022-22128")
    first, second = tables[0], tables[1]
    assert first.text.split("\n") == [
        "Cód. NCE | Designación de la Mercancía",
        "7204 | Desperdicios y desechos de fundición de hierro o acero (chatarra y lingotes).",
    ]
    lines = second.text.split("\n")
    assert len(lines) == 27
    assert lines[0] == "Cod. NCE | Designación de la mercancía"
    assert lines[1] == "7402 | Cobre sin refinar; ánodos de cobre para refinado."
    assert lines[2] == "7403 | Cobre refinado en forma de cátodos y secciones de cátodo."


def test_colgroup_contributes_no_text_and_no_blank_lines(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    for table in table_paragraphs(block, "BOE-A-2022-22128"):
        lines = table.text.split("\n")
        assert all(line.strip() for line in lines)
        assert "row_column" not in table.text


def test_empty_cell_after_a_cell_wrapper_stays_a_blank_cell_not_a_dropped_one(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    second = table_paragraphs(block, "BOE-A-2022-22128")[1]
    assert second.text.split("\n")[-1] == " | Baterías de plomo recuperadas."


@pytest.mark.parametrize("fixture", [TABLE_LEY29, ANEXO_IVA])
def test_table_text_has_no_stray_pipes_or_blank_lines(load_fixture: Loader, fixture: str) -> None:
    block = parse_block(load_fixture(fixture))
    for version in block.versions:
        for paragraph in version.paragraphs:
            if paragraph.kind != "table":
                continue
            lines = paragraph.text.split("\n")
            widths = {len(line.split(" | ")) for line in lines}
            assert len(widths) == 1, "rows of one table have the same number of cells"
            assert all(line.strip(" |") for line in lines), "no row of only separators"
            assert "||" not in paragraph.text
            assert "  " not in paragraph.text.replace(" |  | ", " | | ")


# === Blockquotes in real fixtures =================================================================


def test_blockquote_paragraphs_keep_their_own_kind_and_are_quoted(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(BLOCKQUOTE_A1_2))
    original, repeal, added = block.versions
    assert [(p.kind, p.quoted) for p in original.paragraphs] == [
        ("articulo", False),
        ("parrafo", False),
        ("parrafo", False),
    ]
    assert [(p.kind, p.quoted) for p in repeal.paragraphs] == [
        ("articulo", False),
        ("parrafo", False),
        ("nota_pie", True),
    ]
    assert [(p.kind, p.quoted) for p in added.paragraphs] == [
        ("articulo", False),
        ("parrafo", False),
        ("nota_pie", True),
        ("nota_pie", True),
    ]


def test_blockquote_footnote_text_is_kept_and_exposed_as_a_note(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(BLOCKQUOTE_A1_2))
    repeal = block.versions[1]
    assert repeal.notes == (
        "Se deroga por el art. 1.1 del Real Decreto 296/1998, de 27 de febrero. "
        "Ref. BOE-A-1998-4774",
    )
    assert repeal.text == "Artículo 1. Actividades de los Entes públicos.\n\n(Derogado)"
    added = block.versions[2]
    assert len(added.notes) == 2
    assert added.notes[0].startswith(
        "Se añade por el art. 1.1 del Real Decreto 424/2021, de 15 de junio."
    )


def test_blockquote_creates_no_paragraph_of_its_own(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(BLOCKQUOTE_A1_2))
    assert all(p.kind != "blockquote" for v in block.versions for p in v.paragraphs)


def test_blockquote_with_a_class_is_handled_like_a_plain_one(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    (version,) = (v for v in block.versions if v.source_law_id == "BOE-A-2014-12329")
    quoted_body = [p for p in version.paragraphs if p.quoted and not p.kind.startswith("nota_pie")]
    assert [p.kind for p in quoted_body] == [
        "parrafo",
        "cita_con_pleca",
        "parrafo",
        "parrafo",
        "parrafo_2",
        "parrafo",
        "parrafo",
        "parrafo",
        "parrafo",
    ]
    assert quoted_body[0].text.startswith("Téngase en cuenta que esta modificación del apartado 5")
    assert quoted_body[1].text == "Redacción anterior:"
    assert quoted_body[2].text.startswith("Quinto. Régimen de depósito distinto de los aduaneros:")


def test_classed_blockquote_content_is_part_of_the_body_text_marked_as_quoted(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    (version,) = (v for v in block.versions if v.source_law_id == "BOE-A-2014-12329")
    blocks = version.text.split("\n\n")
    assert "> Redacción anterior:" in blocks
    assert any(
        b.startswith("> Téngase en cuenta que esta modificación del apartado 5") for b in blocks
    )
    assert not any(b.startswith("> Se añaden los apartados 8 y 10") for b in blocks), "footnotes"


def test_plain_blockquote_footnotes_after_the_tables_are_notes(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    (version,) = (v for v in block.versions if v.source_law_id == "BOE-A-2014-12329")
    assert len(version.notes) == 14
    assert version.notes[0].startswith("Se añaden los apartados 8 y 10 y se modifica el 5")


def test_paragraphs_outside_any_blockquote_are_not_quoted_in_the_annex(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    first = block.versions[0]
    assert first.paragraphs[0].kind == "anexo"
    assert first.paragraphs[0].text == "ANEXO"
    assert not first.paragraphs[0].quoted
    assert [p.quoted for p in first.paragraphs].count(True) == 1  # its single footnote


# === Preamble, heading and initial-note blocks ====================================================


def test_preamble_block_has_kind_preambulo_an_empty_title_and_one_version(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(PREAMBULO))
    assert (block.block_id, block.kind, block.title) == ("preambulo", "preambulo", "")
    assert len(block.versions) == 1
    version = block.versions[0]
    assert (version.source_law_id, version.published_on, version.effective_from) == (
        "BOE-A-2015-10565",
        date(2015, 10, 2),
        date(2016, 10, 2),
    )


def test_preamble_paragraph_kinds_are_the_classes_of_the_source(load_fixture: Loader) -> None:
    (version,) = parse_block(load_fixture(PREAMBULO)).versions
    counts = Counter(p.kind for p in version.paragraphs)
    assert counts == {"parrafo": 44, "centro_redonda": 7, "parrafo_2": 6}
    assert version.paragraphs[0].kind == "centro_redonda"
    assert version.paragraphs[0].text == "FELIPE VI"
    assert version.paragraphs[1].text == "REY DE ESPAÑA"


def test_preamble_has_no_notes_and_no_quoted_paragraphs(load_fixture: Loader) -> None:
    (version,) = parse_block(load_fixture(PREAMBULO)).versions
    assert version.notes == ()
    assert not any(p.quoted for p in version.paragraphs)
    assert version.text.startswith("FELIPE VI\n\nREY DE ESPAÑA")


def test_heading_block_has_kind_encabezado_and_its_title(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(TITULO_TI))
    assert (block.block_id, block.kind, block.title) == ("ti", "encabezado", "TÍTULO I")
    assert len(block.versions) == 1


def test_heading_block_paragraphs_are_number_and_name(load_fixture: Loader) -> None:
    (version,) = parse_block(load_fixture(TITULO_TI)).versions
    assert [(p.kind, p.text) for p in version.paragraphs] == [
        ("titulo_num", "TÍTULO I"),
        ("titulo_tit", "De los interesados en el procedimiento"),
    ]
    assert version.text == "TÍTULO I\n\nDe los interesados en el procedimiento"
    assert (version.published_on, version.effective_from) == (date(2015, 10, 2), date(2016, 10, 2))


def test_annex_block_is_an_encabezado_with_anexo_paragraphs(load_fixture: Loader) -> None:
    block = parse_block(load_fixture(ANEXO_IVA))
    assert (block.block_id, block.kind, block.title) == ("an", "encabezado", "ANEXO")
    assert block.versions[0].paragraphs[0].kind == "anexo"


def test_repeal_initial_note_is_an_untitled_nota_inicial_of_siempre_se_ve_paragraphs(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture("texto_bloque_no_lopd1999_repealed.xml"))
    assert (block.block_id, block.kind, block.title) == ("no", "nota_inicial", "")
    (version,) = block.versions
    (note,) = version.paragraphs
    assert note.kind == "siempreSeVe"
    assert note.text.startswith("Norma derogada, con efectos de 7 de diciembre de 2018")
    assert version.notes == ()


def test_initial_note_with_two_versions_keeps_both_and_orders_them_by_date(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture("texto_bloque_no_ley11_2007_repealed_with_tengase.xml"))
    assert [v.effective_from for v in block.versions] == [date(2016, 10, 2), date(2018, 9, 4)]
    first, second = block.versions
    assert [p.kind for p in first.paragraphs] == ["siempreSeVe", "siempreSeVe"]
    assert first.paragraphs[0].text.startswith(
        "Norma derogada, con efectos de 2 de octubre de 2016"
    )
    assert first.paragraphs[1].text.startswith("Téngase en cuenta que la disposición final 7")
    assert second.paragraphs[1].text.startswith("No obstante, hasta que")


def test_initial_note_blank_paragraphs_are_skipped(load_fixture: Loader) -> None:
    block = parse_block(load_fixture("texto_bloque_no_rd1624_1992_correction_not_repeal.xml"))
    (version,) = block.versions
    (note,) = version.paragraphs
    assert note.kind == "textoCompleto"
    assert note.text.startswith("Incluye la corrección de errores publicada en BOE núm. 33")


def test_initial_note_with_an_empty_vigencia_keeps_its_publication_date(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture(NOTE_LEY30))
    original, repeal = block.versions
    assert [(p.kind) for p in original.paragraphs] == ["textoCompleto"]
    assert original.paragraphs[0].text.startswith("Incluye las correcciones de errores")
    assert (repeal.effective_from, repeal.published_on) == (None, date(2015, 10, 2))
    assert [p.kind for p in repeal.paragraphs] == ["siempreSeVe", "siempreSeVe"]


def test_rename_note_is_a_future_dated_nota_inicial_and_ignores_the_odd_attribute(
    load_fixture: Loader,
) -> None:
    block = parse_block(load_fixture("texto_bloque_no_rd424_2005_rename_not_repeal.xml"))
    (version,) = block.versions
    assert (version.published_on, version.effective_from) == (date(2026, 11, 19), date(2027, 1, 1))
    assert version.paragraphs[0].text.startswith(
        "Con efectos de 1 de enero de 2027, esta norma pasa a denominarse"
    )
