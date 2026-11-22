"""Who a stored row belongs to.

Saved searches are the first data the server keeps for a person, so every row carries an owner.
The owner is derived from the authenticated request and is never a tool argument: a model (or
anyone steering it) must not be able to name another owner. Without authentication, which is
the stdio case, everything belongs to ``LOCAL_OWNER``. ``saved_searches`` and ``searchstore``
receive the owner as a plain string; ``server`` is the one caller of ``current_owner``.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token

__all__ = ["LOCAL_OWNER", "NotSignedInError", "current_owner"]

LOCAL_OWNER = "local"
"""Owner of everything when the server runs without authentication."""


class NotSignedInError(PermissionError):
    """The request carries no usable identity; ``guard`` turns it into a curated message."""


def current_owner() -> str:
    """Return the owner of the current request: ``local`` or ``user:<sub>``.

    Raises ``NotSignedInError`` (a ``PermissionError``) for a token whose ``sub`` claim is
    missing, blank, padded with whitespace or holds control characters: falling back to a shared
    owner would let two users read each other's searches, and a subject that is only
    whitespace-different from another could be mistaken for it in logs and files.
    """
    token = get_access_token()
    if token is None:
        return LOCAL_OWNER
    claims = getattr(token, "claims", None) or {}
    subject = claims.get("sub")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or subject != subject.strip()
        or not subject.isprintable()
    ):
        raise NotSignedInError("the access token has no usable subject; cannot tell who owns data")
    return f"user:{subject}"
