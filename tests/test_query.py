"""Contract tests for ``boe_insights_mcp.query.build_search_query``."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import pytest

from boe_insights_mcp.query import build_search_query

LOWER_BOUND = "18000101"
UPPER_BOUND = "99991231"
INJECTIONS = [
    "foo) OR rango@codigo:1300 (",
    'say "hello',
    'a" OR rango@codigo:1300 OR "b',
    "foo:bar",
    "titulo:x AND NOT texto:y",
    "back\\slash\\",
    "\\",
    ")(",
    "wild* card? [x TO y] {a} ^2 ~3 +must !not",
    "a && b || c",
    "/regex.*/",
]


def build(**kwargs: Any) -> dict[str, Any]:
    """Build a query and parse it; the call must produce a JSON document."""
    result = build_search_query(**kwargs)
    assert result is not None
    return json.loads(result)


def query_string(**kwargs: Any) -> str:
    return build(sort_by=None, **kwargs)["query"]["query_string"]["query"]


def clauses(**kwargs: Any) -> set[str]:
    """Clauses of a query made only of simple, space-free parts."""
    return set(query_string(**kwargs).split(" AND "))


def unprotected(expression: str) -> str:
    """Return ``expression`` without quoted phrases and backslash-escaped characters.

    What remains is what the query parser would interpret as syntax. Fails the test when a
    quote is left open, because that would swallow the following clauses.
    """
    kept: list[str] = []
    in_quote = False
    chars = iter(expression)
    for char in chars:
        if char == "\\":
            next(chars, "")
        elif char == '"':
            in_quote = not in_quote
        elif not in_quote:
            kept.append(char)
    assert not in_quote, f"unbalanced quote in {expression!r}"
    return "".join(kept)


def assert_only_fields(expression: str, fields: list[str]) -> None:
    """Assert the user's words created no syntax beyond the expected field clauses."""
    syntax = unprotected(expression)
    assert syntax.count(":") == len(fields)
    for field in fields:
        assert f"{field}:" in syntax
    assert syntax.count("(") == syntax.count(")")
    assert not re.search(r"[\\+!{}\[\]^~*?/]", syntax)
    assert "&&" not in syntax
    assert "||" not in syntax
    assert not re.search(r"\b(OR|NOT)\b", syntax)


# --- shape -----------------------------------------------------------------------------------


def test_no_arguments_gives_only_the_default_sort() -> None:
    assert build() == {"sort": [{"fecha_publicacion": "desc"}]}


def test_nothing_requested_and_no_sort_returns_none() -> None:
    assert build_search_query(sort_by=None) is None


def test_result_is_a_json_string() -> None:
    result = build_search_query(rank_code="1300")
    assert isinstance(result, str)
    assert isinstance(json.loads(result), dict)


def test_arguments_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_search_query("energía")  # type: ignore[misc]


def test_criteria_without_dates_have_no_range() -> None:
    assert "range" not in build(rank_code="1300")["query"]


def test_dates_without_text_criteria_have_no_query_string() -> None:
    data = build(published_from=date(2020, 1, 1), published_to=date(2020, 12, 31))
    assert set(data["query"]) == {"range"}


def test_full_query_has_query_string_range_and_sort() -> None:
    data = build(rank_code="1300", published_from=date(2020, 1, 1), published_to=date(2020, 12, 31))
    assert set(data) == {"query", "sort"}
    assert set(data["query"]) == {"query_string", "range"}
    assert set(data["query"]["query_string"]) == {"query"}


def test_output_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "title": "energía renovable",
        "text": "autoconsumo",
        "official_number": "40/2015",
        "rank_code": "1300",
        "department_code": "7723",
        "scope_code": "1",
        "subject_code": "6936",
        "published_from": date(2020, 1, 1),
        "published_to": date(2020, 12, 31),
    }
    assert build_search_query(**kwargs) == build_search_query(**kwargs)


def test_key_order_is_stable() -> None:
    data = build(rank_code="1300", published_from=date(2020, 1, 1))
    assert list(data) == ["query", "sort"]
    assert list(data["query"]) == ["query_string", "range"]


# --- single parameters -------------------------------------------------------------------------


def test_official_number_is_a_quoted_phrase() -> None:
    assert query_string(official_number="40/2015") == 'numero_oficial:"40/2015"'


def test_official_number_with_letters_is_kept_verbatim() -> None:
    assert query_string(official_number="APA/315/2020") == 'numero_oficial:"APA/315/2020"'


@pytest.mark.parametrize(
    ("argument", "field"),
    [
        ("rank_code", "rango@codigo"),
        ("department_code", "departamento@codigo"),
        ("scope_code", "ambito@codigo"),
        ("subject_code", "materia@codigo"),
    ],
)
def test_code_parameters_use_their_field(argument: str, field: str) -> None:
    assert query_string(**{argument: "1300"}) == f"{field}:1300"


