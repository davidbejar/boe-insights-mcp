"""The whole sign-in, as a client does it, with only GitHub replaced.

Register, authorize, consent, GitHub's redirect back, the token exchange and a call to ``/mcp``,
all through the real app and the real OAuth proxy. GitHub's token endpoint and its ``/user`` check
are faked in process, so nothing leaves the machine. ``login_support.sign_in`` writes proxy state
directly; this is the test that goes through the front door.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp.server.auth.oauth_proxy import upstream as oauth_upstream
from fastmcp.utilities.ui import FASTMCP_LOGO_URL

from boe_insights_mcp import launch
from boe_insights_mcp.access import AllowList

from .login_support import (
    BASE_URL,
    UpstreamGitHub,
    build,
    login_env,
    private_fastmcp_home,
    upstream,
)

__all__ = ["private_fastmcp_home", "upstream"]  # fixtures, imported for pytest

REDIRECT = "http://127.0.0.1:5555/cb"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "flow-test", "version": "1"},
    },
}


@pytest.fixture
def github_token_endpoint(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Fake GitHub's token endpoint: the code exchange returns ``handed_out["token"]``."""
    handed_out: dict[str, str] = {}

    async def fetch_token(_client: object, url: str, **_params: Any) -> dict[str, str]:
        """Stand-in for the proxy's call to GitHub; refuses any other host."""
        assert "github.com" in url
        # A refresh token is handed out so the client can use the refresh grant, as GitHub does
        # for apps that have expiring user tokens enabled.
        return {
            "access_token": handed_out["token"],
            "refresh_token": "gh-refresh",
            "token_type": "bearer",
            "scope": "",
        }

    async def refresh_token(_client: object, url: str, **_params: Any) -> dict[str, str]:
        """Stand-in for the proxy's refresh call to GitHub; hands out the ``refreshed`` token."""
        assert "github.com" in url
        return {"access_token": handed_out["refreshed"], "token_type": "bearer", "scope": ""}

    monkeypatch.setattr(oauth_upstream.AsyncOAuth2Client, "fetch_token", fetch_token)
    monkeypatch.setattr(oauth_upstream.AsyncOAuth2Client, "refresh_token", refresh_token)
    return handed_out


def stored_files(root: Path) -> set[Path]:
    """Every record file below ``root`` (the proxy's encrypted store).

    The ``-info.json`` files describe a collection and appear with its first write; they hold no
    record, so they are left out.
    """
    return {
        path for path in root.rglob("*") if path.is_file() and not path.name.endswith("-info.json")
    }


async def open_consent_page(
    http: httpx.AsyncClient,
) -> tuple[httpx.Response, str, str]:
    """Register and authorize; return ``(consent page, client_id, code_verifier)``."""
    registered = await http.post(
        "/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
    )
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    authorize = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": "client-state",
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
            "resource": f"{BASE_URL}/mcp",
        },
    )
    assert authorize.status_code == 302
    return await http.get(authorize.headers["location"]), client_id, verifier


async def begin_sign_in(
    http: httpx.AsyncClient, github: dict[str, str], upstream_token: str
) -> tuple[str, str, str]:
    """Register, authorize and consent; return ``(client_id, code_verifier, upstream_state)``.

    The next step is GitHub's redirect to ``/auth/callback`` with that state.
    """
    page, client_id, verifier = await open_consent_page(http)
    form = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page.text))
    form["action"] = "approve"
    consent = await http.post("/consent", data=form, headers={"Origin": BASE_URL})
    assert consent.status_code in (302, 303)
    state = parse_qs(urlsplit(consent.headers["location"]).query)["state"][0]
    github["token"] = upstream_token
    return client_id, verifier, state


async def post_token(http: httpx.AsyncClient, **form: str) -> httpx.Response:
    """POST to ``/token`` with ``form`` plus the resource every request here names."""
    return await http.post("/token", data={**form, "resource": f"{BASE_URL}/mcp"})


async def sign_in_up_to_token(
    http: httpx.AsyncClient,
    github: dict[str, str],
    upstream_token: str,
    home: Path,
    before_token: Callable[[], None] | None = None,
) -> tuple[httpx.Response, set[Path]]:
    """Do the whole flow for the user behind ``upstream_token``.

    ``before_token`` runs between the GitHub callback and the ``/token`` request. Returns the
    ``/token`` response and the files the proxy held just before that request.
    """
    client_id, verifier, state = await begin_sign_in(http, github, upstream_token)
    callback = await http.get("/auth/callback", params={"code": "gh-code", "state": state})
    assert callback.status_code in (302, 303)
    code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
    if before_token is not None:
        before_token()
    before = stored_files(home)
    response = await post_token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        client_id=client_id,
    )
    return response, before


async def test_the_consent_page_shows_the_project_logo_not_the_fastmcp_one(
    tmp_path: Path, private_fastmcp_home: Path, upstream: UpstreamGitHub
) -> None:
    logo = resources.files("boe_insights_mcp").joinpath("assets/logo-icon.svg").read_bytes()
    logo_data_uri = "data:image/svg+xml;base64," + base64.b64encode(logo).decode()
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        page, _, _ = await open_consent_page(http)

    assert page.status_code == 200
    assert logo_data_uri in page.text
    assert FASTMCP_LOGO_URL not in page.text
    assert "https://github.com/davidbejar/boe-insights-mcp" in page.text
    assert "boe-insights" in page.text


