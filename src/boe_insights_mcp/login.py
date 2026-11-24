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
from urllib.parse import parse_qs, urlsplit

from fastmcp.server.auth.oauth_proxy.models import _hash_token
from fastmcp.server.auth.oauth_proxy.ui import create_error_html
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.redirect_validation import DEFAULT_LOCALHOST_PATTERNS
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

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


def _refusal_page(sub: Any, login: Any) -> str:
    """The HTML shown to a GitHub account the allow-list does not admit.

    Says nothing about who is on the list. ``sub`` and ``login`` come from GitHub, so they are
    escaped and capped before they reach the page; the numeric id is shown because the operator
    needs it for an ``id:<n>`` entry.
    """
    if isinstance(sub, str | int) and str(sub):
        shown_login = f" (GitHub login: {str(login)[:100]})" if login else ""
        who = f" Ask the operator to add id:{str(sub)[:40]}{shown_login} to the allow-list."
    else:
        who = " Ask the operator to add it to the allow-list."
    message = "This GitHub account is not allowed to use this server." + who
    # create_error_html HTML-escapes the message, so GitHub-supplied text needs no escaping here.
    return create_error_html(error_title="Access not allowed", error_message=message)


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

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate tokens only for an admitted user; a removed one gets ``invalid_grant``, no state.

        The base class never looks at who the refresh token belongs to, so a user removed from the
        allow-list after signing in could keep minting tokens (they were rejected at ``/mcp``, but
        issuing them is the thing to prevent). The identity is the stored upstream token, checked
        through the same cached verifier as at the code exchange; GitHub's refresh response is not
        needed, so a refused user costs no refresh call to GitHub. When the account is known and
        not admitted, the stale refresh token, its JTI mapping and the upstream token set are
        deleted so the token cannot be retried; when GitHub cannot say who the token belongs to
        (revoked, or unreachable) the request is refused but the records stay, since that is not
        a verdict on the allow-list.
        """
        stored = await self._stored_upstream(refresh_token.token)
        if stored is not None:
            jti, upstream_token_id, upstream_access = stored
            upstream = await self._token_validator.verify_token(upstream_access)
            if upstream is None or not self._admits(upstream):
                if upstream is not None:
                    await self._refresh_token_store.delete(key=_hash_token(refresh_token.token))
                    await self._jti_mapping_store.delete(key=jti)
                    await self._upstream_token_store.delete(key=upstream_token_id)
                raise TokenError("invalid_grant", "This GitHub account is not permitted here")
        return await super().exchange_refresh_token(client, refresh_token, scopes)

    async def _stored_upstream(self, refresh_token: str) -> tuple[str, str, str] | None:
        """The ``(jti, upstream token id, upstream access token)`` behind a refresh token.

        ``None`` when the token is not one this server signed or has no stored sign-in; the base
        class then rejects it with its own ``invalid_grant``, so nothing is decided here.
        """
        try:
            payload = self.jwt_issuer.verify_token(refresh_token, expected_token_use="refresh")
            jti = payload["jti"]
        except Exception:  # any failure means "not ours"; the base class reports it
            return None
        mapping = await self._jti_mapping_store.get(key=jti)
        if mapping is None:
            return None
        upstream = await self._upstream_token_store.get(key=mapping.upstream_token_id)
        if upstream is None:
            return None
        return jti, mapping.upstream_token_id, upstream.access_token

    async def _handle_idp_callback(self, request: Request) -> HTMLResponse | RedirectResponse:
        """GitHub's redirect back: show an unlisted account a 403 page instead of a success page.

        The callback is the first point where the GitHub identity is known. The base handler is
        long and private, so it runs unchanged and its result is inspected: on success it has
        stored a one-time code and answers with a redirect to the client carrying that code. If
        the account behind the code is not admitted, the code is deleted (the transaction is
        already spent and the upstream token is only ever stored at the code exchange, so nothing
        remains) and the redirect is replaced by a plain page. Without this the person sees a
        success page and the client later fails with a 401 it cannot explain.
        ``exchange_authorization_code`` keeps its own check as a second line of defence.
        """
        response = await super()._handle_idp_callback(request)
        if not isinstance(response, RedirectResponse):
            return response
        codes = parse_qs(urlsplit(response.headers["location"]).query).get("code")
        if not codes:
            return response  # an error redirect (RFC 6749 section 4.1.2.1): no code, no identity
        code = codes[0]
        code_model = await self._code_store.get(key=code)
        if code_model is None:
            return response
        upstream = await self._token_validator.verify_token(code_model.idp_tokens["access_token"])
        if upstream is not None and self._admits(upstream):
            return response
        await self._code_store.delete(key=code)
        claims = getattr(upstream, "claims", None) or {}
        return HTMLResponse(_refusal_page(claims.get("sub"), claims.get("login")), status_code=403)

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
