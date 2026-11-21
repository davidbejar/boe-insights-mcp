"""Contract tests for ``boe_insights_mcp.references``."""

from __future__ import annotations

import dataclasses

import pytest

from boe_insights_mcp.references import ParsedReference, parse_reference

RANK_NAMES = {
    "1290": "Ley Orgánica",
    "1300": "Ley",
    "1310": "Real Decreto Legislativo",
    "1320": "Real Decreto-ley",
    "1340": "Real Decreto",
    "1350": "Orden",
    "1370": "Resolución",
    "1390": "Circular",
    "1410": "Instrucción",
    "1325": "Decreto-ley Foral",
    "1450": "Ley Foral",
    "1470": "Decreto Legislativo",
    "1480": "Decreto Foral Legislativo",
    "1500": "Decreto-ley",
    "1510": "Decreto",
}


def _numbered(text: str) -> ParsedReference:
    parsed = parse_reference(text)
    assert parsed is not None, f"expected {text!r} to parse"
    assert parsed.kind == "numbered"
    return parsed


# --- Rank words and aliases -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Ley Orgánica 3/2018", "1290"),
        ("LO 3/2018", "1290"),
        ("L.O. 3/2018", "1290"),
        ("Ley 39/2015", "1300"),
        ("Real Decreto-ley 6/2019", "1320"),
        ("Real Decreto ley 6/2019", "1320"),
        ("RDL 6/2019", "1320"),
        ("RD-ley 6/2019", "1320"),
        ("RDley 6/2019", "1320"),
        ("R.D.L. 6/2019", "1320"),
        ("Real Decreto Legislativo 2/2015", "1310"),
        ("RDLeg 2/2015", "1310"),
        ("RD Legislativo 2/2015", "1310"),
        ("R.D.Leg. 2/2015", "1310"),
        ("Real Decreto 1065/2007", "1340"),
        ("RD 1065/2007", "1340"),
        ("R.D. 1065/2007", "1340"),
        ("Orden 315/2020", "1350"),
        ("Resolución 12/2020", "1370"),
        ("Circular 1/2020", "1390"),
        ("Instrucción 2/2020", "1410"),
    ],
)
def test_rank_words_and_aliases_map_to_boe_codes(text: str, code: str) -> None:
    parsed = _numbered(text)

    assert parsed.rank_code == code
    assert parsed.rank_name == RANK_NAMES[code]


@pytest.mark.parametrize(
    ("text", "expected_number"),
    [
        ("Ley 39/2015", "39/2015"),
        ("Real Decreto 1065/2007", "1065/2007"),
        ("Ley 1/1800", "1/1800"),
        ("Ley 1/2100", "1/2100"),
    ],
)
def test_official_number_is_extracted(text: str, expected_number: str) -> None:
    assert _numbered(text).official_number == expected_number


def test_numbered_reference_has_no_identifier() -> None:
    assert _numbered("Ley 39/2015").identifier is None


# --- Tolerance ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["LEY ORGÁNICA 3/2018", "ley organica 3/2018"],
)
def test_rank_matching_ignores_case_and_accents(text: str) -> None:
    assert _numbered(text).rank_code == "1290"


@pytest.mark.parametrize("text", ["Resolucion 12/2020", "RESOLUCIÓN 12/2020"])
def test_resolucion_accent_variants(text: str) -> None:
    assert _numbered(text).rank_code == "1370"


@pytest.mark.parametrize(
    "text",
    ["Ley   39/2015", "\t Ley 39/2015 \n"],
)
def test_extra_whitespace_is_tolerated(text: str) -> None:
    parsed = _numbered(text)

    assert parsed.rank_code == "1300"
    assert parsed.official_number == "39/2015"


def test_multiword_rank_tolerates_spacing_and_case() -> None:
    assert _numbered("REAL  DECRETO   ley 6/2019").rank_code == "1320"


