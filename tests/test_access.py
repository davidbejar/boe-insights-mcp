"""Tests of the allow-list: who may use a signed-in server.

Pure unit tests of ``boe_insights_mcp.access.AllowList``. An entry is a GitHub username (matched
case-insensitively against the ``login`` claim), ``id:<digits>`` (matched exactly against ``sub``)
or ``*``; an empty list allows nobody; a token whose ``sub`` is not a non-empty string of ASCII
digits matches nothing, ``*`` included.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.utilities.authorization import AuthContext

from boe_insights_mcp.access import AllowList, allow_check

VARIABLE = "BOE_INSIGHTS_ALLOWED_USERS"


def allowing(*entries: str) -> AllowList:
    """The allow-list of ``entries``."""
    return AllowList.parse(entries)


# --- Parsing -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "alice",
        "Alice",
        "a",
        "7",
        "a-b",
        "a-b-c",
        "alice99",
        "12345",
        "x" * 39,
        "id:1",
        "id:0",
        "id:012",
        "id:9007199254740993",
        "*",
        "  alice  ",
    ],
)
def test_a_valid_entry_is_accepted(entry: str) -> None:
    AllowList.parse([entry])


@pytest.mark.parametrize(
    "entry",
    [
        "a--b",
        "x" * 40,
        "al ice",
        "al_ice",
        "alice!",
        "@alice",
        "alice/bob",
        "alice;bob",
        "ali\nce",
        "alice\x00",
        "id:",
        "id:abc",
        "id:1a",
        "id:-1",
        "id:+1",
        "id:1.0",
        "id: 12",
        "id:12:13",
        "id:\u0661\u0662",  # Arabic-Indic digits: str.isdigit() and \d say yes, we say no
        "id:\u00b2",  # superscript two: isdigit() is True
        "id:\uff11\uff12",  # fullwidth digits
        "**",
        "*alice",
        "al*",
        "*,alice",
        "\u0430lice",  # Cyrillic a
        "alic\u00e9",
        "\uff41lice",  # fullwidth a
        "\u212aevin",  # Kelvin sign: matches [a-z] under re.IGNORECASE
        "\u017fam",  # long s: matches [a-z] under re.IGNORECASE
        "\u0131",  # dotless i
    ],
)
def test_an_invalid_entry_is_refused_naming_the_variable_and_the_entry(entry: str) -> None:
    with pytest.raises(ValueError) as caught:
        AllowList.parse(["alice", entry, "bob"])

    message = str(caught.value)
    assert VARIABLE in message
    assert entry in message or repr(entry) in message


def test_one_bad_entry_refuses_the_whole_list() -> None:
    with pytest.raises(ValueError):
        AllowList.parse(["*", "id:12", "bad entry"])


def test_blank_entries_are_ignored_not_errors() -> None:
    allow = AllowList.parse(["", "  ", "alice", "\t"])

    assert allow.allows("1", "alice") is True
    assert allow.allows("1", "bob") is False


def test_entries_may_come_from_any_iterable() -> None:
    allow = AllowList.parse(entry for entry in ("alice", "id:12"))

    assert allow.allows("99", "alice") is True
    assert allow.allows("12", None) is True


# --- Closed by default ---------------------------------------------------------------------------


@pytest.mark.parametrize("entries", [[], [""], [" ", ""], ()])
def test_an_empty_list_allows_nobody(entries: Iterable[str]) -> None:
    allow = AllowList.parse(entries)

    assert allow.allows("1001", "alice") is False
    assert allow.allows("1001", None) is False
    assert allow.allows(None, None) is False


# --- Usernames -----------------------------------------------------------------------------------


@pytest.mark.parametrize("login", ["alice", "Alice", "ALICE", "aLiCe"])
def test_a_username_matches_the_login_case_insensitively(login: str) -> None:
    assert allowing("alice").allows("1001", login) is True
    assert allowing("ALICE").allows("1001", login) is True


def test_a_username_entry_is_matched_after_trimming_its_spaces() -> None:
    assert allowing("  alice ").allows("1001", "alice") is True


@pytest.mark.parametrize(
    "login",
    [
        "alic",
        "al",
        "a",
        "alice2",
        "alice-",
        "alice-smith",
        "xalice",
        "malice",
        "-alice",
        "alicee",
        "alice ",
        " alice",
        "alice\n",
        "alice\x00",
        "al-ice",
        "al_ice",
        "alice.",
        "alice@example.org",
        "",
    ],
)
def test_a_username_does_not_match_a_prefix_a_superstring_or_a_padded_login(login: str) -> None:
    assert allowing("alice").allows("1001", login) is False


@pytest.mark.parametrize("login", ["alic", "al", "a", "a-b"])
def test_an_allowed_longer_name_does_not_allow_its_prefixes(login: str) -> None:
    assert allowing("alice-bob").allows("1001", login) is False


def test_a_hyphenated_username_matches_only_itself() -> None:
    allow = allowing("a-b")

    assert allow.allows("1", "a-b") is True
    assert allow.allows("1", "A-B") is True
    assert allow.allows("1", "ab") is False
    assert allow.allows("1", "a_b") is False
    assert allow.allows("1", "a-b-c") is False


@pytest.mark.parametrize(
    ("entry", "login"),
    [
        ("alice", "\u0430lice"),  # Cyrillic a
        ("alice", "\uff41lice"),  # fullwidth a
        ("kevin", "\u212aevin"),  # Kelvin sign lower-cases to k
        ("sam", "\u017fam"),  # long s casefolds to s
        ("ss", "\u00df"),  # sharp s casefolds to ss
        ("strasse", "stra\u00dfe"),
        ("ian", "\u0130an"),  # dotted capital I
        ("alice", "alic\u00e9"),
        ("alice", "alice\u200b"),  # zero-width space
    ],
)
def test_a_unicode_look_alike_login_does_not_match(entry: str, login: str) -> None:
    assert allowing(entry).allows("1001", login) is False


def test_a_username_is_not_matched_against_the_subject() -> None:
    allow = allowing("alice")

    assert allow.allows("alice", "bob") is False
    assert allow.allows("alice", None) is False


def test_several_usernames_each_match_only_their_own_login() -> None:
    allow = allowing("alice", "Bob")

    assert allow.allows("1", "alice") is True
    assert allow.allows("2", "BOB") is True
    assert allow.allows("3", "carol") is False


# --- Ids -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("login", [None, "", "someone-else", "alice"])
def test_an_id_entry_matches_its_subject_whatever_the_login(login: str | None) -> None:
    assert allowing("id:1001").allows("1001", login) is True


@pytest.mark.parametrize(
    "sub", ["100", "10010", "11001", "1", "001001", "01001", "1001 1", "10011", "2001"]
)
def test_an_id_entry_is_matched_exactly_not_by_prefix_suffix_or_substring(sub: str) -> None:
    assert allowing("id:1001").allows(sub, "alice") is False


def test_id_12_does_not_allow_id_123_and_the_other_way_round() -> None:
    assert allowing("id:12").allows("123", "x") is False
    assert allowing("id:123").allows("12", "x") is False
    assert allowing("id:12").allows("12", "x") is True


def test_leading_zeros_are_not_normalised_away() -> None:
    assert allowing("id:012").allows("12", "x") is False
    assert allowing("id:12").allows("012", "x") is False
    assert allowing("id:012").allows("012", "x") is True


def test_an_id_entry_is_not_matched_against_the_login() -> None:
    # A login can be all digits (GitHub allows it); only a username entry reads the login.
    assert allowing("id:12345").allows("99", "12345") is False


def test_an_all_digit_entry_is_a_username_not_an_id() -> None:
    allow = allowing("12345")

    assert allow.allows("12345", "bob") is False
    assert allow.allows("99", "12345") is True


# --- Everyone ------------------------------------------------------------------------------------


@pytest.mark.parametrize("login", ["alice", "BOB", "", None, " odd "])
def test_a_star_allows_anyone_with_a_usable_subject(login: str | None) -> None:
    assert allowing("*").allows("1001", login) is True


def test_a_star_mixed_with_names_still_allows_everyone() -> None:
    allow = allowing("alice", "*", "id:5")

    assert allow.allows("77", "stranger") is True
    assert allow.allows("5", None) is True


def test_a_star_is_not_a_wildcard_inside_a_name() -> None:
    with pytest.raises(ValueError):
        AllowList.parse(["al*"])


# --- A token without a usable subject ------------------------------------------------------------

UNUSABLE_SUBJECTS: list[Any] = [
    None,
    "",
    " ",
    " 12",
    "12 ",
    "12\n",
    "\n12",
    "1\n2",
    "abc",
    "12a",
    "1.5",
    "-12",
    "+12",
    "1_2",
    "1e3",
    "0x1f",
    "\u0661\u0662",
    "\u00b2",
    "\uff11\uff12",
    "12\x00",
    "12\u200b",
    42,
    12,
    b"12",
]


@pytest.mark.parametrize("sub", UNUSABLE_SUBJECTS, ids=repr)
def test_an_unusable_subject_matches_nothing_not_even_the_star(sub: Any) -> None:
    everything = allowing("*", "alice", "id:12", "id:1", "id:42", "12")

    assert everything.allows(sub, "alice") is False
    assert everything.allows(sub, None) is False
    assert everything.allows(sub, "12") is False


# --- A token with a missing or odd login ---------------------------------------------------------


@pytest.mark.parametrize("login", [None, "", "   ", 5, 5.5, ["alice"], b"alice"], ids=repr)
def test_a_missing_or_odd_login_matches_no_username_entry(login: Any) -> None:
    assert allowing("alice", "12345", "a").allows("1001", login) is False


@pytest.mark.parametrize("login", [None, "", "   ", 5, ["alice"]], ids=repr)
def test_a_missing_or_odd_login_can_still_match_an_id_entry(login: Any) -> None:
    assert allowing("alice", "id:1001").allows("1001", login) is True


# --- Answering for a token -----------------------------------------------------------------------


def token(**claims: Any) -> Any:
    """A token object carrying ``claims``, as FastMCP's access token does."""
    return SimpleNamespace(claims=claims)


