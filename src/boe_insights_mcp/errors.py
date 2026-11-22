"""Exceptions raised by the BOE client.

Every error derives from :class:`BoeError` and carries a stable machine-readable ``code`` and a
``retryable`` flag, so callers (such as MCP tools) can map failures without parsing messages.

``client`` raises them (``_error_for`` maps HTTP statuses onto the classes, and its retry loop
retries only those with ``retryable`` true). ``server`` turns them into tool errors using the
``code``, never the raw message, so API text is not forwarded to the model. ``describe_error`` is
that translation, shared with the use cases that report a failure inside a result (the
``str()`` of an error can carry the API's own text, so it is never shown).
"""

from __future__ import annotations

from typing import ClassVar

__all__ = [
    "BoeError",
    "BoeNotFoundError",
    "BoeQueryError",
    "BoeResponseError",
    "BoeUnavailableError",
    "describe_error",
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


# Tool error messages are assembled from these fixed texts, keyed by ``BoeError.code``. The API's
# own message is deliberately not included: it is Spanish, and can leak internals such as the
# index name ``TITULOS`` (api-notes 2, "Errors"). Only what to *do* about it is told to the model.
_ERROR_SUMMARIES = {
    "not_found": "BOE has no law with that identifier",
    "invalid_query": "BOE rejected the search",
    "unavailable": "BOE API unavailable",
    "bad_response": "BOE returned an unexpected response",
}
_ERROR_ADVICE = {
    "not_found": "Use resolve_reference to find the correct identifier.",
    "unavailable": "Wait a few seconds and try again.",
    "bad_response": "Try again later; if it persists, the BOE API may have changed.",
}
# What to do when the BOE rejects a request as invalid depends on what the tool sent.
_INVALID_QUERY_ADVICE = {
    "search_legislation": "Simplify the search terms or remove filters, then try again.",
    "resolve_reference": "Check the reference: use a form such as 'Ley 40/2015' or an "
    "identifier, or search by title with search_legislation.",
    "get_law_overview": "Check the identifier (for example BOE-A-2015-10566) or get it again "
    "from resolve_reference.",
    "get_amendment_history": "Check the identifier (for example BOE-A-2015-10566) or get it "
    "again from resolve_reference.",
    "read_article": "Check the identifier and the article wording, or get the identifier again "
    "from resolve_reference.",
    "compare_article_versions": "Check the identifier, the article wording and the ISO dates, "
    "or get the identifier again from resolve_reference.",
    "get_article_history": "Check the identifier and the article wording, or get the identifier "
    "again from resolve_reference.",
    "get_law_structure": "Check the identifier (for example BOE-A-2015-10566) or get it again "
    "from resolve_reference.",
    # The trend tools send the BOE only the text and subject_code filters.
    "legislation_over_time": "Simplify the text filter or check subject_code, then try again.",
    "top_departments": "Simplify the text filter or check subject_code, then try again.",
    "compare_topics": "Simplify the text words or check the subject codes, then try again.",
    "explain_spike": "Simplify the text filter or check subject_code, then try again.",
    "most_amended_laws": "Simplify the text filter or check subject_code, then try again.",
    # The gazette tools send the BOE nothing but a date, which the arguments already constrain.
    "get_daily_summary": "Check the date, then try again.",
    "scan_gazette": "Check the dates, then try again.",
    "verify_citation": "Check the law references, the article wordings and the ISO dates, then "
    "try again.",
    "explore_amendment_chain": "Check the identifier (for example BOE-A-2020-3824) or get it "
    "again from resolve_reference.",
}


def describe_error(error: BoeError, tool: str) -> str:
    """Describe a client error with its stable code, retryability and what to do next.

    The text is curated: it names the error code and what to do, never the API's own message.
    """
    retry = "retryable" if error.retryable else "not retryable"
    summary = _ERROR_SUMMARIES.get(error.code, "BOE request failed")
    if error.code == "invalid_query":
        advice = _INVALID_QUERY_ADVICE.get(tool, "Check the arguments, then try again.")
    else:
        advice = _ERROR_ADVICE.get(error.code, "Try again later.")
    return f"{summary} ({error.code}, {retry}): {advice}"
