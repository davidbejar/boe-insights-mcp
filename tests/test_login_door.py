"""The allow-list at the door: the real GitHub provider refuses users the list does not admit.

``server._build`` builds the real ``GitHubProvider`` (see ``tests/login_support.py`` for what is
replaced), so a request goes through the real bearer-token path: FastMCP JWT, stored upstream
token, GitHub's check, then the allow-list. A refused user gets 401 before any MCP session exists.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from boe_insights_mcp import launch
from boe_insights_mcp.settings import Settings

from . import auth_support as auth
from .login_support import (
    CODE_CHALLENGE,
    UpstreamGitHub,
    build,
    login_env,
    private_fastmcp_home,
    sign_in,
    upstream,
)

__all__ = ["private_fastmcp_home", "upstream"]  # fixtures, imported for pytest


def client(app: object) -> httpx.AsyncClient:
    """A plain HTTP client for ``app`` that speaks to its public host name."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="https://boe.example.com",
    )


async def post_initialize(http: httpx.AsyncClient, bearer: str | None) -> httpx.Response:
    """POST an MCP ``initialize`` to ``/mcp`` with ``bearer`` as the access token."""
    headers = dict(auth.MCP_HEADERS)
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return await http.post("/mcp", headers=headers, json=auth.INITIALIZE)


