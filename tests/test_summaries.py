"""Tests of ``summaries.parse_summary`` over recorded XML bodies of several eras.

The fixtures are real ``Accept: application/xml`` responses with whole elements removed
(``tests/fixtures/boe/README.md``). Small hand-written documents cover the cases no real day shows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from boe_insights_mcp.models import DailySummary
from boe_insights_mcp.summaries import parse_summary

Loader = Callable[[str], bytes]


def summary(load_fixture: Loader, name: str, day: date) -> DailySummary:
    return parse_summary(load_fixture(name), day)


def wrap(*issues: str) -> str:
    """A minimal response body around the given ``<diario>`` elements."""
    return f"<response><data><sumario>{''.join(issues)}</sumario></data></response>"


def issue(inner: str, number: str = "1", identifier: str = "BOE-S-2026-1") -> str:
    return (
        f'<diario numero="{number}"><sumario_diario><identificador>{identifier}</identificador>'
        f"</sumario_diario>{inner}</diario>"
    )


def item(identifier: str = "BOE-A-2026-1", title: str = "T") -> str:
    return (
        f"<item><identificador>{identifier}</identificador><titulo>{title}</titulo>"
        '<url_pdf szBytes="1" pagina_inicial="5" pagina_final="7">https://x/p.pdf</url_pdf>'
        "<url_html>https://x/h</url_html></item>"
    )


# --- real days ---------------------------------------------------------------------------------


def test_two_issues_are_kept_in_the_order_the_api_gives_them(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_20200314_trimmed.xml", date(2020, 3, 14))

    assert result.day == date(2020, 3, 14)
    assert [(i.number, i.identifier) for i in result.issues] == [
        ("67", "BOE-S-2020-67"),
        ("66", "BOE-S-2020-66"),
    ]
    first = result.items[0]
    assert first.identifier == "BOE-A-2020-3692"
    assert first.issue_number == "67"
    assert first.title.startswith("Real Decreto 463/2020, de 14 de marzo")
    assert first.section_code == "1"
    assert first.department_code == "9574"
    assert {item.issue_number for item in result.items} == {"67", "66"}
    assert [item.issue_number for item in result.items].count("67") == 1


def test_every_field_of_an_item_with_an_epigraph_is_read(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_20200610_trimmed.xml", date(2020, 6, 10))

    rdl = result.items[0]
    assert rdl.identifier == "BOE-A-2020-5895"
    assert rdl.section_code == "1"
    assert rdl.section_name == "I. Disposiciones generales"
    assert (rdl.department_code, rdl.department_name) == ("7723", "JEFATURA DEL ESTADO")
    assert rdl.heading is not None
    assert (rdl.first_page, rdl.last_page) == (38723, 38752)
    assert rdl.pdf_url is not None and rdl.pdf_url.endswith(".pdf")
    assert rdl.html_url == "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2020-5895"


def test_sections_t_and_5c_and_items_directly_under_a_department(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_20200610_trimmed.xml", date(2020, 6, 10))

    codes = {item.section_code for item in result.items}
    assert {"T", "5C", "1", "2A"} <= codes
    constitutional = [i for i in result.items if i.section_code == "T"]
    assert constitutional[0].department_name == "TRIBUNAL CONSTITUCIONAL"
    assert constitutional[0].heading == "Sentencias"
    assert any(i.heading is None for i in result.items)  # directly under a department
    assert any(i.heading is not None for i in result.items)  # under an epigraph


def test_1960_departments_without_code_or_name_give_none(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_19600901_trimmed.xml", date(1960, 9, 1))

    unnamed = [i for i in result.items if i.department_code is None]
    assert unnamed
    assert all(i.department_name is None for i in unnamed)
    assert result.issues[0].identifier == "BOE-S-1960-210"
    assert "5" in {i.section_code for i in result.items}


def test_items_without_html_link_have_none(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_20000614_trimmed.xml", date(2000, 6, 14))

    without = [i for i in result.items if i.html_url is None]
    assert without
    assert all(i.pdf_url and i.first_page is not None for i in without)
    assert any(i.html_url for i in result.items)


def test_the_constitution_day_has_a_single_item(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_19781229_constitution.xml", date(1978, 12, 29))

    assert [i.identifier for i in result.items] == ["BOE-A-1978-31229"]
    assert result.items[0].title == "Constitución Española."
    assert result.items[0].department_name == "CORTES GENERALES"


def test_a_recent_ordinary_day_keeps_document_order(load_fixture: Loader) -> None:
    result = summary(load_fixture, "sumario_boe_20261120_trimmed.xml", date(2026, 11, 20))

    assert [i.number for i in result.issues] == ["289"]
    sections = [i.section_code for i in result.items]
    # Sections come in the order of the document, not sorted.
    assert sections == sorted(
        sections, key=lambda code: ["1", "2A", "2B", "3", "5A", "5B", "5C"].index(code)
    )
    assert result.items[0].identifier.startswith("BOE-A-2026-")
    assert all(i.title == " ".join(i.title.split()) for i in result.items)


# --- constructed documents ---------------------------------------------------------------------


def test_whitespace_in_a_title_is_collapsed_and_text_is_otherwise_untouched() -> None:
    body = wrap(
        issue(
            '<seccion codigo="1" nombre="I"><departamento codigo="1" nombre="D">'
            + item(title="  Orden\n   de  1\u00a0de  enero, «ñ»  ")
            + "</departamento></seccion>"
        )
    )

    result = parse_summary(body, date(2026, 1, 1))

    assert result.items[0].title == "Orden de 1 de enero, «ñ»"


def test_a_department_with_only_text_gives_nothing_and_unknown_elements_are_ignored() -> None:
    body = wrap(
        issue(
            '<seccion codigo="5C" nombre="V"><departamento codigo="9" nombre="D">'
            "<texto>plain</texto><mystery/></departamento>"
            '<departamento codigo="8" nombre="E">' + item() + "<novedad>x</novedad>"
            "</departamento></seccion><otra/>"
        )
    )

    result = parse_summary(body, date(2026, 1, 1))

    assert [i.identifier for i in result.items] == ["BOE-A-2026-1"]


def test_items_without_identifier_or_title_are_skipped() -> None:
    no_id = "<item><titulo>Sin id</titulo></item>"
    no_title = "<item><identificador>BOE-A-2026-2</identificador><titulo>  </titulo></item>"
    body = wrap(
        issue(
            '<seccion codigo="1" nombre="I"><departamento codigo="1" nombre="D">'
            + no_id
            + no_title
            + item("BOE-A-2026-3")
            + "</departamento></seccion>"
        )
    )

    assert [i.identifier for i in parse_summary(body, date(2026, 1, 1)).items] == ["BOE-A-2026-3"]


def test_epigraph_and_direct_items_keep_their_document_order() -> None:
    body = wrap(
        issue(
            '<seccion codigo="1" nombre="I"><departamento codigo="1" nombre="D">'
            + item("BOE-A-2026-1")
            + '<epigrafe nombre="E1">'
            + item("BOE-A-2026-2")
            + "</epigrafe>"
            + item("BOE-A-2026-3")
            + "</departamento></seccion>"
        )
    )

    result = parse_summary(body, date(2026, 1, 1))

    assert [(i.identifier, i.heading) for i in result.items] == [
        ("BOE-A-2026-1", None),
        ("BOE-A-2026-2", "E1"),
        ("BOE-A-2026-3", None),
    ]


def test_missing_pages_and_urls_are_none() -> None:
    bare = "<item><identificador>BOE-A-2026-1</identificador><titulo>T</titulo></item>"
    body = wrap(
        issue(
            '<seccion codigo="1" nombre="I"><departamento codigo="1" nombre="D">'
            + bare
            + "</departamento></seccion>"
        )
    )

    only = parse_summary(body, date(2026, 1, 1)).items[0]

    assert (only.first_page, only.last_page, only.pdf_url, only.html_url) == (
        None,
        None,
        None,
        None,
    )


def test_an_issue_without_items_is_still_an_issue() -> None:
    result = parse_summary(wrap(issue("")), date(2026, 1, 1))

    assert len(result.issues) == 1
    assert result.items == ()


@pytest.mark.parametrize(
    "body",
    [
        "<response><data><sumario/></data></response>",
        "<response><status><code>200</code></status></response>",
    ],
)
def test_a_body_without_an_issue_is_refused(body: str) -> None:
    with pytest.raises(ValueError, match="no issue"):
        parse_summary(body, date(2026, 1, 1))


@pytest.mark.parametrize(
    "body",
    [
        "not xml at all",
        '<!DOCTYPE r [<!ENTITY a "x">]><response>&a;</response>',
        "<response>" + "<a>" * 150 + "</a>" * 150 + "</response>",
    ],
)
def test_malformed_unsafe_or_too_deep_xml_is_refused(body: str) -> None:
    with pytest.raises(ValueError):
        parse_summary(body, date(2026, 1, 1))
