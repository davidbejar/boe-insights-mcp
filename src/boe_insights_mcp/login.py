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
from mcp.server.auth.provider import AccessToken, AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

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
    """``GitHubProvider`` that only issues and recognises tokens for users the allow-list admits."""

    def __init__(
        self, *, allow_list: AllowList, advertised_scopes: list[str], **options: Any
    ) -> None:
        """Create the provider; ``options`` are those of ``GitHubProvider``.

        ``advertised_scopes`` are the scopes clients are told they may ask for and may register
        with. They are not required of any token: ``required_scopes`` (in ``options``) stays the
        only thing the token check and the scope sent to GitHub follow.
        """
        super().__init__(**options)
        self._advertised_scopes = list(advertised_scopes)
        # Replaces the advertised, default and valid scopes that the base class derived from
        # ``required_scopes`` (oauth_proxy/proxy.py:802-822); a client that sends no scope still
        # gets ``required_scopes`` (proxy.py:1253), so nothing is asked of GitHub by default.
        self.update_default_scopes(self._advertised_scopes)
        self._allow_list = allow_list
        # Names already reported by ``_note_name_entry``; one provider lives as long as the process.
        self._named: set[tuple[str, str]] = set()

    def get_challenge_scopes(self, required_scopes: list[str] | None = None) -> list[str]:
        """The scopes the 401 challenge names: the advertised ones, not the (empty) required ones.

        The base class answers with ``required_scopes`` (fastmcp/server/auth/auth.py:398), which
        would leave the ``WWW-Authenticate`` header with no scope while the metadata lists some.
        """
        if required_scopes is None:
            return list(self._advertised_scopes)
        return required_scopes

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
        if access is None or not self._admits(access):
            return None
        return access

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Issue tokens only to an admitted user; a refused one gets ``invalid_grant``, no state.

        Checking in ``load_access_token`` alone would let a refused user complete the sign-in and
        be handed a token that then fails with 401 on every call, which clients answer by
        registering again (stored state grows with every attempt). The upstream token is checked
        here, before the proxy stores anything (``OAuthProxy.exchange_authorization_code`` writes
        the upstream token set at oauth_proxy/proxy.py:1499 and the JWTs after it). The one-time
        code is deleted so it cannot be replayed. ``invalid_grant`` because ``access_denied`` is
        not an error code the SDK's token endpoint can return (mcp/server/auth/provider.py,
        ``TokenErrorCode``); it is how an authorization code that cannot be redeemed is reported
        (RFC 6749 section 5.2).
        """
        code_model = await self._code_store.get(key=authorization_code.code)
        if code_model is not None:
            upstream = await self._token_validator.verify_token(
                code_model.idp_tokens["access_token"]
            )
            if upstream is None or not self._admits(upstream):
                await self._code_store.delete(key=authorization_code.code)
                raise TokenError("invalid_grant", "This GitHub account is not permitted here")
        return await super().exchange_authorization_code(client, authorization_code)

    def _admits(self, access: AccessToken) -> bool:
        """Whether the GitHub identity in ``access``'s claims is on the allow-list."""
        # The proxy declares the SDK's token type, but the verifier's FastMCP token (with claims) is
        # what it returns; a token without claims has no identity and is refused below.
        claims = getattr(access, "claims", None) or {}
        sub, login = claims.get("sub"), claims.get("login")
        if not self._allow_list.allows(sub, login):
            # Neither the token nor the identity is logged: a refusal is a normal event here.
            logger.debug("Refused a token of a user who is not on the allow-list")
            return False
        if self._allow_list.admitted_by_name_only(sub, login):
            self._note_name_entry(str(login), str(sub))
        return True

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
