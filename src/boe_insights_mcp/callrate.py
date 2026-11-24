"""A limit on how often one caller may use the server over HTTP.

This is separate from, and independent of, ``ratelimit`` (which paces the server's own requests
to boe.es): this one protects the server from the people calling it. It is FastMCP's
``RateLimitingMiddleware`` with three changes, all in :class:`CallRateLimit`: only calls that do
work are counted, the caller is identified from the access token, and the refusal is a curated
message. ``server.create_server`` adds it over HTTP only, in front of the allow-list check
(``access``), and only when ``Settings.calls_per_minute`` is above zero.
"""

from __future__ import annotations

import math
from typing import Any

from fastmcp.exceptions import PromptError, ResourceError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, RateLimitingMiddleware

from boe_insights_mcp.access import is_subject

__all__ = ["UNIDENTIFIED", "CallRateLimit", "caller_key", "refusal"]

UNIDENTIFIED = "unidentified"
"""The bucket shared by every caller without a usable GitHub id."""

# The requests that do work. Listing, ``initialize`` and ``ping`` are not counted: a typical client
# session spends about five of them before its first call, which would be a quarter of the default
# burst of 20, and they cost the server almost nothing. The value is the error class that reaches
# the caller of that kind of request.
_COUNTED: dict[str, type[Exception]] = {
    "tools/call": ToolError,
    "resources/read": ResourceError,
    "prompts/get": PromptError,
}

# Returned by the stand-in for ``call_next`` handed to FastMCP's limiter, so that ``on_request``
# can tell "admitted" from "refused" without wrapping the real call in a ``try``.
_ADMITTED = object()


def caller_key() -> str:
    """The bucket of the current request: ``github:<id>``, or ``UNIDENTIFIED``.

    A valid token whose ``sub`` is not a GitHub id, and a request with no token at all, both land
    in the one shared ``UNIDENTIFIED`` bucket: they get neither an unlimited nor a private one.
    The prefix keeps a real id from ever colliding with that name.
    """
    token = get_access_token()
    subject = (getattr(token, "claims", None) or {}).get("sub")
    return f"github:{subject}" if is_subject(subject) else UNIDENTIFIED


def refusal(calls_per_minute: int) -> str:
    """The text a refused caller reads: code, retryability and how long to wait.

    One call is back after ``60 / calls_per_minute`` seconds. It names neither the caller nor
    anything they asked for.
    """
    seconds = max(1, math.ceil(60 / calls_per_minute))
    unit = "second" if seconds == 1 else "seconds"
    return (
        "Rate limit reached (rate_limited, retryable): too many calls in a short time. "
        f"Wait at least {seconds} {unit} and try again."
    )


class CallRateLimit(RateLimitingMiddleware):
    """FastMCP's token-bucket limit, counting calls per GitHub id or in one bucket overall.

    ``by_user`` true (signed in): one bucket per :func:`caller_key`. False (open HTTP): a single
    bucket for everybody, because without a login nothing about the request is trustworthy enough
    to tell callers apart (an address or header is whatever the client says, or a proxy's).
    Buckets live in the instance, so every server has its own, and idle ones are dropped by
    FastMCP's own pruning once they would be full again.
    """

    def __init__(self, calls_per_minute: int, burst: int, *, by_user: bool) -> None:
        """Allow ``calls_per_minute`` sustained calls with bursts of ``burst`` (both >= 1)."""
        super().__init__(
            max_requests_per_second=calls_per_minute / 60,
            burst_capacity=burst,
            get_client_id=(lambda _context: caller_key()) if by_user else None,
            global_limit=not by_user,
        )
        self.calls_per_minute = calls_per_minute

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        """Count a call, or refuse it with ``refusal``; let everything else through."""
        error_class = _COUNTED.get(context.method or "")
        if error_class is None:
            return await call_next(context)

        async def admitted(context: MiddlewareContext[Any]) -> object:
            """Stands in for the rest of the chain, which runs only after the check."""
            return _ADMITTED

        try:
            await super().on_request(context, admitted)
        except RateLimitError:
            # The built-in message names the caller ("... for client: github:1001").
            raise error_class(refusal(self.calls_per_minute)) from None
        return await call_next(context)