async def test_a_listed_user_signs_in_and_gets_a_working_session(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        token, _ = await sign_in_up_to_token(
            http, github_token_endpoint, "gho_alice", private_fastmcp_home
        )
        bearer = token.json()["access_token"]
        mcp = await http.post(
            "/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {bearer}"}, json=INITIALIZE
        )
        verified: Any = await built.auth.load_access_token(bearer)  # type: ignore[union-attr]

    assert token.status_code == 200
    assert mcp.status_code == 200
    assert "mcp-session-id" in mcp.headers
    assert (verified.claims["sub"], verified.claims["login"]) == ("1001", "alice")


async def test_the_code_exchange_still_refuses_an_unlisted_user_if_the_callback_let_them_through(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_mallory", "9999", "mallory")
    # The list admits everyone while the callback runs, then only alice: the callback check is
    # bypassed, which leaves the check in the code exchange (the second line of defence) alone.
    built, settings = build(login_env(tmp_path, BOE_INSIGHTS_ALLOWED_USERS="*"))
    provider: Any = built.auth
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        refused, before = await sign_in_up_to_token(
            http,
            github_token_endpoint,
            "gho_mallory",
            private_fastmcp_home,
            before_token=lambda: setattr(provider, "_allow_list", AllowList.parse(["alice"])),
        )
        after = stored_files(private_fastmcp_home)

    # FastMCP turns every invalid_grant into a 401 (fastmcp/server/auth/auth.py:131-148).
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"] == "invalid_grant"
    assert "access_token" not in refused.json()
    # The exchange adds no token record of any kind; the one-time code is spent, never reusable.
    assert after <= before
    assert len(after) < len(before)


async def test_an_unlisted_account_is_stopped_at_the_github_callback_with_a_403_page(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_mallory", "9999", "mallory")
    built, settings = build(login_env(tmp_path))  # the list holds only "alice"
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        _, _, state = await begin_sign_in(http, github_token_endpoint, "gho_mallory")
        before = stored_files(private_fastmcp_home)
        callback = await http.get("/auth/callback", params={"code": "gh-code", "state": state})
        after = stored_files(private_fastmcp_home)

    assert callback.status_code == 403
    assert callback.headers["content-type"].startswith("text/html")
    assert "location" not in callback.headers
    page = callback.text
    assert "not allowed" in page
    assert "allow-list" in page
    assert "id:9999" in page and "mallory" in page
    assert "alice" not in page  # who is on the list is not revealed
    assert "code=" not in page  # nothing usable is handed back
    # No authorization code and no upstream token is left behind.
    assert after <= before


async def test_the_refusal_page_escapes_the_login(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_evil", "4242", "<script>alert(1)</script>")
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        _, _, state = await begin_sign_in(http, github_token_endpoint, "gho_evil")
        callback = await http.get("/auth/callback", params={"code": "gh-code", "state": state})

    assert callback.status_code == 403
    assert "<script>alert(1)" not in callback.text
    assert "&lt;script&gt;" in callback.text


async def test_a_listed_account_still_gets_the_redirect_at_the_callback(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        _, _, state = await begin_sign_in(http, github_token_endpoint, "gho_alice")
        callback = await http.get("/auth/callback", params={"code": "gh-code", "state": state})

    assert callback.status_code in (302, 303)
    assert callback.headers["location"].startswith(REDIRECT)
    assert "code=" in callback.headers["location"]


async def refresh(http: httpx.AsyncClient, token: dict[str, Any], client_id: str) -> httpx.Response:
    """Redeem ``token``'s refresh token at ``/token``."""
    return await post_token(
        http,
        grant_type="refresh_token",
        refresh_token=token["refresh_token"],
        client_id=client_id,
    )


async def signed_in_with_refresh(
    http: httpx.AsyncClient, github: dict[str, str], home: Path
) -> tuple[dict[str, Any], str]:
    """Sign alice in; return the token response body and the client id."""
    client_id, verifier, state = await begin_sign_in(http, github, "gho_alice")
    callback = await http.get("/auth/callback", params={"code": "gh-code", "state": state})
    code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
    response = await post_token(
        http,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        client_id=client_id,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"]
    return body, client_id


async def test_a_listed_user_can_still_refresh(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    upstream.add("gho_alice2", "1001", "alice")
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        token, client_id = await signed_in_with_refresh(
            http, github_token_endpoint, private_fastmcp_home
        )
        github_token_endpoint["refreshed"] = "gho_alice2"
        refreshed = await refresh(http, token, client_id)
        mcp = await http.post(
            "/mcp",
            headers={**MCP_HEADERS, "Authorization": f"Bearer {refreshed.json()['access_token']}"},
            json=INITIALIZE,
        )

    assert refreshed.status_code == 200, refreshed.text
    assert mcp.status_code == 200


async def test_a_user_removed_from_the_list_cannot_refresh_and_the_stale_token_is_removed(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    upstream.add("gho_alice2", "1001", "alice")
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        token, client_id = await signed_in_with_refresh(
            http, github_token_endpoint, private_fastmcp_home
        )
        github_token_endpoint["refreshed"] = "gho_alice2"
        # Like restarting with another list: the stored sign-in stays, the list changes.
        provider: Any = built.auth
        provider._allow_list = AllowList.parse(["bob"])
        before = stored_files(private_fastmcp_home)
        refused = await refresh(http, token, client_id)
        after = stored_files(private_fastmcp_home)
        retried = await refresh(http, token, client_id)

    assert refused.status_code == 401, refused.text
    assert refused.json()["error"] == "invalid_grant"
    assert "access_token" not in refused.json()
    # Nothing new is stored, and the refresh token, its mapping and the upstream token are gone.
    assert after <= before
    assert len(after) < len(before)
    assert retried.status_code == 401
