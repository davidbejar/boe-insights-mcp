"""Who may use a signed-in server: the allow-list of ``BOE_INSIGHTS_ALLOWED_USERS``.

``AllowList`` is pure (no I/O, no FastMCP): ``settings`` uses it to reject a bad entry at startup,
and ``server`` turns it into the check of FastMCP's ``AuthMiddleware`` with :func:`allow_check`.
It is closed by default: an empty list allows nobody.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fastmcp.utilities.authorization import AuthCheck, AuthContext

__all__ = ["VARIABLE", "AllowList", "allow_check", "is_subject"]

VARIABLE = "BOE_INSIGHTS_ALLOWED_USERS"

# Explicit ASCII classes, not ``\w``, ``\d`` or ``re.IGNORECASE``: those match look-alikes (Kelvin
# sign, long s, Arabic-Indic digits) that must never be mistaken for a listed user. A GitHub name
# is 1-39 letters, digits and single hyphens, and cannot start or end with a hyphen.
_USERNAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_MAX_USERNAME = 39
_ID_ENTRY = re.compile(r"id:[0-9]+")
_EVERYONE = "*"


def is_subject(value: object) -> bool:
    """Whether ``value`` is a usable GitHub subject: a non-empty string of ASCII digits.

    The one definition shared by the allow-list, ``owner`` (who owns a row) and ``callrate`` (whose
    bucket a call uses), so the three can never disagree about who is identified.
    """
    # ``isdigit`` alone accepts superscripts and other scripts' digits; ``isascii`` excludes them.
    return isinstance(value, str) and value.isascii() and value.isdigit()


@dataclass(frozen=True)
class AllowList:
    """The parsed allow-list; build it with :meth:`parse`."""

    everyone: bool = False
    """``*`` was listed: every user with a usable subject is allowed."""
    ids: frozenset[str] = frozenset()
    """Numeric GitHub ids (``id:<digits>`` entries), matched exactly against ``sub``."""
    names: frozenset[str] = frozenset()
    """Lower-cased GitHub usernames, matched against the ``login`` claim."""

    @classmethod
    def parse(cls, entries: Iterable[str]) -> AllowList:
        """Parse ``entries``; blank ones are ignored.

        Raises ``ValueError`` naming the variable and the first bad entry.
        """
        everyone = False
        ids: set[str] = set()
        names: set[str] = set()
        for raw in entries:
            entry = raw.strip()
            if not entry:
                continue
            if entry == _EVERYONE:
                everyone = True
            elif _ID_ENTRY.fullmatch(entry):
                ids.add(entry.removeprefix("id:"))
            elif len(entry) <= _MAX_USERNAME and _USERNAME.fullmatch(entry):
                names.add(entry.lower())
            else:
                raise ValueError(
                    f"{VARIABLE} entries must be a GitHub username, id:<number> or *, got {entry!r}"
                )
        return cls(everyone, frozenset(ids), frozenset(names))

    def allows(self, sub: Any, login: Any) -> bool:
        """Whether the user with this ``sub`` and ``login`` claim may use the server.

        Fails closed: a ``sub`` that is not a usable subject is refused whatever the list holds,
        ``*`` included. Claims come from a token, so any type is possible.
        """
        if not is_subject(sub):
            return False
        if self.everyone or sub in self.ids:
            return True
        # ``isascii`` first: ``lower()`` maps some non-ASCII characters (Kelvin sign) onto ASCII
        # letters, which would let a look-alike login pass.
        return isinstance(login, str) and login.isascii() and login.lower() in self.names

    def admitted_by_name_only(self, sub: Any, login: Any) -> bool:
        """Whether the user is allowed solely because their login is listed.

        Such an entry stops matching if the person renames their account (and could in time match
        whoever takes the old name); ``id:<n>`` does not have that problem. The caller uses this
        to say so once in the log.
        """
        return self.allows(sub, login) and not self.everyone and sub not in self.ids


def allow_check(allow_list: AllowList) -> AuthCheck:
    """The ``AuthMiddleware`` check that admits only the users ``allow_list`` allows."""

    def check(ctx: AuthContext) -> bool:
        """Allow the component when its caller's token is on the list; no token never is."""
        if ctx.token is None:
            return False
        claims = getattr(ctx.token, "claims", None) or {}
        return allow_list.allows(claims.get("sub"), claims.get("login"))

    return check