NO_IDENTITY: list[Any] = [
    pytest.param(None, id="no-token"),
    # A token type without claims, as the SDK's own access token is.
    pytest.param(object(), id="no-claims-attribute"),
    pytest.param(SimpleNamespace(claims=None), id="claims-none"),
    pytest.param(SimpleNamespace(claims={}), id="claims-empty"),
]


@pytest.mark.parametrize("given", NO_IDENTITY)
def test_the_component_check_refuses_a_caller_without_an_identity(given: Any) -> None:
    check = allow_check(allowing("*", "alice", "id:1001"))

    assert check(AuthContext(token=given, component=object())) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("given", NO_IDENTITY)
def test_a_token_without_an_identity_is_not_allowed_even_by_a_star(given: Any) -> None:
    assert allowing("*", "alice", "id:1001").allows_token(given) is False


TOKEN_CASES: list[tuple[tuple[str, ...], dict[str, Any], bool]] = [
    (("id:1001",), {"sub": "1001", "login": "someone-else"}, True),  # the id alone admits
    (("alice",), {"sub": "1001", "login": "Alice"}, True),  # the login alone admits
    (("alice",), {"login": "alice"}, False),  # a listed login without a subject does not
    (("alice",), {"sub": "1001", "login": "bob"}, False),
    (("id:1001",), {"sub": "1002", "login": "alice"}, False),
    (("*",), {"sub": "7"}, True),
    (("*",), {"sub": 7, "login": "alice"}, False),
]


@pytest.mark.parametrize(("entries", "claims", "expected"), TOKEN_CASES)
def test_the_component_check_answers_from_the_sub_and_login_claims(
    entries: tuple[str, ...], claims: dict[str, Any], expected: bool
) -> None:
    check = allow_check(allowing(*entries))

    assert check(AuthContext(token=token(**claims), component=object())) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize(("entries", "claims", "expected"), TOKEN_CASES)
def test_a_token_is_answered_for_from_its_sub_and_login_claims(
    entries: tuple[str, ...], claims: dict[str, Any], expected: bool
) -> None:
    assert allowing(*entries).allows_token(token(**claims)) is expected


def test_claims_of_an_odd_type_raise_rather_than_admit() -> None:
    # Pinned as it behaves today: claims are read as a mapping, and FastMCP's token types always
    # carry one, so a truthy non-mapping fails loudly instead of being guessed at.
    odd = SimpleNamespace(claims=["sub", "login"])
    allow = allowing("*")

    with pytest.raises(AttributeError):
        allow.allows_token(odd)
    with pytest.raises(AttributeError):
        allow_check(allow)(AuthContext(token=odd, component=object()))  # type: ignore[arg-type]
