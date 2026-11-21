"""Rule not written in the spec but seen in real data: blocks that stand for a range of articles.

The Código Civil has blocks whose ``articulo`` paragraph reads ``Arts. 127 a 130.`` (api-notes,
third probe, "Text structure"). ``test_structure`` is the spec's acceptance suite and is left as it
is; this file pins the extension.
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
    ],
)
def test_abbreviated_articles_are_articles_split_after_their_designation(
    text: str, label: str, heading: str
) -> None:
    (entry,) = build_structure([precept(text)], date(2026, 11, 21))

    assert (entry.kind, entry.label, entry.heading) == ("article", label, heading)
