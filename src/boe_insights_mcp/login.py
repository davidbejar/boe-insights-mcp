"""The GitHub sign-in the server uses over HTTP: who gets a token, and where they may redirect to.

``server._build`` creates the provider defined here when login is configured. It is FastMCP's
``GitHubProvider`` with two additions that ``settings`` and ``access`` only describe: the
allow-list is enforced when a token is *verified* (so a refused user gets 401 before any MCP
session exists, instead of a valid session that sees nothing), and the redirect URIs that clients
may register are limited. ``AuthMiddleware`` in ``server`` stays as a second layer on the
components.

Before the proxy is built, ``make_token_store_private`` makes the folder where FastMCP keeps the
encrypted sign-in records private to the server's user (mode 0700), and refuses to start when that
folder is a symbolic link or not a folder; the refusal is a ``launch.StartupError``, which
``launch.run`` reports in one line.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

import fastmcp
from fastmcp.server.auth.oauth_proxy.models import _hash_token
from fastmcp.server.auth.oauth_proxy.ui import create_error_html
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.redirect_validation import DEFAULT_LOCALHOST_PATTERNS
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from boe_insights_mcp.access import AllowList
from boe_insights_mcp.launch import StartupError

__all__ = [
    "AllowListedGitHubProvider",
    "TokenStoreError",
    "make_token_store_private",
    "redirect_patterns",
]

logger = logging.getLogger(__name__)

# When no ``client_storage`` is given, the OAuth proxy keeps its encrypted records (upstream GitHub
# tokens, client registrations) in ``settings.home / "oauth-proxy" / <key fingerprint>``, creating
# it with ``mkdir(parents=True, exist_ok=True)`` and so with the default mode
# (fastmcp/server/auth/oauth_proxy/proxy.py:615-623). Only the folder name is repeated here; the
# home is read from the same ``fastmcp.settings`` object the proxy imports (proxy.py:75), so a
# ``FASTMCP_HOME`` variable or ``.env`` entry moves both together.
TOKEN_STORE_FOLDER = "oauth-proxy"

_PRIVATE_MODE = 0o700

# POSIX permission bits mean nothing on Windows (``chmod`` there only toggles read-only), so the
# store is left to FastMCP and to the folder's ACLs.
_POSIX_MODES = os.name == "posix"


class TokenStoreError(StartupError):
    """The sign-in token store's folder is not one the server will keep secrets in."""


def make_token_store_private(home: Path) -> Path | None:
    """Make ``home / "oauth-proxy"`` a folder only its owner can enter, before FastMCP uses it.

    A missing store is created, with any missing parents, with mode 0700 whatever the umask. An
    existing one with group or other permission bits is set to 0700, and so are the fingerprint
    folders directly inside it; a pre-existing ``home`` and everything above it keep their modes,
    since they may be shared with other programs. A folder that cannot be tightened for lack of
    permission is reported with one WARNING and left as it is.

    Returns the store's path, or ``None`` on Windows, where nothing is done. Raises
    ``TokenStoreError`` when the store is a symbolic link or exists but is not a folder, or when it
    cannot be created.
    """
    if not _POSIX_MODES:
        return None
    store = home / TOKEN_STORE_FOLDER
    # Checked before anything else and without following the link: ``exists()`` would follow it
    # (a dangling one looks missing) and ``chmod`` would change whatever it points at. A store
    # that silently lives somewhere else is not something to trust with sign-in secrets.
    if store.is_symlink():
        raise TokenStoreError(
            f"the sign-in token store {store} is a symbolic link; refusing to keep sign-in "
            "secrets behind it. Replace it with a real folder, or set FASTMCP_HOME elsewhere"
        )
    if not store.exists():
        _create_private(store)
        return store
    # Refused rather than moved or deleted: the file is not ours to remove, and FastMCP would
    # fail on it anyway, with a traceback instead of this message.
    if not store.is_dir():
        raise TokenStoreError(
            f"the sign-in token store {store} exists but is not a directory; move it away, or "
            "set FASTMCP_HOME elsewhere"
        )
    _tighten(store)
    return store


def _create_private(store: Path) -> None:
    """Create ``store`` and its missing parents, each with mode 0700; ``TokenStoreError`` if not.

    Raises ``TokenStoreError`` naming the store when a folder cannot be created.
    """
    # As ``searchstore._make_private_directories``: ``mkdir(parents=True, mode=...)`` gives the
    # mode to the last folder only and the umask narrows it, so each missing folder is created and
    # set explicitly. ``lexists`` so that a dangling symlink among the parents counts as present
    # (mkdir then fails and is reported) rather than as missing.
    missing: list[Path] = []
    current = store
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    try:
        for created in reversed(missing):
            created.mkdir(mode=_PRIVATE_MODE)
            created.chmod(_PRIVATE_MODE)
    except OSError as error:
        raise TokenStoreError(
            f"cannot create the sign-in token store {store}: {error.strerror or error}"
        ) from error
    # FastMCP then creates the fingerprint folder (and the store creates one per collection)
    # inside with the default mode, typically 0755. Those are not patched: nobody but the owner
    # can enter a 0700 folder, so nobody else reaches anything below it whatever its mode, and
    # the record files themselves are written 0600 (checked with FastMCP 4.2.4).


