"""One translation of expected failures into model-visible errors, shared by tools and resources.

The server is created with ``mask_error_details=True``: FastMCP shows the model only the message
of a ``ToolError`` (or, for a resource, a ``ResourceError``) and replaces any other exception
with a generic one, so internals (paths, input values, stack details) never leave the process.
``guarded`` is where the exceptions we *want* the model to see are turned into the error class of
the caller: ``server`` passes ``ToolError``, ``resources`` passes ``ResourceError``.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

from pydantic import ValidationError

from boe_insights_mcp.errors import BoeError, describe_error
from boe_insights_mcp.owner import NotSignedInError
from boe_insights_mcp.searchstore import SearchStoreError

__all__ = ["guarded", "invalid_argument"]

_T = TypeVar("_T")


def invalid_argument(message: str) -> str:
    """The curated text for a bad argument."""
    return f"Invalid argument (invalid_argument, not retryable): {message}"


async def guarded(call: Awaitable[_T], tool: str, error_class: type[Exception]) -> _T:
    """Await a use case of ``tool``, translating its expected errors into ``error_class``.

    ``BoeError`` becomes a curated message with code, retryability and next step; a
    ``SearchStoreError`` (the saved-searches file cannot be used) and a missing identity become
    their own curated messages, which carry no file contents; ``ValueError``
    (bad arguments detected by the use cases or the client) becomes an "Invalid argument"
    message. Everything else propagates to be masked. A pydantic ``ValidationError`` is a
    ``ValueError`` too, but here it means a bug in our own models and carries input values, so it
    propagates as well.
    """
    try:
        return await call
    except BoeError as error:
        raise error_class(describe_error(error, tool)) from error
    except SearchStoreError as error:
        # The store's messages are written to be shown (they never quote file contents).
        text = str(error).rstrip(".")
        raise error_class(
            f"Saved searches are unavailable (storage_error, not retryable): {text}."
        ) from error
    except NotSignedInError as error:
        raise error_class(
            "Not signed in (unauthorized, not retryable): saved searches need an identified user."
        ) from error
    except ValidationError:
        # Must come before ``except ValueError``, which it would otherwise be caught by.
        raise
    except ValueError as error:
        raise error_class(invalid_argument(str(error))) from error
