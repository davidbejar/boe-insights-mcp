"""Who a stored row belongs to.

Saved searches are the first data the server keeps for a person, so every row carries an owner.
The owner is derived from the authenticated request and is never a tool argument: a model (or
anyone steering it) must not be able to name another owner. Without authentication, which is
the stdio case, everything belongs to ``LOCAL_OWNER``. ``saved_searches`` and ``searchstore``
receive the owner as a plain string; ``server`` is the one caller of ``current_owner``.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token

__all__ = ["LOCAL_OWNER", "current_owner"]

LOCAL_OWNER = "local"
"""Owner of everything when the server runs without authentication."""


def current_owner() -> str:
    """Return the owner of the current request: ``local`` or ``user:<sub>``.

    Raises ``PermissionError`` for a token without a non-empty ``sub`` claim: falling back to a
    shared owner would let two users read each other's searches.
    """
    token = get_access_token()
    if token is None:
        return LOCAL_OWNER
    claims = getattr(token, "claims", None) or {}
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise PermissionError("the access token has no subject; cannot tell who owns the data")
    return f"user:{subject}"
