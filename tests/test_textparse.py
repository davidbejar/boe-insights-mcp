"""Contract tests for the text models and ``boe_insights_mcp.textparse.parse_block``.

Grounded in the recorded ``texto_bloque_*`` fixtures; rules the fixtures do not exercise use small
inline XML documents built with the helpers below.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Any

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


def test_version_without_effective_date_sorts_first() -> None:
    xml = doc(
        version_xml(law="dated", effective="20200101"),
        version_xml(law="undated", effective=None),
    )
    assert source_ids(parse_block(xml)) == ["undated", "dated"]


def test_versions_with_blank_or_missing_dates_keep_document_order_among_themselves() -> None:
    xml = doc(
        version_xml(law="dated", effective="20190101"),
        version_xml(law="blank", effective=""),
        version_xml(law="missing", effective=None),
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
