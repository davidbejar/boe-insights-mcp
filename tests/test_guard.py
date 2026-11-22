"""Tests of ``guard``: expected failures become the caller's error class, the rest propagate."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.guard import guarded, invalid_argument


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