# --- Longest match precedence ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Real Decreto-ley 6/2019", "1320"),
        ("Real Decreto Legislativo 2/2015", "1310"),
        ("Ley Orgánica 3/2018", "1290"),
        ("RDL 6/2019", "1320"),
        ("RD 6/2019", "1340"),
    ],
)
def test_longest_rank_alternative_wins(text: str, code: str) -> None:
    assert _numbered(text).rank_code == code


# --- Embedded references --------------------------------------------------------------


def test_reference_embedded_in_phrase_with_article_and_date() -> None:
    parsed = _numbered("artículo 5 de la Ley 39/2015, de 1 de octubre")

    assert parsed.rank_code == "1300"
    assert parsed.official_number == "39/2015"


def test_embedded_decree_law_is_not_misread_as_law() -> None:
    parsed = _numbered("según el Real Decreto-ley 6/2019, de 1 de marzo, de medidas urgentes")

    assert parsed.rank_code == "1320"
    assert parsed.official_number == "6/2019"


def test_first_rank_number_pair_wins() -> None:
    parsed = _numbered("el RD 1065/2007 desarrolla la Ley 58/2003")

    assert parsed.rank_code == "1340"
    assert parsed.official_number == "1065/2007"


def test_first_number_wins_among_several_bare_numbers() -> None:
    parsed = _numbered("ver 39/2015 y 40/2015")

    assert parsed.rank_code is None
    assert parsed.official_number == "39/2015"


# --- Bare numbers and ministerial orders ----------------------------------------------


def test_bare_number_has_no_rank() -> None:
    parsed = _numbered("40/2015")

    assert parsed.rank_code is None
    assert parsed.rank_name is None
    assert parsed.official_number == "40/2015"


def test_ministerial_order_number_is_kept_whole() -> None:
    parsed = _numbered("Orden APA/315/2020")

    assert parsed.rank_code == "1350"
    assert parsed.rank_name == "Orden"
    assert parsed.official_number == "APA/315/2020"


def test_ministerial_order_number_embedded_in_phrase() -> None:
    parsed = _numbered("la Orden APA/315/2020, de 30 de marzo")

    assert parsed.rank_code == "1350"
    assert parsed.official_number == "APA/315/2020"


# --- Words that merely contain a rank word --------------------------------------------


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("Morley 3/2018", "3/2018"),
        ("leyenda 3/2018", "3/2018"),
        ("record 5/2020", "5/2020"),
    ],
)
def test_rank_words_inside_longer_words_are_not_ranks(text: str, number: str) -> None:
    parsed = _numbered(text)

    assert parsed.rank_code is None
    assert parsed.rank_name is None
    assert parsed.official_number == number


# --- Identifiers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("BOE-A-2015-10566", "BOE-A-2015-10566"),
        ("  BOE-A-2015-10566\n", "BOE-A-2015-10566"),
        ("DOGC-f-2010-90008", "DOGC-f-2010-90008"),
    ],
)
def test_identifiers_are_recognised(text: str, expected: str) -> None:
    parsed = parse_reference(text)

    assert parsed is not None
    assert parsed.kind == "identifier"
    assert parsed.identifier == expected


def test_identifier_has_no_rank_or_number() -> None:
    parsed = parse_reference("BOE-A-2015-10566")

    assert parsed is not None
    assert parsed.rank_code is None
    assert parsed.rank_name is None
    assert parsed.official_number is None


def test_lowercase_identifier_prefix_is_upper_cased() -> None:
    parsed = parse_reference("boe-a-2015-10566")

    assert parsed is not None
    assert parsed.kind == "identifier"
    assert parsed.identifier is not None
    assert parsed.identifier.startswith("BOE-")
    assert parsed.identifier.upper() == "BOE-A-2015-10566"


# --- Rejections -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Ley 39/15",
        "39/15",
        "Ley 39/1799",
        "Ley 39/2101",
        "Ley 39",
        "Ley",
        "",
        "   ",
        "hello world",
        "1/2",
    ],
)
def test_unparseable_text_returns_none(text: str) -> None:
    assert parse_reference(text) is None


