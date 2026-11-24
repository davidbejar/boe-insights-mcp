"""Tests of ``guard``: expected failures become the caller's error class, the rest propagate."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.guard import guarded, guarded_for_owner, invalid_argument
from boe_insights_mcp.owner import NotSignedInError
from boe_insights_mcp.searchstore import SearchStoreError


class CallerError(Exception):
    """Stands for ``ToolError`` or ``ResourceError``."""


class Model(BaseModel):
    """A model that can fail validation."""

    number: int


async def fails_with(error: BaseException) -> None:
    raise error


async def fails_with_invalid_model() -> None:
    Model(number="x")  # type: ignore[arg-type]


async def test_a_result_passes_through() -> None:
    async def value() -> int:
        return 7

    assert await guarded(value(), "tool", CallerError) == 7


async def test_a_boe_error_becomes_a_curated_message() -> None:
    with pytest.raises(CallerError, match="not_found"):
        await guarded(fails_with(BoeNotFoundError("gone", status=404)), "read_article", CallerError)


async def test_a_value_error_becomes_an_invalid_argument() -> None:
    with pytest.raises(CallerError) as caught:
        await guarded(fails_with(ValueError("limit must be 1")), "tool", CallerError)

    assert str(caught.value) == invalid_argument("limit must be 1")
    assert str(caught.value).startswith("Invalid argument (invalid_argument, not retryable): ")


async def test_a_validation_error_is_a_bug_and_propagates_unchanged() -> None:
    with pytest.raises(ValidationError):
        await guarded(fails_with_invalid_model(), "tool", CallerError)


async def test_anything_else_propagates() -> None:
    with pytest.raises(PermissionError):
        await guarded(fails_with(PermissionError("no subject")), "tool", CallerError)


async def test_a_store_failure_is_a_curated_storage_error() -> None:
    with pytest.raises(CallerError) as caught:
        await guarded(
            fails_with(SearchStoreError("the saved-searches file cannot be used")),
            "list_saved_searches",
            CallerError,
        )

    assert str(caught.value) == (
        "Saved searches are unavailable (storage_error, not retryable): "
        "the saved-searches file cannot be used."
    )


async def test_a_missing_identity_is_a_curated_unauthorized_error() -> None:
    with pytest.raises(CallerError) as caught:
        await guarded(fails_with(NotSignedInError("no subject")), "save_search", CallerError)

    assert str(caught.value) == (
        "Not signed in (unauthorized, not retryable): saved searches need an identified user."
    )
    assert "no subject" not in str(caught.value)


def with_token(monkeypatch: pytest.MonkeyPatch, token: Any) -> None:
    """Make ``owner`` see ``token`` as the authenticated access token."""
    monkeypatch.setattr("boe_insights_mcp.owner.get_access_token", lambda: token)


class Spy:
    """A use case that records the owner it was run for."""

    def __init__(self) -> None:
        self.owners: list[str] = []

    async def __call__(self, who: str) -> str:
        self.owners.append(who)
        return "done"


async def test_locally_the_use_case_runs_for_the_local_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, None)
    spy = Spy()

    assert await guarded_for_owner(spy, "tool", CallerError) == "done"
    assert spy.owners == ["local"]


async def test_over_http_the_use_case_runs_for_the_github_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": "1001", "login": "alice"}))
    spy = Spy()

    result = await guarded_for_owner(spy, "tool", CallerError, remote=True)

    assert result == "done"
    assert spy.owners == ["github:1001"]


async def test_over_http_without_a_token_the_use_case_never_runs_and_the_message_is_curated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_token(monkeypatch, None)
    spy = Spy()

    with pytest.raises(CallerError) as caught:
        await guarded_for_owner(spy, "tool", CallerError, remote=True)

    assert spy.owners == []
    assert str(caught.value) == (
        "Not signed in (unauthorized, not retryable): saved searches need an identified user."
    )


@pytest.mark.parametrize("subject", ["abc", "", " 12", 12, None])
async def test_over_http_an_unusable_subject_never_runs_the_use_case(
    monkeypatch: pytest.MonkeyPatch, subject: Any
) -> None:
    with_token(monkeypatch, SimpleNamespace(claims={"sub": subject}))
    spy = Spy()

    with pytest.raises(CallerError, match="Not signed in"):
        await guarded_for_owner(spy, "tool", CallerError, remote=True)

    assert spy.owners == []