@pytest.mark.parametrize("code", ["1300", "ab-C_9", "A", "007"])
def test_valid_codes_are_accepted(code: str) -> None:
    assert query_string(rank_code=code) == f"rango@codigo:{code}"


def test_title_word_targets_the_title_field() -> None:
    expression = query_string(title="energía")
    assert_only_fields(expression, ["titulo"])
    assert "energía" in expression


def test_text_word_targets_the_body_field() -> None:
    expression = query_string(text="autoconsumo")
    assert_only_fields(expression, ["texto"])
    assert "autoconsumo" in expression


def test_title_and_text_together_use_both_fields() -> None:
    expression = query_string(title="energía", text="autoconsumo")
    assert_only_fields(expression, ["titulo", "texto"])
    assert "energía" in expression
    assert "autoconsumo" in expression


def test_surrounding_whitespace_of_title_is_ignored() -> None:
    assert query_string(title="  energía  ") == query_string(title="energía")


# --- title / text words and phrases -----------------------------------------------------------


@pytest.mark.parametrize("argument", ["title", "text"])
def test_multiple_words_must_all_match(argument: str) -> None:
    expression = query_string(**{argument: "energía renovable marina"})
    syntax = unprotected(expression)
    assert len(re.findall(r"\bAND\b", syntax)) == 2
    for word in ("energía", "renovable", "marina"):
        assert word in expression


def test_extra_whitespace_between_words_is_collapsed() -> None:
    assert query_string(title="energía   renovable\t marina") == query_string(
        title="energía renovable marina"
    )


@pytest.mark.parametrize("argument", ["title", "text"])
def test_double_quoted_phrase_is_kept_as_a_phrase(argument: str) -> None:
    expression = query_string(**{argument: '"ley del suelo"'})
    assert '"ley del suelo"' in expression
    assert not re.search(r"\b(ley|del|suelo)\b", unprotected(expression))


def test_phrase_and_words_can_be_mixed() -> None:
    expression = query_string(title='energía "ley del suelo"')
    assert '"ley del suelo"' in expression
    assert "energía" in expression
    assert len(re.findall(r"\bAND\b", unprotected(expression))) == 1


@pytest.mark.parametrize("blank", ["", " ", "   \t\n"])
@pytest.mark.parametrize("argument", ["title", "text"])
def test_blank_title_or_text_is_treated_as_none(argument: str, blank: str) -> None:
    kwargs: dict[str, Any] = {argument: blank}
    assert build_search_query(**kwargs, sort_by=None) is None


def test_blank_title_does_not_disturb_other_clauses() -> None:
    assert query_string(title="  ", rank_code="1300") == "rango@codigo:1300"


@pytest.mark.parametrize("hostile", INJECTIONS)
def test_hostile_title_creates_no_extra_syntax(hostile: str) -> None:
    assert_only_fields(query_string(title=hostile), ["titulo"])


@pytest.mark.parametrize("hostile", INJECTIONS)
def test_hostile_text_creates_no_extra_syntax(hostile: str) -> None:
    assert_only_fields(query_string(text=hostile), ["texto"])


def test_injected_rank_clause_does_not_add_a_rank_filter() -> None:
    expression = query_string(title="foo) OR rango@codigo:1300 (", rank_code="1290")
    assert_only_fields(expression, ["titulo", "rango@codigo"])
    assert "rango@codigo:1290" in unprotected(expression)
    assert "rango@codigo:1300" not in unprotected(expression)


def test_hostile_title_keeps_hostile_words_searchable() -> None:
    expression = query_string(title="foo) OR rango@codigo:1300 (")
    assert "foo" in expression


def test_operator_words_in_title_are_not_operators() -> None:
    expression = query_string(title="pan OR vino NOT agua")
    assert_only_fields(expression, ["titulo"])
    for word in ("pan", "vino", "agua"):
        assert word in expression


def test_hostile_official_number_is_rejected_or_neutralised() -> None:
    try:
        result = build_search_query(
            official_number='1/2000" OR rango@codigo:1300 OR "', sort_by=None
        )
    except ValueError:
        return
    assert result is not None
    assert_only_fields(json.loads(result)["query"]["query_string"]["query"], ["numero_oficial"])


# --- codes -------------------------------------------------------------------------------------


@pytest.mark.parametrize("argument", ["rank_code", "department_code", "scope_code", "subject_code"])
@pytest.mark.parametrize(
    "code",
    [
        "1300 OR rango@codigo:1290",
        "13 00",
        "13:00",
        "1300)",
        '13"00',
        "13*",
        "13\\0",
        "ñ1",
        "a@b",
    ],
)
def test_invalid_codes_raise_value_error(argument: str, code: str) -> None:
    with pytest.raises(ValueError):
        kwargs: dict[str, Any] = {argument: code}
        build_search_query(**kwargs)


