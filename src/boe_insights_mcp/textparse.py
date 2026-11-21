"""Turns the XML of a ``/texto/bloque/{id}`` response into ``Block`` models.

This module is pure: it takes the response body and returns models, with no I/O. ``client`` calls
``parse_block`` on the raw body and maps its ``ValueError`` to a response error; ``versions`` and
the tools work on the resulting models and never see XML. The document shape is described in
``docs/api-notes.md`` section 6.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml.ElementTree import fromstring

from boe_insights_mcp.models import Block, BlockVersion, Paragraph, collapse_whitespace

__all__ = ["parse_block"]

_DATE_FORMAT = re.compile(r"\d{8}")
# Elements that separate words even though they are written inline; every other inline tag
# (``a``, ``strong``, ``sup``...) is glued to its neighbours, so ``m<sup>2</sup>`` is ``m2``.
_SEPARATING_TAGS = frozenset({"br", "p", "div"})
# Real blocks nest at most a few levels (a table cell holds a ``p`` holding an ``a`` holding an
# ``a``). The flattening below recurses once per level, so a hostile document nested thousands
# deep would end in ``RecursionError``; anything beyond this depth is refused instead.
_MAX_DEPTH = 100


def parse_block(xml: bytes | str) -> Block:
    """Parse a ``/texto/bloque/{id}`` response body into a ``Block``.

    Raises ``ValueError`` when the document is not well-formed or unsafe XML, has no ``<bloque>``,
    has a ``<bloque>`` without an ``id``, or carries a malformed date.
    """
    root = _parse_xml(xml)
    bloque = next(root.iter("bloque"), None)
    if bloque is None:
        raise ValueError("no <bloque> element in the response")
    block_id = bloque.get("id")
    if not block_id:
        raise ValueError("<bloque> has no id attribute")
    versions = [_parse_version(version) for version in bloque.findall("version")]
    # Why re-sort: the server lists versions in an order that is usually but not always
    # chronological, and an empty ``fecha_vigencia`` occurs (api-notes 6.1), hence ``starts_on``
    # (which falls back to the publication date). ``sort`` is stable, so ties and fully undated
    # versions keep the server's order; undated ones go first.
    versions.sort(key=lambda version: version.start_key)
    return Block(
        block_id=block_id,
        kind=bloque.get("tipo", ""),
        title=bloque.get("titulo", ""),
        versions=tuple(versions),
    )


def _parse_xml(xml: bytes | str) -> Element:
    """Parse XML safely, turning every parse failure into ``ValueError``."""
    # Why defusedxml: the body comes from a remote server, and the standard parsers can expand
    # entities (the "billion laughs" memory bomb) or load external files. defusedxml refuses
    # entity declarations and external references by raising a ``ValueError`` subclass, while a
    # plain DOCTYPE without entities, which is harmless, is still accepted.
    try:
        root = fromstring(xml)
    except ParseError as error:
        raise ValueError(f"malformed XML: {error}") from error
    _check_depth(root)
    return root


def _check_depth(root: Element) -> None:
    """Raise ``ValueError`` when the tree is nested deeper than ``_MAX_DEPTH`` levels."""
    # Iterative on purpose: the check itself must survive the document it guards against.
    pending = [(root, 1)]
    while pending:
        element, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise ValueError(f"XML nested more than {_MAX_DEPTH} levels deep")
        pending.extend((child, depth + 1) for child in element)


def _parse_date(value: str | None) -> date | None:
    """Parse a ``yyyymmdd`` attribute; blank or missing means no date, anything else raises."""
    if not value:
        return None
    # ``strptime`` alone would accept short forms such as ``2020101``; require eight digits.
    if not _DATE_FORMAT.fullmatch(value):
        raise ValueError(f"malformed date {value!r}, expected yyyymmdd")
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_version(element: Element) -> BlockVersion:
    """Build a ``BlockVersion`` from a ``<version>`` element, reading attributes by name."""
    # By name, never by position: some versions carry an extra ``fpub`` attribute (api-notes 6).
    paragraphs = [p for child in element for p in _paragraphs(child, quoted=False)]
    return BlockVersion(
        source_law_id=element.get("id_norma", ""),
        published_on=_parse_date(element.get("fecha_publicacion")),
        effective_from=_parse_date(element.get("fecha_vigencia")),
        paragraphs=tuple(paragraphs),
    )


def _paragraphs(element: Element, *, quoted: bool) -> Iterator[Paragraph]:
    """Yield the paragraphs of one child of ``<version>`` or ``<blockquote>``; none when empty."""
    tag = element.tag
    if tag == "blockquote":
        yield from _blockquote_paragraphs(element)
        return
    if tag == "p":
        kind, text = element.get("class", ""), _flatten(element)
    elif tag == "table":
        kind, text = "table", _table_text(element)
    else:
        # Never drop text: an element we do not know still becomes a paragraph, labelled by its
        # tag. The ``class`` is ignored here because it only has a meaning on ``<p>``.
        kind, text = str(tag), _flatten(element)
    if text:
        yield Paragraph(kind=kind, text=text, quoted=quoted)


def _blockquote_paragraphs(element: Element) -> Iterator[Paragraph]:
    """Yield the quoted paragraphs of a ``<blockquote>`` (footnotes and citations)."""
    if len(element) == 0:
        # Bare text without a ``<p>`` was not seen in any fixture; handled only so that no text
        # is lost (api-notes 6.1).
        text = _flatten(element)
        if text:
            yield Paragraph(kind="blockquote", text=text, quoted=True)
        return
    for child in element:
        yield from _paragraphs(child, quoted=True)


def _table_text(table: Element) -> str:
    """Render a table as one line per non-empty row, with the cells joined by `` | ``."""
    lines: list[str] = []
    for row in table.iter("tr"):
        cells = [_flatten(cell) for cell in row if cell.tag in ("td", "th")]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _flatten(element: Element) -> str:
    """The plain text of an element: inline tags flattened, whitespace collapsed, stripped."""
    return collapse_whitespace("".join(_raw_text(element)))


def _raw_text(element: Element) -> Iterator[str]:
    """Yield the text fragments of an element in document order, including children's tails."""
    # ``itertext`` cannot be used because ``<br/>`` and block children must act as word
    # separators; whitespace is collapsed afterwards by the caller. The entity decoding and the
    # NBSP handling are done by the parser and by ``collapse_whitespace`` respectively.
    if element.text:
        yield element.text
    for child in element:
        separator = " " if child.tag in _SEPARATING_TAGS else ""
        yield separator
        yield from _raw_text(child)
        yield separator
        if child.tail:
            yield child.tail
