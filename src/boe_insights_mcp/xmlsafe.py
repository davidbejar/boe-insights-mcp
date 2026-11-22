"""Safe parsing of XML bodies received from the BOE, shared by the XML parsers.

``textparse`` (law text) and ``summaries`` (daily summaries) both read XML from a remote server
we do not control, so both go through :func:`parse_xml` and get the same hardening. This module
is pure and has no neighbours except the parsers that import it.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, ParseError

from defusedxml.ElementTree import fromstring

__all__ = ["MAX_DEPTH", "check_depth", "parse_xml"]

# Real documents nest at most a few levels (a table cell holds a ``p`` holding an ``a`` holding an
# ``a``). Parsers may recurse once per level, so a hostile document nested thousands deep would
# end in ``RecursionError``; anything beyond this depth is refused instead.
MAX_DEPTH = 100


def parse_xml(xml: bytes | str) -> Element:
    """Parse XML safely, turning every parse failure into ``ValueError``."""
    # Why defusedxml: the body comes from a remote server, and the standard parsers can expand
    # entities (the "billion laughs" memory bomb) or load external files. defusedxml refuses
    # entity declarations and external references by raising a ``ValueError`` subclass, while a
    # plain DOCTYPE without entities, which is harmless, is still accepted.
    try:
        root = fromstring(xml)
    except ParseError as error:
        raise ValueError(f"malformed XML: {error}") from error
    check_depth(root)
    return root


def check_depth(root: Element) -> None:
    """Raise ``ValueError`` when the tree is nested deeper than ``MAX_DEPTH`` levels."""
    # Iterative on purpose: the check itself must survive the document it guards against.
    pending = [(root, 1)]
    while pending:
        element, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError(f"XML nested more than {MAX_DEPTH} levels deep")
        pending.extend((child, depth + 1) for child in element)
