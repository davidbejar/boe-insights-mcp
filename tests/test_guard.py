"""Tests of ``guard``: expected failures become the caller's error class, the rest propagate."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.guard import guarded, invalid_argument
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
