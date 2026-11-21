"""Exceptions raised by the BOE client.

Every error derives from :class:`BoeError` and carries a stable machine-readable ``code`` and a
``retryable`` flag, so callers (such as MCP tools) can map failures without parsing messages.
"""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "BoeError",
    "BoeNotFoundError",
    "BoeQueryError",
    "BoeResponseError",
    "BoeUnavailableError",
]


class BoeError(Exception):
    """Base class for BOE client failures.

    ``str(error)`` is a clear English message; ``api_message`` is the (Spanish) text of the
    API's own error envelope, when there was one, and ``status`` the HTTP status, if any.
    """

    code: ClassVar[str] = "error"
    retryable: ClassVar[bool] = False

    def __init__(
        self, message: str, *, status: int | None = None, api_message: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.api_message = api_message


class BoeNotFoundError(BoeError):
    """The requested resource does not exist (HTTP 404)."""

    code = "not_found"
    retryable = False


class BoeQueryError(BoeError):
    """The API rejected the request or the search query as invalid."""

    code = "invalid_query"
    retryable = False


class BoeUnavailableError(BoeError):
    """The API could not be reached or kept failing after all retries."""

    code = "unavailable"
    retryable = True


class BoeResponseError(BoeError):
    """The API answered, but the response could not be parsed or validated."""

    code = "bad_response"
    retryable = False
