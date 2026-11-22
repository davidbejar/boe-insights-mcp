"""Tests of ``naming``: slugs and proposed names for saved searches (pure functions)."""

from __future__ import annotations

import pytest

from boe_insights_mcp.naming import (
    MAX_NAME,
    gazette_base,
    law_base,
    propose_name,
    slugify,
)

# --- slugify -----------------------------------------------------------------------------------


def test_max_name_is_sixty() -> None:
    assert MAX_NAME == 60


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Vivienda", "vivienda"),
        ("Subvenciones a la Vivienda", "subvenciones-a-la-vivienda"),
        ("Ñandú Über", "nandu-uber"),
        ("Protección de datos: año 2024", "proteccion-de-datos-ano-2024"),
        ("Ley 40/2015", "ley-40-2015"),
        ("a   b\t\nc", "a-b-c"),
        ("a---b__c..d", "a-b-c-d"),
        ("  --hola--  ", "hola"),
        ("ABC", "abc"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "!!!", "---", "¿?"])
def test_slugify_may_return_an_empty_string(text: str) -> None:
    assert slugify(text) == ""


def test_slugify_cuts_to_the_maximum_length() -> None:
    assert slugify("a" * 100) == "a" * MAX_NAME


def test_slugify_does_not_leave_a_trailing_dash_after_the_cut() -> None:
    # The 60th character of the full slug is the dash.
    text = "a" * (MAX_NAME - 1) + " " + "b" * 10

    assert slugify(text) == "a" * (MAX_NAME - 1)


def test_slugify_keeps_a_dash_inside_the_cut() -> None:
    text = "a" * (MAX_NAME - 3) + " " + "b" * 10

    assert slugify(text) == "a" * (MAX_NAME - 3) + "-bb"


# --- propose_name ------------------------------------------------------------------------------


def test_a_free_base_is_kept() -> None:
    assert propose_name("vivienda", set()) == "vivienda"
    assert propose_name("vivienda", {"otra"}) == "vivienda"


def test_a_taken_base_gets_the_first_free_suffix() -> None:
    assert propose_name("vivienda", {"vivienda"}) == "vivienda-2"
    assert propose_name("vivienda", {"vivienda", "vivienda-2"}) == "vivienda-3"


def test_a_free_suffix_is_found_even_with_gaps() -> None:
    assert propose_name("x", {"x", "x-3"}) == "x-2"


def test_taken_may_be_any_collection() -> None:
    assert propose_name("x", frozenset({"x"})) == "x-2"
    assert propose_name("x", ["x", "x-2"]) == "x-3"


def test_an_empty_base_is_treated_as_search() -> None:
    assert propose_name("", set()) == "search"
    assert propose_name("", {"search"}) == "search-2"


def test_a_long_base_is_shortened_to_fit_the_suffix() -> None:
    base = "a" * MAX_NAME

    assert propose_name(base, set()) == base
    result = propose_name(base, {base})
    assert result == "a" * (MAX_NAME - 2) + "-2"
    assert len(result) == MAX_NAME


def test_a_long_base_is_shortened_further_for_longer_suffixes() -> None:
    base = "a" * MAX_NAME
    taken = {base, *(("a" * (MAX_NAME - 2)) + f"-{n}" for n in range(2, 10))}

    result = propose_name(base, taken)

    assert result == "a" * (MAX_NAME - 3) + "-10"
    assert len(result) == MAX_NAME


# --- gazette_base ------------------------------------------------------------------------------


def test_gazette_base_from_words() -> None:
    assert gazette_base("Vivienda subvenciones", None, None) == "vivienda-subvenciones"


def test_gazette_base_with_a_section() -> None:
    assert gazette_base("vivienda", "3", None) == "vivienda-seccion-3"
    assert gazette_base(None, "2A", None) == "seccion-2a"


def test_gazette_base_with_a_department() -> None:
    assert gazette_base(None, None, "8161") == "dept-8161"


def test_gazette_base_joins_all_three_in_order() -> None:
    assert gazette_base("vivienda", "3", "8161") == "vivienda-seccion-3-dept-8161"


def test_gazette_base_with_nothing_is_empty() -> None:
    assert gazette_base(None, None, None) == ""


def test_gazette_base_respects_the_length_limit() -> None:
    result = gazette_base("palabra " * 30, "3", "8161")

    assert len(result) <= MAX_NAME
    assert not result.endswith("-")
    assert result.startswith("palabra-palabra")


# --- law_base ----------------------------------------------------------------------------------


def test_law_base_uses_rank_and_number_when_both_are_known() -> None:
    assert law_base("Ley", "40/2015", "BOE-A-2015-10566") == "ley-40-2015"
    assert law_base("Real Decreto", "1/2020", "BOE-A-2020-1") == "real-decreto-1-2020"


@pytest.mark.parametrize(("rank", "number"), [(None, "40/2015"), ("Ley", None), (None, None)])
def test_law_base_falls_back_to_the_identifier(rank: str | None, number: str | None) -> None:
    assert law_base(rank, number, "BOE-A-2015-10566") == "boe-a-2015-10566"
