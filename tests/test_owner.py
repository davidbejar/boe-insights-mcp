"""Tests of ``owner``: who a stored row belongs to (never taken from a tool argument)."""

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


def test_a_token_gives_a_user_owner_from_its_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "abc-123"}))

    assert owner.current_owner() == "user:abc-123"


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"sub": ""},
        {"sub": None},
        {"other": "x"},
        {"sub": "   "},
        {"sub": " alice"},
        {"sub": "alice "},
        {"sub": "al\nice"},
        {"sub": "al\x00ice"},
        {"sub": "al\u200bice"},
        {"sub": 42},
    ],
)
def test_a_token_without_a_subject_is_refused(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims=claims))

    with pytest.raises(PermissionError):
        owner.current_owner()


def test_the_refusal_is_the_error_the_server_turns_into_a_curated_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={}))

    with pytest.raises(owner.NotSignedInError):
        owner.current_owner()


@pytest.mark.parametrize("subject", ["abc-123", "a b", "ñandú", "auth0|12345", "x@y.org"])
def test_ordinary_subjects_are_accepted_as_they_are(
    monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": subject}))

    assert owner.current_owner() == f"user:{subject}"


def test_two_subjects_are_two_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "a"}))
    first = owner.current_owner()
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "b"}))

    assert first != owner.current_owner()