def _tighten(store: Path) -> None:
    """Set an existing ``store`` and the fingerprint folders inside it to 0700 when wider.

    Symlinked entries are skipped, never followed. Folders that cannot be changed for lack of
    permission are named in one WARNING; the server still starts.
    """
    refused = [] if _restrict(store) else [str(store)]
    inside: list[Path] = []
    # An unreadable store (another account's, or one its owner closed to itself) is already in
    # ``refused`` if others could enter it; otherwise there is nothing to tighten for their sake,
    # and FastMCP fails when it tries to use it, which says more than we could.
    with contextlib.suppress(PermissionError):
        inside = sorted(
            entry for entry in store.iterdir() if not entry.is_symlink() and entry.is_dir()
        )
    refused += [str(folder) for folder in inside if not _restrict(folder)]
    if refused:
        # A warning, not a refusal, like the open-HTTP one in ``launch.run``: the folder may
        # belong to another account on purpose (a service user, a shared volume), and only the
        # operator can tell.
        logger.warning(
            "Could not make the sign-in token store private (mode 0700): %s. Other users of "
            "this machine may be able to list it; give it to the server's user or chmod 700 it",
            ", ".join(refused),
        )


def _restrict(folder: Path) -> bool:
    """Set ``folder`` to 0700 if group or others have any access; False if not permitted."""
    # Only access by others is a reason to change it: a folder already closed to them is left
    # as it is, so a store owned by someone else but already private causes no warning.
    if not stat.S_IMODE(folder.lstat().st_mode) & 0o077:
        return True
    try:
        # A symlink swapped in between the checks and this call would be followed; doing that
        # needs write access to the parent, which is the home or the 0700 store itself.
        folder.chmod(_PRIVATE_MODE)
    except PermissionError:
        return False
    return True


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


class _Screened(NamedTuple):
    """What ``AllowListedGitHubProvider._screen_code`` found for a stored one-time code."""

    admitted: bool
    """Whether the allow-list admits the account; when not, the code has been deleted."""
    upstream: AccessToken | None
    """The verifier's token for the code's upstream token, with GitHub's claims.

    ``None`` when the verifier returned no token: GitHub rejected the credentials, required
    scopes are missing, or verification failed unexpectedly. An unreachable or rate-limited
    GitHub raises ``TokenVerificationError`` instead, so it never produces a verdict.
    """


class AllowListedGitHubProvider(GitHubProvider):
    """``GitHubProvider`` that only issues and recognises tokens for users the allow-list admits."""

    def __init__(
        self, *, allow_list: AllowList, advertised_scopes: list[str], **options: Any
    ) -> None:
        """Create the provider; ``options`` are those of ``GitHubProvider``.

        ``advertised_scopes`` are the scopes clients are told they may ask for and may register
        with. They are not required of any token: ``required_scopes`` (in ``options``) stays the
        only thing the token check and the scope sent to GitHub follow.

        Unless ``options`` give a ``client_storage``, the token store folder is made private
        first (``make_token_store_private``), which may raise ``TokenStoreError``.
        """
        # Before ``super().__init__``, which creates the store folder: with our 0700 folder already
        # there, FastMCP's ``mkdir(exist_ok=True)`` leaves its mode alone. A caller-supplied
        # storage means FastMCP never touches the folder, so neither do we.
        if options.get("client_storage") is None:
            make_token_store_private(fastmcp.settings.home)
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
        screened = await self._screen_code(authorization_code.code)
        if screened is not None and not screened.admitted:
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
        screened = await self._screen_code(codes[0])
        if screened is None or screened.admitted:
            return response
        claims = getattr(screened.upstream, "claims", None) or {}
        return HTMLResponse(_refusal_page(claims.get("sub"), claims.get("login")), status_code=403)

    async def _screen_code(self, code: str) -> _Screened | None:
        """Decide whether the account behind the stored one-time ``code`` is admitted.

        Returns ``None`` when no such code is stored (nothing is decided or deleted), otherwise
        the verdict with GitHub's answer for the code's token. A refused code is deleted before
        returning. Shared by the callback and the code exchange, which differ only in how they
        refuse. A ``TokenVerificationError`` from the verifier propagates and the code stays.
        """
        code_model = await self._code_store.get(key=code)
        if code_model is None:
            return None
        upstream = await self._token_validator.verify_token(code_model.idp_tokens["access_token"])
        # ``_admits`` only sees a real answer, so an unverifiable token is refused without the
        # "not on the allow-list" log line: it is not a verdict on the list.
        if upstream is not None and self._admits(upstream):
            return _Screened(admitted=True, upstream=upstream)
        await self._code_store.delete(key=code)
        return _Screened(admitted=False, upstream=upstream)

    def _admits(self, access: AccessToken) -> bool:
        """Whether the GitHub identity in ``access``'s claims is on the allow-list."""
        if not self._allow_list.allows_token(access):
            # Neither the token nor the identity is logged: a refusal is a normal event here.
            logger.debug("Refused a token of a user who is not on the allow-list")
            return False
        # The proxy declares the SDK's token type, but the verifier returns FastMCP's token, whose
        # claims carry GitHub's id (``sub``) and ``login``.
        claims = getattr(access, "claims", None) or {}
        sub, login = claims.get("sub"), claims.get("login")
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