# --- dates -------------------------------------------------------------------------------------


def test_closed_date_range_uses_both_bounds() -> None:
    data = build(published_from=date(2020, 1, 1), published_to=date(2020, 12, 31))
    assert data["query"]["range"] == {"fecha_publicacion": {"gte": "20200101", "lte": "20201231"}}


def test_lower_bound_only_gets_an_upper_bound() -> None:
    data = build(published_from=date(2020, 5, 17))
    assert data["query"]["range"] == {"fecha_publicacion": {"gte": "20200517", "lte": UPPER_BOUND}}


def test_upper_bound_only_gets_a_lower_bound() -> None:
    data = build(published_to=date(2020, 5, 17))
    assert data["query"]["range"] == {"fecha_publicacion": {"gte": LOWER_BOUND, "lte": "20200517"}}


def test_single_day_range_is_allowed() -> None:
    day = date(2020, 2, 29)
    data = build(published_from=day, published_to=day)
    assert data["query"]["range"]["fecha_publicacion"] == {"gte": "20200229", "lte": "20200229"}


def test_inverted_date_range_raises() -> None:
    with pytest.raises(ValueError):
        build_search_query(published_from=date(2021, 1, 1), published_to=date(2020, 1, 1))


def test_dates_are_zero_padded() -> None:
    data = build(published_from=date(2001, 2, 3), published_to=date(2001, 4, 5))
    assert data["query"]["range"]["fecha_publicacion"] == {"gte": "20010203", "lte": "20010405"}


def test_date_range_alone_still_returns_a_query() -> None:
    result = build_search_query(published_from=date(2020, 1, 1), sort_by=None)
    assert result is not None
    assert "sort" not in json.loads(result)


# --- combinations ------------------------------------------------------------------------------


def test_all_simple_filters_are_joined_with_and() -> None:
    assert clauses(
        official_number="40/2015",
        rank_code="1300",
        department_code="7723",
        scope_code="1",
        subject_code="6936",
    ) == {
        'numero_oficial:"40/2015"',
        "rango@codigo:1300",
        "departamento@codigo:7723",
        "ambito@codigo:1",
        "materia@codigo:6936",
    }


def test_clauses_are_separated_by_exactly_one_and() -> None:
    expression = query_string(official_number="3/2018", rank_code="1290")
    assert " AND " in expression
    assert len(expression.split(" AND ")) == 2


def test_reference_lookup_recipe() -> None:
    expression = query_string(official_number="40/2015", rank_code="1300", scope_code="1")
    assert set(expression.split(" AND ")) == {
        'numero_oficial:"40/2015"',
        "rango@codigo:1300",
        "ambito@codigo:1",
    }


def test_title_combined_with_codes_and_dates() -> None:
    data = build(
        title="energía",
        rank_code="1300",
        published_from=date(2020, 1, 1),
        published_to=date(2020, 12, 31),
    )
    expression = data["query"]["query_string"]["query"]
    assert_only_fields(expression, ["titulo", "rango@codigo"])
    assert "rango@codigo:1300" in expression
    assert data["query"]["range"]["fecha_publicacion"] == {"gte": "20200101", "lte": "20201231"}


def test_title_text_and_number_each_keep_their_field() -> None:
    expression = query_string(title="energía", text="autoconsumo", official_number="244/2019")
    assert_only_fields(expression, ["titulo", "texto", "numero_oficial"])
    assert 'numero_oficial:"244/2019"' in expression


# --- sorting -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sort_by", "field"),
    [
        ("published_on", "fecha_publicacion"),
        ("updated_at", "fecha_actualizacion"),
        ("enacted_on", "fecha_disposicion"),
    ],
)
def test_sort_field_mapping(sort_by: str, field: str) -> None:
    assert build(sort_by=sort_by)["sort"] == [{field: "desc"}]


def test_ascending_sort() -> None:
    assert build(descending=False)["sort"] == [{"fecha_publicacion": "asc"}]


def test_sort_is_added_to_a_filtered_query() -> None:
    data = build(rank_code="1300", sort_by="updated_at", descending=False)
    assert data["sort"] == [{"fecha_actualizacion": "asc"}]
    assert data["query"]["query_string"]["query"] == "rango@codigo:1300"


def test_sort_none_omits_the_sort_key() -> None:
    assert "sort" not in build(rank_code="1300", sort_by=None)


def test_sort_with_a_descending_flag_alone_is_not_a_criterion() -> None:
    assert build_search_query(sort_by=None, descending=False) is None
