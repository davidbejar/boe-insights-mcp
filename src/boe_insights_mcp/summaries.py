"""Turns the XML of a ``/boe/sumario/{yyyymmdd}`` response into a ``DailySummary``.

This module is pure: it takes the response body and returns models, with no I/O. ``client`` calls
``parse_summary`` on the raw body and maps its ``ValueError`` to a response error; ``gazette`` and
the tools work on the resulting models and never see XML. The document shape is described in
``docs/api-notes.md`` section 8 and "Verified after the fourth probe". It shares its safe XML
parsing with ``textparse`` through ``xmlsafe``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from xml.etree.ElementTree import Element

from boe_insights_mcp.models import (
    DailySummary,
    GazetteIssue,
    GazetteItem,
    collapse_whitespace,
    strip_or_none,
)
from boe_insights_mcp.xmlsafe import parse_xml

__all__ = ["parse_summary"]


def parse_summary(xml: bytes | str, day: date) -> DailySummary:
    """Parse a ``/boe/sumario/{yyyymmdd}`` response body into a ``DailySummary`` for ``day``.

    ``day`` is passed in rather than read from the body so that the result does not depend on a
    field the API could change. Items without an identifier or a title are skipped, unknown
    elements are ignored, and a department that carries only text yields nothing. Raises
    ``ValueError`` when the XML is malformed or unsafe, or when it contains no issue.
    """
    # Why XML and not JSON (api-notes, fourth probe): the XML is about 45% smaller, repeated
    # elements stay repeated where JSON collapses a single element into an object, and it has no
    # ``texto`` wrapper nodes, which appear in the JSON under a rule nobody has found. The XML
    # of eight days from 1960 to 2026 was compared with the JSON and holds the same items and
    # the same fields.
    root = parse_xml(xml)
    diarios = list(root.iter("diario"))
    if not diarios:
        raise ValueError("no issue (<diario>) in the response")
    issues: list[GazetteIssue] = []
    items: list[GazetteItem] = []
    for diario in diarios:
        number = diario.get("numero", "")
        issues.append(GazetteIssue(number=number, identifier=_issue_identifier(diario)))
        # Items are collected issue by issue and keep document order. Two issues can share a day
        # (2020-03-14: an extraordinary one listed before the ordinary one, api-notes fourth
        # probe), and the same section code then appears once per issue, so the issue number
        # travels with every item instead of being implied by position.
        items.extend(_issue_items(diario, number))
    return DailySummary(day=day, issues=tuple(issues), items=tuple(items))


def _issue_identifier(diario: Element) -> str:
    """The ``BOE-S-...`` identifier of an issue, ``""`` when the element is missing."""
    return (diario.findtext("sumario_diario/identificador") or "").strip()


def _issue_items(diario: Element, number: str) -> Iterator[GazetteItem]:
    """Yield the valid items of one issue in document order."""
    for seccion in diario.findall("seccion"):
        section_code = seccion.get("codigo", "")
        section_name = seccion.get("nombre", "")
        for departamento in seccion.findall("departamento"):
            # An empty code or name is how the 1960 announcements arrive; it means "none".
            code = strip_or_none(departamento.get("codigo"))
            name = strip_or_none(departamento.get("nombre"))
            # Items sit directly under the department or under an epigrafe (section 5A/5B items
            # have no epigrafe; section 1 usually has). Walking the children once keeps the two
            # kinds in the order of the document. Anything else (text, unknown tags) is ignored.
            for child in departamento:
                if child.tag == "item":
                    heading = None
                    candidates = [child]
                elif child.tag == "epigrafe":
                    heading = strip_or_none(child.get("nombre"))
                    candidates = child.findall("item")
                else:
                    continue
                for element in candidates:
                    parsed = _parse_item(
                        element, number, section_code, section_name, code, name, heading
                    )
                    if parsed is not None:
                        yield parsed


def _parse_item(
    element: Element,
    issue_number: str,
    section_code: str,
    section_name: str,
    department_code: str | None,
    department_name: str | None,
    heading: str | None,
) -> GazetteItem | None:
    """Build one ``GazetteItem``; ``None`` when it lacks an identifier or a title."""
    identifier = (element.findtext("identificador") or "").strip()
    title_element = element.find("titulo")
    title = (
        collapse_whitespace("".join(title_element.itertext())) if title_element is not None else ""
    )
    if not identifier or not title:
        return None
    pdf = element.find("url_pdf")
    return GazetteItem(
        identifier=identifier,
        title=title,
        section_code=section_code,
        section_name=section_name,
        department_code=department_code,
        department_name=department_name,
        heading=heading,
        issue_number=issue_number,
        first_page=_page(pdf, "pagina_inicial"),
        last_page=_page(pdf, "pagina_final"),
        pdf_url=strip_or_none(pdf.text) if pdf is not None else None,
        # ``url_html`` is absent on some announcements before 2009 (api-notes, fourth probe).
        html_url=strip_or_none(element.findtext("url_html")),
    )


def _page(pdf: Element | None, attribute: str) -> int | None:
    """A page number attribute of ``<url_pdf>``; ``None`` when absent or not a number."""
    value = pdf.get(attribute) if pdf is not None else None
    # Tolerant on purpose: a page number is informational and must not make a day unreadable.
    return int(value) if value is not None and value.isdigit() else None
