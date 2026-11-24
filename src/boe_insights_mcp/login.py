"""The GitHub sign-in the server uses over HTTP: who gets a token, and where they may redirect to.

``server._build`` creates the provider defined here when login is configured. It is FastMCP's
``GitHubProvider`` with two additions that ``settings`` and ``access`` only describe: the
allow-list is enforced when a token is *verified* (so a refused user gets 401 before any MCP
session exists, instead of a valid session that sees nothing), and the redirect URIs that clients
may register are limited. ``AuthMiddleware`` in ``server`` stays as a second layer on the
components.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.redirect_validation import DEFAULT_LOCALHOST_PATTERNS
from mcp.server.auth.provider import AccessToken

from boe_insights_mcp.access import AllowList

__all__ = ["AllowListedGitHubProvider", "redirect_patterns"]

logger = logging.getLogger(__name__)

# What command-line MCP clients (Claude Code and others) use for their callback: RFC 8252 loopback
# redirects on a port they pick at run time. FastMCP's own list covers ``localhost`` and
# ``127.0.0.1`` (fastmcp/server/auth/redirect_validation.py:577); IPv6 loopback is added for
# clients that bind ``[::1]``. ``:*`` matches any port (same file, ``_match_port``).
LOOPBACK_REDIRECT_PATTERNS: tuple[str, ...] = (*DEFAULT_LOCALHOST_PATTERNS, "http://[::1]:*")


def redirect_patterns(extra: tuple[str, ...]) -> list[str]:
    """The redirect URI patterns clients may use: loopback ones plus the operator's ``extra``."""
    return [*LOOPBACK_REDIRECT_PATTERNS, *extra]


class AllowListedGitHubProvider(GitHubProvider):
    """``GitHubProvider`` that only recognises tokens of users the allow-list admits."""

    def __init__(self, *, allow_list: AllowList, **options: Any) -> None:
        """Create the provider; ``options`` are those of ``GitHubProvider``."""
        super().__init__(**options)
        self._allow_list = allow_list
        # Names already reported by ``_note_name_entry``; one provider lives as long as the process.
        self._named: set[tuple[str, str]] = set()

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Return the access token behind ``token``, or ``None`` unless the user is admitted.

        ``OAuthProxy.load_access_token`` is what the bearer-token middleware calls for every
        request (fastmcp/server/auth/auth.py:1127, ``verify_token``): the FastMCP JWT must be one
        this server signed, map to a stored GitHub token, and GitHub must still accept that token
        (fastmcp/server/auth/oauth_proxy/proxy.py:2258-2326). Only then are the claims checked, so a
        ``None`` here is a 401 with no session created. The claims carry GitHub's numeric id as
        ``sub`` and the login (providers/github.py, ``GitHubTokenVerifier``).
        """
        access = await super().load_access_token(token)
        if access is None:
            return None
        # The proxy declares the SDK's token type, but the verifier's FastMCP token (with claims) is
        # what it returns; a token without claims has no identity and is refused below.
        claims = getattr(access, "claims", None) or {}
        sub, login = claims.get("sub"), claims.get("login")
        if not self._allow_list.allows(sub, login):
            # Neither the token nor the identity is logged: a refusal is a normal event here.
            logger.debug("Refused a token of a user who is not on the allow-list")
            return None
        if self._allow_list.admitted_by_name_only(sub, login):
            self._note_name_entry(str(login), str(sub))
        return access

    def _note_name_entry(self, login: str, sub: str) -> None:
        """Say once per (login, id) that a username entry admitted this user, and what is safer."""
        if (login, sub) in self._named:
            return
        self._named.add((login, sub))
        logger.info(
            "GitHub user %r (id %s) is admitted by a username entry of the allow-list; "
            "id:%s is the form that survives a rename",
            login,
            sub,
            sub,
        )