@pytest.fixture
def served(tmp_path: Path, private_fastmcp_home: Path, upstream: UpstreamGitHub):  # type: ignore[no-untyped-def]
    """Builds a signed-in server for ``env`` overrides and serves it in process."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def serve(**changes: str):  # type: ignore[no-untyped-def]
        env = login_env(tmp_path, BOE_INSIGHTS_ALLOWED_HOSTS="boe.example.com", **changes)
        built, settings = build(env)
        async with auth.serve(built, settings) as gateway:
            yield built, gateway

    return serve


async def test_a_listed_user_gets_a_session(served, upstream: UpstreamGitHub) -> None:  # type: ignore[no-untyped-def]
    upstream.add("gho_alice", "1001", "alice")
    async with served() as (built, gateway):
        jwt = await sign_in(built, "gho_alice")
        async with gateway.raw() as http:
            response = await post_initialize(http, jwt)

    assert response.status_code == 200


async def test_an_unlisted_user_is_refused_at_the_door(served, upstream: UpstreamGitHub) -> None:  # type: ignore[no-untyped-def]
    upstream.add("gho_mallory", "9999", "mallory")
    async with served() as (built, gateway):
        jwt = await sign_in(built, "gho_mallory")
        async with gateway.raw() as http:
            response = await post_initialize(http, jwt)
            listing = await http.post(
                "/mcp",
                headers={**auth.MCP_HEADERS, "Authorization": f"Bearer {jwt}"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )

    assert response.status_code == 401
    assert "mcp-session-id" not in response.headers
    assert listing.status_code == 401


async def test_an_unusable_subject_is_refused_even_for_everyone(
    served,  # type: ignore[no-untyped-def]
    upstream: UpstreamGitHub,
) -> None:
    upstream.add("gho_odd", "not-a-number", "alice")
    async with served(BOE_INSIGHTS_ALLOWED_USERS="*") as (built, gateway):
        jwt = await sign_in(built, "gho_odd")
        async with gateway.raw() as http:
            response = await post_initialize(http, jwt)

    assert response.status_code == 401


async def test_an_id_entry_admits_a_renamed_user(served, upstream: UpstreamGitHub) -> None:  # type: ignore[no-untyped-def]
    upstream.add("gho_renamed", "1001", "alice-renamed")
    async with served(BOE_INSIGHTS_ALLOWED_USERS="id:1001") as (built, gateway):
        jwt = await sign_in(built, "gho_renamed")
        async with gateway.raw() as http:
            response = await post_initialize(http, jwt)

    assert response.status_code == 200


@pytest.mark.parametrize("raw", ["ghp_personalaccesstoken", "gho_someotherapp"])
async def test_a_raw_github_token_is_refused(served, upstream: UpstreamGitHub, raw: str) -> None:  # type: ignore[no-untyped-def]
    # Valid for GitHub and for an allowed user, yet not a token this server issued.
    upstream.add(raw, "1001", "alice")
    async with served() as (_built, gateway), gateway.raw() as http:
        response = await post_initialize(http, raw)

    assert response.status_code == 401
    assert upstream.checked == []


async def test_a_signed_jwt_without_a_stored_token_is_refused(
    served,  # type: ignore[no-untyped-def]
    upstream: UpstreamGitHub,
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    async with served() as (built, gateway):
        forged = built.auth.jwt_issuer.issue_access_token(  # type: ignore[union-attr]
            client_id="mcp-client", scopes=[], jti="no-such-mapping", expires_in=3600
        )
        async with gateway.raw() as http:
            response = await post_initialize(http, forged)

    assert response.status_code == 401
    assert upstream.checked == []


async def test_a_username_entry_logs_the_rename_proof_form_once_and_no_token(
    served,  # type: ignore[no-untyped-def]
    upstream: UpstreamGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream.add("gho_secret_alice", "1001", "Alice")
    with caplog.at_level(logging.INFO):
        async with (
            served(BOE_INSIGHTS_ALLOWED_USERS="alice") as (built, gateway),
            gateway.raw() as http,
        ):
            for _ in range(3):
                jwt = await sign_in(built, "gho_secret_alice")
                assert (await post_initialize(http, jwt)).status_code == 200

    hints = [r for r in caplog.records if "id:1001" in r.getMessage()]
    assert len(hints) == 1
    assert hints[0].levelno == logging.INFO
    assert "Alice" in hints[0].getMessage()
    assert "gho_secret_alice" not in caplog.text


async def test_an_id_entry_logs_no_rename_hint(
    served,  # type: ignore[no-untyped-def]
    upstream: UpstreamGitHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    with caplog.at_level(logging.INFO):
        async with served(BOE_INSIGHTS_ALLOWED_USERS="id:1001") as (built, gateway):
            jwt = await sign_in(built, "gho_alice")
            async with gateway.raw() as http:
                assert (await post_initialize(http, jwt)).status_code == 200

    assert "rename" not in caplog.text.lower()


# --- The public URL is trusted by the Host/Origin guard ------------------------------------------


@pytest.mark.parametrize("origin", ["https://boe.example.com", "https://BOE.example.com"])
async def test_the_base_url_origin_reaches_the_consent_route_without_extra_settings(
    tmp_path: Path, private_fastmcp_home: Path, origin: str
) -> None:
    # Only BOE_INSIGHTS_BASE_URL is set: no ALLOWED_HOSTS, no ALLOWED_ORIGINS. Behind a TLS proxy
    # the app sees plain http, so the same-origin fallback of the guard cannot save the request.
    built, settings = build(login_env(tmp_path))
    async with (
        auth.serve(built, settings) as gateway,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://boe.example.com"
        ) as http,
    ):
        response = await http.post("/consent", headers={"Origin": origin}, data={"a": "b"})
        foreign = await http.post(
            "/consent", headers={"Origin": "https://evil.example"}, data={"a": "b"}
        )
        wrong_host = await http.post(
            "/consent", headers={"Host": "evil.example", "Origin": origin}, data={"a": "b"}
        )

    assert response.text not in ("Forbidden Origin", "Misdirected Request")
    assert (foreign.status_code, foreign.text) == (403, "Forbidden Origin")
    assert wrong_host.status_code == 421


async def test_without_login_the_base_url_adds_nothing(tmp_path: Path) -> None:
    env = login_env(tmp_path, BOE_INSIGHTS_ALLOWED_USERS="")
    for name in (
        "BOE_INSIGHTS_GITHUB_CLIENT_ID",
        "BOE_INSIGHTS_GITHUB_CLIENT_SECRET",
        "BOE_INSIGHTS_AUTH_SIGNING_KEY",
        "BOE_INSIGHTS_BASE_URL",
    ):
        del env[name]
    options = launch._http_options(Settings.from_env(env))

    assert options["allowed_hosts"] == []
    assert options["allowed_origins"] == []


# --- Which redirect URIs clients may use --------------------------------------------------------

EVIL = "https://evil.example/cb"
LOOPBACK = "http://localhost:5555/cb"


def authorize_query(client_id: str, redirect: str, **extra: str) -> dict[str, str]:
    """The query of an authorization request with PKCE."""
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": "xyz",
        "code_challenge": CODE_CHALLENGE,
        "code_challenge_method": "S256",
        **extra,
    }


async def register(http: httpx.AsyncClient, redirect: str, **extra: str) -> httpx.Response:
    """Dynamic client registration of a public client with one redirect URI."""
    body = {"redirect_uris": [redirect], "token_endpoint_auth_method": "none", **extra}
    return await http.post("/register", json=body)


@pytest.fixture
def oauth(tmp_path: Path, private_fastmcp_home: Path):  # type: ignore[no-untyped-def]
    """Serves a signed-in server (``env`` overrides) and yields a client of its OAuth routes."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def serve(**changes: str):  # type: ignore[no-untyped-def]
        built, settings = build(login_env(tmp_path, **changes))
        async with (
            auth.serve(built, settings) as gateway,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app), base_url="http://boe.example.com"
            ) as http,
        ):
            yield http

    return serve