# --- Result type ----------------------------------------------------------------------


def test_parsed_reference_is_frozen() -> None:
    parsed = _numbered("Ley 39/2015")

    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.rank_code = "1340"  # type: ignore[misc]


def test_parsed_reference_equality_is_by_value() -> None:
    assert parse_reference("Ley 39/2015") == parse_reference("ley  39/2015")


# --- Regional ranks (BOE `rangos` table) -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Ley Foral 3/2010", "1450"),
        ("Decreto Legislativo 1/2010", "1470"),
        ("Decreto Foral Legislativo 1/2010", "1480"),
        ("Decreto-ley 1/2010", "1500"),
        ("Decreto ley 1/2010", "1500"),
        ("Decreto-ley Foral 1/2010", "1325"),
        ("Decreto 1/2010", "1510"),
        ("el Decreto Legislativo 1/2010, de 5 de marzo", "1470"),
        # Longest match: the state ranks keep winning when "Real" is present.
        ("Real Decreto Legislativo 1/2010", "1310"),
        ("Real Decreto-ley 1/2010", "1320"),
        ("Real Decreto 1/2010", "1340"),
        ("Ley 1/2010", "1300"),
        ("Ley Orgánica 1/2010", "1290"),
    ],
)
def test_regional_ranks_and_longest_match_precedence(text: str, code: str) -> None:
    parsed = _numbered(text)

    assert parsed.rank_code == code
    assert parsed.rank_name == RANK_NAMES[code]


# (text, may be regional, rank is regional, unrecognised rank word, region mentioned)
SIGNALS = [
    ("Ley 40/2015", False, False, False, False),
    ("40/2015", False, False, False, False),
    ("Real Decreto Legislativo 1/2010", False, False, False, False),
    ("artículo 5 de la Ley 39/2015, de 1 de octubre", False, False, False, False),
    ("ver 39/2015", False, False, False, False),
    ("Decreto Legislativo 1/2010", True, True, False, False),
    ("Ley Foral 3/2010", True, True, False, True),
    ("Decreto 1/2010", True, True, False, False),
    ("Ley 2/2020 de Andalucía", True, False, False, True),
    ("Ley 2/2020 de Andalucia", True, False, False, True),
    ("Ley 5/2010 de Cataluña", True, False, False, True),
    ("Ley 5/2010 de Catalunya", True, False, False, True),
    ("Ley 3/2010 del País Vasco", True, False, False, True),
    ("Ley 3/2010 de Euskadi", True, False, False, True),
    ("Ley 3/2010 de la Comunidad de Madrid", True, False, False, True),
    ("Ley 3/2010 de Castilla y León", True, False, False, True),
    ("Ley 3/2010 de Castilla-La Mancha", True, False, False, True),
    ("Ley 3/2010 de las Illes Balears", True, False, False, True),
    ("Ley 3/2010 de la Región de Murcia", True, False, False, True),
    ("Ley 3/2010 de La Rioja", True, False, False, True),
    ("Ley 4/2010 foral", True, False, False, True),
    ("Ley 4/2010 autonómica", True, False, False, True),
    ("Ley 4/2010 de la Comunitat Valenciana", True, False, False, True),
    ("Decreto Foral 3/2010", True, False, True, True),
    ("Reglamento 3/2010", True, False, True, False),
    ("Acuerdo 3/2010", True, False, True, False),
    ("Ley de Cataluña 3/2010", True, False, True, True),
]


@pytest.mark.parametrize(
    ("text", "maybe", "regional_rank", "unrecognised", "region"),
    SIGNALS,
    ids=[s[0] for s in SIGNALS],
)
def test_reference_signals_that_the_law_may_be_regional(
    text: str, maybe: bool, regional_rank: bool, unrecognised: bool, region: bool
) -> None:
    parsed = _numbered(text)

    assert parsed.may_be_regional is maybe
    assert parsed.regional_rank is regional_rank
    assert (parsed.unrecognised_rank_word is not None) is unrecognised
    assert (parsed.region is not None) is region
