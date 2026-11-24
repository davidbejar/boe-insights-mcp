"""Who a stored row belongs to.

Saved searches are the first data the server keeps for a person, so every row carries an owner.
The owner is derived from the authenticated request and is never a tool argument: a model (or
anyone steering it) must not be able to name another owner. Over stdio there is no identity and
everything belongs to ``LOCAL_OWNER``; over HTTP the owner is ``github:<numeric id>``, and a
request without a usable identity is refused rather than given a shared owner. ``saved_searches``
and ``searchstore`` receive the owner as a plain string; ``guard.guarded_for_owner`` is the one
caller of ``current_owner``.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token, get_http_request

from boe_insights_mcp.access import is_subject

__all__ = ["LOCAL_OWNER", "NotSignedInError", "current_owner"]

LOCAL_OWNER = "local"
"""Owner of everything when the server runs without authentication."""


class NotSignedInError(PermissionError):
    """The request carries no usable identity; ``guard`` turns it into a curated message."""


def _in_http_request() -> bool:
    """Whether the code is running inside an HTTP request to this server."""
    # ``get_http_request`` raises ``RuntimeError`` when there is no active HTTP request: over
    # stdio and with the in-memory test client neither of its two sources is set
    # (fastmcp/server/dependencies.py:588-606; the context variable is bound by
    # ``RequestContextMiddleware`` in fastmcp/server/http.py:373-413, which only the HTTP app
    # installs).
    try:
        get_http_request()
    except RuntimeError:
        return False
    return True


def current_owner(*, remote: bool = False) -> str:
    """Return the owner of the current request: ``local``, or ``github:<id>`` when ``remote``.

    ``remote`` is true whenever the server runs over HTTP, signed in or not, and then the answer
    is never ``local``: that owner is for the person at their own terminal, and handing it to a
    network caller would let strangers share it. A request that arrives over HTTP counts as
    remote even when ``remote`` is false (a server built without settings but served over HTTP):
    the transport, not a flag set at build time, has the last word.

    Raises ``NotSignedInError`` (a ``PermissionError``) over HTTP when there is no token or its
    ``sub`` claim is not a non-empty string of ASCII digits (GitHub's numeric id). The id, not
    the renameable login, names the owner; anything else could collide with another user's data
    or be mistaken for it in logs and files. The message never repeats the subject.
    """
    if not remote and not _in_http_request():
        return LOCAL_OWNER
    token = get_access_token()
    if token is None:
        raise NotSignedInError("no access token; cannot tell who owns data")
    subject = (getattr(token, "claims", None) or {}).get("sub")
    if not is_subject(subject):
        raise NotSignedInError("the access token has no usable subject; cannot tell who owns data")
    return f"github:{subject}"