async def test_a_client_cannot_register_a_redirect_outside_loopback(oauth) -> None:  # type: ignore[no-untyped-def]
    async with oauth() as http:
        evil = await register(http, EVIL)
        scripted = await register(http, "javascript:alert(1)")

    assert evil.status_code == 400
    assert evil.json()["error"] == "invalid_redirect_uri"
    assert scripted.status_code == 400


@pytest.mark.parametrize(
    "redirect",
    [
        "http://localhost:5555/cb",
        "http://localhost:1/anything/at/all",
        "http://127.0.0.1:49152/callback",
        "http://[::1]:8080/cb",
    ],
)
async def test_a_loopback_client_registers_and_reaches_the_consent_page(
    oauth,  # type: ignore[no-untyped-def]
    redirect: str,
) -> None:
    async with oauth() as http:
        registered = await register(http, redirect)
        response = await http.get(
            "/authorize", params=authorize_query(registered.json()["client_id"], redirect)
        )

    assert registered.status_code == 201
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://boe.example.com/consent?txn_id=")


async def test_authorizing_with_the_upstream_client_id_and_an_evil_redirect_is_refused(
    oauth,  # type: ignore[no-untyped-def]
) -> None:
    async with oauth() as http:
        response = await http.get("/authorize", params=authorize_query("Iv1.clientid", EVIL))

    assert response.status_code >= 400
    assert "evil.example" not in response.headers.get("location", "")


async def test_a_registered_loopback_client_cannot_authorize_an_evil_redirect(
    oauth,  # type: ignore[no-untyped-def]
) -> None:
    async with oauth() as http:
        registered = await register(http, LOOPBACK)
        client_id = registered.json()["client_id"]
        plain = await http.get("/authorize", params=authorize_query(client_id, EVIL))
        with_scope = await http.get(
            "/authorize", params=authorize_query(client_id, EVIL, scope="repo")
        )

    for response in (plain, with_scope):
        assert response.status_code >= 400
        assert "evil.example" not in response.headers.get("location", "")


async def test_an_operator_entry_adds_one_more_redirect(oauth) -> None:  # type: ignore[no-untyped-def]
    hosted = "https://claude.example.org/api/mcp/auth_callback"
    async with oauth(BOE_INSIGHTS_ALLOWED_REDIRECT_URIS=hosted) as http:
        allowed = await register(http, hosted)
        other = await register(http, "https://claude.example.org/other")
        loopback = await register(http, LOOPBACK)

    assert allowed.status_code == 201
    assert other.status_code == 400
    assert loopback.status_code == 201


async def test_scopes_a_client_may_send(oauth) -> None:  # type: ignore[no-untyped-def]
    """Observed behaviour with ``required_scopes=[]`` (see the comment on ``_GITHUB_SCOPES``)."""
    async with oauth() as http:
        metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
        outcomes: dict[str, tuple[int, str]] = {}
        for registered_scope in (None, "user", "read:user"):
            extra = {} if registered_scope is None else {"scope": registered_scope}
            registered = await register(http, LOOPBACK, **extra)
            outcomes[f"register scope={registered_scope}"] = (registered.status_code, "")
            if registered.status_code != 201:
                continue
            client_id = registered.json()["client_id"]
            for asked in (None, "user", "read:user"):
                extra_query = {} if asked is None else {"scope": asked}
                response = await http.get(
                    "/authorize", params=authorize_query(client_id, LOOPBACK, **extra_query)
                )
                outcomes[f"authorize scope={asked}"] = (
                    response.status_code,
                    response.headers.get("location", "")[:60],
                )

    assert metadata["scopes_supported"] == []
    assert outcomes["register scope=None"][0] == 201
    assert outcomes["register scope=user"][0] == 400
    assert outcomes["register scope=read:user"][0] == 400
    assert outcomes["authorize scope=None"][1].startswith("https://boe.example.com/consent")
    assert "error=invalid_scope" in outcomes["authorize scope=user"][1] or (
        outcomes["authorize scope=user"][0] >= 400
    )


@pytest.mark.parametrize(
    ("redirect", "accepted"),
    [
        ("https://app.example.net/oauth/done", True),
        ("https://example.net/oauth/done", False),
        ("https://app.example.net/elsewhere", False),
        ("https://app.example.net.evil.example/oauth/done", False),
        ("https://evil.example/oauth/https://app.example.net", False),
    ],
)
async def test_operator_wildcards_match_only_what_fastmcps_matcher_defines(
    oauth,  # type: ignore[no-untyped-def]
    redirect: str,
    accepted: bool,
) -> None:
    async with oauth(BOE_INSIGHTS_ALLOWED_REDIRECT_URIS="https://*.example.net/oauth/*") as http:
        response = await register(http, redirect)

    assert (response.status_code == 201) is accepted
