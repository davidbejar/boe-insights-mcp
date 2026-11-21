"""Contract tests for the full-text parsing of ``textparse`` (spec A1) and ``Block.expires_on``.

Covers ``parse_full_text`` (the body of ``GET legislacion-consolidada/id/{id}/texto``), and the two
fields ``parse_block`` now fills: ``expires_on`` and ``expired_marker`` (the ``fecha_caducidad``
attribute of ``<bloque>``, api-notes "Verified after the third probe"). Real data comes from the
``texto_full_*`` fixtures (whole ``<bloque>`` elements removed from real responses); edge cases use
small inline XML built with the helpers below.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import date

import pytest

from boe_insights_mcp.models import Block
from boe_insights_mcp.textparse import parse_block, parse_full_text

Loader = Callable[[str], bytes]

NBSP = chr(0xA0)

ASTURIAS = "texto_full_asturias_ley8_2026_not_yet_in_force_trimmed.xml"
LEY40 = "texto_full_ley40_2015_trimmed.xml"
CODIGO_CIVIL = "texto_full_codigo_civil_trimmed.xml"


# --- helpers ---------------------------------------------------------------------------------


def version_xml(
    body: str = "<p class='parrafo'>Texto.</p>",
    *,
    published: str = "20000101",
    effective: str = "20000102",
) -> str:
    """Build one ``<version>`` element."""
    return (
        f'<version id_norma="BOE-A-2000-1" fecha_publicacion="{published}" '
        f'fecha_vigencia="{effective}">{body}</version>'
    )


def bloque_xml(
    block_id: str | None = "a1",
    *,
    kind: str | None = "precepto",
    title: str | None = "Artículo 1",
    caducidad: str | None = None,
    versions: tuple[str, ...] | None = None,
) -> str:
    """Build one ``<bloque>``; a ``None`` attribute value is left out entirely."""
    attrs = {"id": block_id, "tipo": kind, "titulo": title, "fecha_caducidad": caducidad}
    rendered = "".join(f' {name}="{value}"' for name, value in attrs.items() if value is not None)
    inner = "".join(versions if versions is not None else (version_xml(),))
    return f"<bloque{rendered}>{inner}</bloque>"


def full_text_xml(*blocks: str) -> str:
    """Wrap blocks in the ``<response><data><texto>`` envelope of ``/texto``."""
    return (
        "<response><status><code>200</code><text>ok</text></status><data><texto>"
        f"{''.join(blocks)}</texto></data></response>"
    )


def single_block_xml(block: str) -> str:
    """Wrap one block in the ``<response><data>`` envelope of ``/texto/bloque/{id}``."""
    return f"<response><status><code>200</code></status><data>{block}</data></response>"


def parse_one(
    block_id: str | None = "a1",
    *,
    kind: str | None = "precepto",
    title: str | None = "Artículo 1",
    caducidad: str | None = None,
    versions: tuple[str, ...] | None = None,
) -> Block:
    """Parse a one-block full text built with ``bloque_xml``."""
    xml = bloque_xml(block_id, kind=kind, title=title, caducidad=caducidad, versions=versions)
    (block,) = parse_full_text(full_text_xml(xml))
    return block


# --- Block.expires_on / expired_marker, built directly ------------------------------------------


def test_block_defaults_to_not_expired() -> None:
    block = Block(block_id="a1", kind="precepto", title="Artículo 1", versions=())
    assert block.expires_on is None
    assert block.expired_marker is False


def test_block_accepts_an_expiry_date_and_marker() -> None:
    block = Block(
        block_id="a1",
        kind="precepto",
        title="Artículo 1",
        versions=(),
        expires_on=date(1981, 7, 20),
        expired_marker=True,
    )
    assert block.expires_on == date(1981, 7, 20)
    assert block.expired_marker is True


# --- fecha_caducidad via parse_full_text -----------------------------------------------------


def test_real_expiry_date_sets_both_fields() -> None:
    block = parse_one(caducidad="19810720")
    assert block.expires_on == date(1981, 7, 20)
    assert block.expired_marker is True


def test_sentinel_00000001_has_no_date_but_sets_the_marker() -> None:
    block = parse_one(caducidad="00000001")
    assert block.expires_on is None
    assert block.expired_marker is True


def test_absent_attribute_means_not_expired() -> None:
    block = parse_one(caducidad=None)
    assert block.expires_on is None
    assert block.expired_marker is False


@pytest.mark.parametrize("value", ["abc", "20211340", "20210230", "2021", "99999999", "0000000A"])
def test_any_non_date_value_has_no_date_but_sets_the_marker_and_does_not_raise(
    value: str,
) -> None:
    block = parse_one(caducidad=value)
    assert block.expires_on is None
    assert block.expired_marker is True


def test_expiry_date_is_independent_of_the_version_dates() -> None:
    # Known data oddity (FINDINGS D.4): versions may start after the expiry date.
    block = parse_one(caducidad="19810720", versions=(version_xml(effective="19990101"),))
    assert block.expires_on == date(1981, 7, 20)
    assert block.versions[0].effective_from == date(1999, 1, 1)


def test_empty_attribute_is_the_same_as_an_absent_one() -> None:
    block = parse_one(caducidad="")
    assert block.expires_on is None
    assert block.expired_marker is False


# --- fecha_caducidad via parse_block ---------------------------------------------------------


def test_parse_block_fills_a_real_expiry_date() -> None:
    block = parse_block(single_block_xml(bloque_xml(caducidad="20030710")))
    assert (block.expires_on, block.expired_marker) == (date(2003, 7, 10), True)


def test_parse_block_sentinel_and_absent() -> None:
    sentinel = parse_block(single_block_xml(bloque_xml(caducidad="00000001")))
    absent = parse_block(single_block_xml(bloque_xml()))
    assert (sentinel.expires_on, sentinel.expired_marker) == (None, True)
    assert (absent.expires_on, absent.expired_marker) == (None, False)


def test_parse_block_garbage_value_does_not_raise() -> None:
    block = parse_block(single_block_xml(bloque_xml(caducidad="soon")))
    assert (block.expires_on, block.expired_marker) == (None, True)


# --- parse_full_text: shape and order --------------------------------------------------------


def test_returns_a_list_of_blocks() -> None:
    blocks = parse_full_text(full_text_xml(bloque_xml("a1"), bloque_xml("a2")))
    assert isinstance(blocks, list)
    assert all(isinstance(block, Block) for block in blocks)


def test_document_order_is_preserved_not_sorted() -> None:
    xml = full_text_xml(*(bloque_xml(block_id) for block_id in ("z9", "a1", "m5", "a1-2", "b")))
    assert [b.block_id for b in parse_full_text(xml)] == ["z9", "a1", "m5", "a1-2", "b"]


def test_accepts_bytes_and_str() -> None:
    xml = full_text_xml(bloque_xml("a1"), bloque_xml("a2"))
    assert parse_full_text(xml) == parse_full_text(xml.encode("utf-8"))


def test_each_block_is_parsed_exactly_as_parse_block_parses_it() -> None:
    blocks = (
        bloque_xml("a1", caducidad="19810720"),
        bloque_xml("a2", kind="encabezado", title="TÍTULO I"),
        bloque_xml(
            "a3",
            caducidad="00000001",
            versions=(
                version_xml(effective="20100101"),
                version_xml("<p class='parrafo'>Antes.</p>", effective="19900101"),
            ),
        ),
    )
    parsed = parse_full_text(full_text_xml(*blocks))
    expected = [parse_block(single_block_xml(block)) for block in blocks]
    assert parsed == expected


def test_versions_of_each_block_are_sorted_ascending_like_parse_block() -> None:
    block = parse_one(
        versions=(
            version_xml("<p class='parrafo'>Nueva.</p>", effective="20200101"),
            version_xml("<p class='parrafo'>Vieja.</p>", effective="19900101"),
        )
    )
    assert [v.paragraphs[0].text for v in block.versions] == ["Vieja.", "Nueva."]


# --- parse_full_text: attributes -------------------------------------------------------------


def test_missing_titulo_gives_an_empty_title() -> None:
    block = parse_one(block_id="co", kind="nota_inicial", title=None)
    assert block.title == ""
    assert (block.block_id, block.kind) == ("co", "nota_inicial")


def test_unknown_tipo_is_kept_as_it_is() -> None:
    assert parse_one(kind="rareza_nueva").kind == "rareza_nueva"


def test_missing_tipo_gives_an_empty_kind() -> None:
    assert parse_one(kind=None).kind == ""


def test_title_whitespace_is_normalised_like_parse_block() -> None:
    assert parse_one(title=f"Artículo{NBSP} 46").title == "Artículo 46"


# --- parse_full_text: errors and hardening ---------------------------------------------------


@pytest.mark.parametrize("body", ["", "<response><data><texto><bloque id='a1'>", "not xml at all"])
def test_malformed_body_raises_value_error_like_parse_block(body: str) -> None:
    with pytest.raises(ValueError):
        parse_full_text(body)
    with pytest.raises(ValueError):
        parse_block(body)


def test_entity_declaration_is_refused_with_value_error() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        "<response><data><texto><bloque id='a1'><version><p>&b;</p></version></bloque>"
        "</texto></data></response>"
    )
    with pytest.raises(ValueError):
        parse_full_text(bomb)


def test_document_nested_too_deep_raises_value_error() -> None:
    deep = "<response>" + "<d>" * 500 + "</d>" * 500 + "</response>"
    with pytest.raises(ValueError):
        parse_full_text(deep)


def test_malformed_version_date_raises_value_error() -> None:
    xml = full_text_xml(bloque_xml(versions=(version_xml(effective="2020-01-01"),)))
    with pytest.raises(ValueError):
        parse_full_text(xml)


def test_bloque_without_id_raises_the_same_error_as_parse_block() -> None:
    xml = full_text_xml(bloque_xml(None))
    with pytest.raises(ValueError):
        parse_block(single_block_xml(bloque_xml(None)))
    with pytest.raises(ValueError):
        parse_full_text(xml)


def test_well_formed_body_without_blocks_gives_an_empty_list() -> None:
    assert parse_full_text(full_text_xml()) == []
    assert parse_full_text("<response><data><texto/></data></response>") == []


# --- fixtures: Asturias (new law, not yet in force) ------------------------------------------


@pytest.fixture
def asturias(load_fixture: Loader) -> list[Block]:
    return parse_full_text(load_fixture(ASTURIAS))


def count_bloques(raw: bytes) -> int:
    return len(re.findall(rb"<bloque\b", raw))


def test_asturias_has_one_block_per_bloque_element(
    asturias: list[Block], load_fixture: Loader
) -> None:
    assert len(asturias) == count_bloques(load_fixture(ASTURIAS)) == 26


def test_asturias_order_and_ends(asturias: list[Block]) -> None:
    ids = [b.block_id for b in asturias]
    assert ids[:4] == ["pr", "ti", "ci", "a1"]
    assert ids[-3:] == ["df", "df-4", "fi"]
    assert len(set(ids)) == len(ids)


def test_asturias_block_kinds_and_titles(asturias: list[Block]) -> None:
    kinds = Counter(b.kind for b in asturias)
    assert kinds["preambulo"] == 1
    assert kinds["firma"] == 1
    assert kinds["encabezado"] == 10
    assert kinds["precepto"] == 14
    by_id = {b.block_id: b for b in asturias}
    assert by_id["pr"].title == "[preambulo]"
    assert by_id["fi"].title == "[firma]"
    assert by_id["a1"].title == "Artículo 1"
    assert by_id["df"].title == "Disposición final primera"


def test_asturias_blocks_are_single_version_and_not_expired(asturias: list[Block]) -> None:
    assert all(len(b.versions) == 1 for b in asturias)
    assert all(b.expires_on is None and b.expired_marker is False for b in asturias)


def test_asturias_version_dates_are_in_the_future(asturias: list[Block]) -> None:
    first = asturias[0].versions[0]
    assert first.published_on == date(2026, 10, 23)
    assert first.effective_from == date(2026, 11, 23)


def test_asturias_article_paragraphs(asturias: list[Block]) -> None:
    a1 = next(b for b in asturias if b.block_id == "a1")
    first = a1.versions[0].paragraphs[0]
    assert (first.kind, first.text) == ("articulo", "Artículo 1. Objeto.")


# --- fixtures: Ley 40/2015 -------------------------------------------------------------------


@pytest.fixture
def ley40(load_fixture: Loader) -> list[Block]:
    return parse_full_text(load_fixture(LEY40))


def test_ley40_starts_with_the_initial_note_whose_id_is_co(ley40: list[Block]) -> None:
    first = ley40[0]
    assert (first.block_id, first.kind, first.title) == ("co", "nota_inicial", "")
    assert first.versions[0].paragraphs[0].text.startswith("Incluye la corrección de errores")


def test_ley40_block_count_matches_the_file(ley40: list[Block], load_fixture: Loader) -> None:
    assert len(ley40) == count_bloques(load_fixture(LEY40)) == 24
    assert ley40[-1].kind == "firma"


def test_ley40_keeps_multiple_versions_in_ascending_order(ley40: list[Block]) -> None:
    by_id = {b.block_id: b for b in ley40}
    assert len(by_id["a48"].versions) == 2
    assert len(by_id["a50"].versions) == 3
    dates = [v.starts_on for v in by_id["a50"].versions]
    assert dates == sorted(dates)  # type: ignore[type-var]


def test_ley40_nbsp_in_article_heading_is_collapsed(ley40: list[Block]) -> None:
    a1 = next(b for b in ley40 if b.block_id == "a1")
    assert a1.versions[0].paragraphs[0].text == "Artículo 1. Objeto."


def test_ley40_section_headings_are_single_paragraphs(ley40: list[Block]) -> None:
    s1 = next(b for b in ley40 if b.block_id == "s1")
    assert [(p.kind, p.text) for p in s1.versions[-1].paragraphs] == [
        ("seccion", "Sección 1.ª De los órganos administrativos")
    ]


# --- fixtures: Código Civil (expiry, headings that change, group headings) -------------------


@pytest.fixture
def codigo_civil(load_fixture: Loader) -> dict[str, Block]:
    blocks = parse_full_text(load_fixture(CODIGO_CIVIL))
    return {b.block_id: b for b in blocks}


def test_codigo_civil_block_count_and_first_blocks(
    codigo_civil: dict[str, Block], load_fixture: Loader
) -> None:
    assert len(codigo_civil) == count_bloques(load_fixture(CODIGO_CIVIL)) == 32
    ids = list(codigo_civil)
    assert ids[:3] == ["preambulo", "fi", "codigocivil"]
    assert codigo_civil["preambulo"].kind == "parte_dispositiva"
    assert codigo_civil["preambulo"].title == ""
    assert codigo_civil["fi"].kind == "firma"


def test_codigo_civil_real_expiry_dates(codigo_civil: dict[str, Block]) -> None:
    assert codigo_civil["art307"].expires_on == date(1983, 11, 15)
    assert codigo_civil["sprimera"].expires_on == date(1981, 7, 20)
    assert codigo_civil["cii-4"].expires_on == date(1981, 7, 20)
    assert codigo_civil["disposicionpreliminar"].expires_on == date(2021, 12, 16)
    assert all(codigo_civil[i].expired_marker for i in ("art307", "sprimera", "cii-4"))


def test_codigo_civil_sentinel_block(codigo_civil: dict[str, Block]) -> None:
    sentinel = codigo_civil["art.1873bis"]
    assert sentinel.expires_on is None
    assert sentinel.expired_marker is True
    assert sentinel.title == "Art. 1873 bis"


def test_codigo_civil_blocks_without_the_attribute_are_not_expired(
    codigo_civil: dict[str, Block],
) -> None:
    for block_id in ("a1", "tpreliminar", "df", "da", "cunico"):
        assert codigo_civil[block_id].expires_on is None
        assert codigo_civil[block_id].expired_marker is False


def test_codigo_civil_version_after_the_expiry_is_kept(codigo_civil: dict[str, Block]) -> None:
    # art307: the 1983 version starts on the expiry date itself; both versions are parsed.
    assert len(codigo_civil["art307"].versions) == 2


def test_codigo_civil_heading_text_changes_between_versions(
    codigo_civil: dict[str, Block],
) -> None:
    old, new = codigo_civil["tpreliminar"].versions
    assert [p.text for p in old.paragraphs[:2]] == [
        "TÍTULO PRELIMINAR",
        "De las Leyes, de sus efectos y de las reglas generales para su aplicación",
    ]
    assert [p.text for p in new.paragraphs[:2]] == [
        "TÍTULO PRELIMINAR",
        "De las normas jurídicas, su aplicación y eficacia",
    ]
    assert old.effective_from == date(1889, 8, 16)
    assert new.effective_from == date(1974, 7, 29)


def test_codigo_civil_title_attribute_can_differ_from_paragraph_text(
    codigo_civil: dict[str, Block],
) -> None:
    block = codigo_civil["tprimero"]
    assert block.title == "TÍTULO PRIMERO"
    assert block.versions[0].paragraphs[0].text == "TÍTULO I"


def test_codigo_civil_group_headings_use_the_capitulo_class(
    codigo_civil: dict[str, Block],
) -> None:
    for block_id, text in (("da", "DISPOSICIONES ADICIONALES"), ("df", "DISPOSICIÓN FINAL")):
        paragraphs = codigo_civil[block_id].versions[0].paragraphs
        assert [(p.kind, p.text) for p in paragraphs] == [("capitulo", text)]
    assert codigo_civil["codigocivil"].versions[0].paragraphs[0].kind == "anexo"
