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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastmcp.server.auth.oauth_proxy import upstream as oauth_upstream

from boe_insights_mcp import launch

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
        return {"access_token": handed_out["token"], "token_type": "bearer", "scope": ""}

    monkeypatch.setattr(oauth_upstream.AsyncOAuth2Client, "fetch_token", fetch_token)
    return handed_out


def stored_files(root: Path) -> set[Path]:
    """Every file below ``root`` (the proxy's encrypted store)."""
    return {path for path in root.rglob("*") if path.is_file()}


async def sign_in_up_to_token(
    http: httpx.AsyncClient, github: dict[str, str], upstream_token: str, home: Path
) -> tuple[httpx.Response, set[Path]]:
    """Do the whole flow for the user behind ``upstream_token``.

    Returns the ``/token`` response and the files the proxy held just before that request.
    """
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
    page = await http.get(authorize.headers["location"])
    form = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page.text))
    form["action"] = "approve"
    consent = await http.post("/consent", data=form, headers={"Origin": BASE_URL})
    assert consent.status_code in (302, 303)
    state = parse_qs(urlsplit(consent.headers["location"]).query)["state"][0]
    github["token"] = upstream_token
    callback = await http.get("/auth/callback", params={"code": "gh-code", "state": state})
    assert callback.status_code in (302, 303)
    code = parse_qs(urlsplit(callback.headers["location"]).query)["code"][0]
    before = stored_files(home)
    response = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": client_id,
            "resource": f"{BASE_URL}/mcp",
        },
    )
    return response, before


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


async def test_an_unlisted_user_is_refused_at_the_token_endpoint_and_nothing_is_stored(
    tmp_path: Path,
    private_fastmcp_home: Path,
    upstream: UpstreamGitHub,
    github_token_endpoint: dict[str, str],
) -> None:
    upstream.add("gho_mallory", "9999", "mallory")
    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        refused, before = await sign_in_up_to_token(
            http, github_token_endpoint, "gho_mallory", private_fastmcp_home
        )
        after = stored_files(private_fastmcp_home)

    # FastMCP turns every invalid_grant into a 401 (fastmcp/server/auth/auth.py:131-148).
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"] == "invalid_grant"
    assert "access_token" not in refused.json()
    # The exchange adds no token record of any kind; the one-time code is spent, never reusable.
    assert after <= before
    assert len(after) < len(before)
