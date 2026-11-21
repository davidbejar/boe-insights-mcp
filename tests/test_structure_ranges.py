"""Rules seen in real data: article ranges and headings typed as articles.

The Código Civil has blocks whose ``articulo`` paragraph reads ``Arts. 127 a 130.`` (api-notes,
third probe, "Text structure"). ``test_structure`` covers the general rules; this file pins these
extensions.
"""

from __future__ import annotations

from datetime import date

import pytest

from boe_insights_mcp.models import Block, BlockVersion, Paragraph
from boe_insights_mcp.structure import build_structure


def precept(text: str) -> Block:
    version = BlockVersion(
        source_law_id="BOE-A-1889-4763",
        published_on=date(1889, 1, 1),
        effective_from=date(1889, 1, 1),
        paragraphs=(Paragraph(kind="articulo", text=text),),
    )
    return Block(block_id="x", kind="precepto", title="Arts", versions=(version,))


@pytest.mark.parametrize(
    ("text", "label", "heading"),
    [
        ("Arts. 127 a 130.", "Arts. 127 a 130", ""),
        ("Arts. 1863 bis a 1873 bis. (Derogados)", "Arts. 1863 bis a 1873 bis", "(Derogados)"),
        ("Art. 1873 bis. Algo.", "Art. 1873 bis", "Algo"),
        ("Arts 325 a 332.", "Arts 325 a 332", ""),  # no period after the abbreviation
        ("Art 94 bis. Algo.", "Art 94 bis", "Algo"),
    ],
)
def test_abbreviated_articles_are_articles_split_after_their_designation(
    text: str, label: str, heading: str
) -> None:
    (entry,) = build_structure([precept(text)], date(2026, 11, 21))

    assert (entry.kind, entry.label, entry.heading) == ("article", label, heading)


def block(block_id: str, *paragraphs: Paragraph) -> Block:
    version = BlockVersion(
        source_law_id="BOE-A-1889-4763",
        published_on=date(1889, 1, 1),
        effective_from=date(1889, 1, 1),
        paragraphs=paragraphs,
    )
    return Block(block_id=block_id, kind="precepto", title="", versions=(version,))


def test_a_precepto_with_only_a_section_paragraph_is_a_section_heading() -> None:
    # ``sprimera-2`` of the Código Civil: typed ``precepto``, written like a heading.
    blocks = [
        block(
            "tx",
            Paragraph(kind="titulo_num", text="TÍTULO X"),
            Paragraph(kind="titulo_tit", text="De la tutela"),
        ),
        block("sprimera-2", Paragraph(kind="seccion", text="Sección 1.ª Disposiciones generales")),
        block("a1", Paragraph(kind="articulo", text="Artículo 1. Objeto.")),
    ]

    entries = build_structure(blocks, date(2026, 11, 21))

    assert [(e.kind, e.level, e.label) for e in entries] == [
        ("title", 1, "TÍTULO X"),
        ("section", 2, "Sección 1.ª"),
        ("article", 3, "Artículo 1"),
    ]
    assert entries[0].heading == "De la tutela"


@pytest.mark.parametrize("kind", ["capitulo", "capitulo_num", "libro_num"])
def test_other_heading_paragraphs_make_a_precepto_a_heading_too(kind: str) -> None:
    (entry,) = build_structure(
        [block("h", Paragraph(kind=kind, text="CAPÍTULO ÚNICO"))], date(2026, 11, 21)
    )

    assert entry.kind in {"chapter", "book"}


def test_a_precepto_with_an_articulo_paragraph_stays_an_article() -> None:
    mixed = block(
        "a1",
        Paragraph(kind="seccion", text="Sección 1.ª"),
        Paragraph(kind="articulo", text="Artículo 1. Objeto."),
    )

    (entry,) = build_structure([mixed], date(2026, 11, 21))

    assert entry.kind == "article"
