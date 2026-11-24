"""Tests of ``owner``: who a stored row belongs to (never taken from a tool argument).

Two regimes. Locally (stdio, ``remote=False``, the default) there is no identity and the owner is
``local``. Over HTTP the caller passes ``remote=True``: the owner is then ``github:<numeric id>``
taken from the token's ``sub`` claim, and anything else fails closed with ``NotSignedInError``;
``local`` is never the answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from boe_insights_mcp import owner


def with_token(monkeypatch: pytest.MonkeyPatch, token: Any) -> None:
    """Make ``owner`` see ``token`` as the authenticated access token."""
    monkeypatch.setattr("boe_insights_mcp.owner.get_access_token", lambda: token)


def test_the_local_owner_constant() -> None:
    assert owner.LOCAL_OWNER == "local"


def test_without_authentication_the_owner_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, None)

    assert owner.current_owner() == "local"


def test_the_real_dependency_gives_local_outside_a_request() -> None:
    assert owner.current_owner() == "local"


def test_a_token_gives_a_github_owner_from_its_numeric_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "1001", "login": "alice"}))

    assert owner.current_owner(remote=True) == "github:1001"


def test_the_owner_comes_from_the_subject_not_from_the_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "1001", "login": "renamed"}))
    renamed = owner.current_owner(remote=True)
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "1001", "login": "alice"}))

    assert renamed == owner.current_owner(remote=True) == "github:1001"


def test_over_http_without_a_token_the_owner_is_never_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, None)

    with pytest.raises(owner.NotSignedInError):
        owner.current_owner(remote=True)


def test_over_http_the_real_dependency_outside_a_request_is_not_local() -> None:
    with pytest.raises(owner.NotSignedInError):
        owner.current_owner(remote=True)


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"sub": ""},
        {"sub": None},
        {"other": "x"},
        {"sub": "   "},
        {"sub": " 1001"},
        {"sub": "1001 "},
        {"sub": "1001\n"},
        {"sub": "10\n01"},
        {"sub": "10\x0001"},
        {"sub": "1001\u200b"},
        {"sub": 42},
        {"sub": 1001},
        {"sub": ["1001"]},
        {"sub": "abc-123"},
        {"sub": "alice"},
        {"sub": "user:1001"},
        {"sub": "github:1001"},
        {"sub": "1.5"},
        {"sub": "-1"},
        {"sub": "+1"},
        {"sub": "1_0"},
        {"sub": "\u0661\u0662"},
        {"sub": "\u00b2"},
        {"sub": "\uff11\uff12"},
        {"sub": "local"},
    ],
    ids=repr,
)
def test_a_token_without_a_numeric_subject_is_refused(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims=claims))

    with pytest.raises(PermissionError):
        owner.current_owner(remote=True)


def test_a_token_without_claims_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, SimpleNamespace())

    with pytest.raises(owner.NotSignedInError):
        owner.current_owner(remote=True)


def test_the_refusal_is_the_error_the_server_turns_into_a_curated_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={}))

    with pytest.raises(owner.NotSignedInError):
        owner.current_owner(remote=True)


def test_the_refusal_message_does_not_echo_the_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "secret-looking-subject"}))

    with pytest.raises(owner.NotSignedInError) as caught:
        owner.current_owner(remote=True)

    assert "secret-looking-subject" not in str(caught.value)


@pytest.mark.parametrize("subject", ["1", "1001", "9007199254740993", "0", "012"])
def test_ordinary_numeric_subjects_are_accepted_as_they_are(
    monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": subject}))

    assert owner.current_owner(remote=True) == f"github:{subject}"


def test_two_subjects_are_two_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "12"}))
    first = owner.current_owner(remote=True)
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "123"}))

    assert first != owner.current_owner(remote=True)


def test_the_old_user_prefix_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "1001"}))

    assert not owner.current_owner(remote=True).startswith("user:")
