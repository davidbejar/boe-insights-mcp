"""Helpers for tests that drive the real ``server._build`` with the real GitHub OAuth proxy.

Nothing here reaches the network: the one call the provider makes to GitHub's API (checking an
upstream token) is replaced by a table of known tokens, and signed-in sessions are created by
storing the upstream token the way the OAuth proxy does after a real sign-in and issuing the
matching FastMCP JWT. Requests go straight into the ASGI app.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import fastmcp
import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, UpstreamTokenSet
from fastmcp.server.auth.providers import github

from boe_insights_mcp import server
from boe_insights_mcp.settings import Settings

BASE_URL = "https://boe.example.com"
CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def login_env(tmp_path: Path, **changes: str) -> dict[str, str]:
    """Environment of a signed-in HTTP server whose files all live under ``tmp_path``."""
    env = {
        "BOE_INSIGHTS_TRANSPORT": "http",
        "BOE_INSIGHTS_CACHE": "memory",
        "BOE_INSIGHTS_GITHUB_CLIENT_ID": "Iv1.clientid",
        "BOE_INSIGHTS_GITHUB_CLIENT_SECRET": "client-secret-VALUE-1234",
        "BOE_INSIGHTS_AUTH_SIGNING_KEY": "k" * 32,
        "BOE_INSIGHTS_BASE_URL": BASE_URL,
        "BOE_INSIGHTS_ALLOWED_USERS": "alice",
        "BOE_INSIGHTS_DATA_DIR": str(tmp_path / "data"),
        "BOE_INSIGHTS_CACHE_DIR": str(tmp_path / "cache"),
    }
    env.update(changes)
    return env


class UpstreamGitHub:
    """Stands in for ``api.github.com/user``: knows which upstream token belongs to whom."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.checked: list[str] = []

    def add(self, token: str, sub: Any, login: Any) -> None:
        """Make ``token`` valid upstream for the account ``sub`` / ``login``."""
        self.accounts[token] = {"sub": sub, "login": login}

    async def verify(self, token: str) -> AccessToken | None:
        """The replacement of ``GitHubTokenVerifier.verify_token`` (token in, claims out)."""
        self.checked.append(token)
        claims = self.accounts.get(token)
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims["sub"]),
            scopes=[],
            expires_at=None,
            subject=str(claims["sub"]),
            claims=dict(claims),
        )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> UpstreamGitHub:
    """Replace GitHub's token check by a table; no request leaves the process."""
    fake = UpstreamGitHub()

    async def verify_token(_verifier: object, token: str) -> AccessToken | None:
        """Class-level stand-in: drops the verifier instance and asks the table."""
        return await fake.verify(token)

    monkeypatch.setattr(github.GitHubTokenVerifier, "verify_token", verify_token)
    return fake


@pytest.fixture
def private_fastmcp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the OAuth proxy's encrypted store (under FastMCP's home) inside ``tmp_path``."""
    home = tmp_path / "fastmcp-home"
    monkeypatch.setattr(fastmcp.settings, "home", home)
    return home


def build(env: dict[str, str]) -> tuple[FastMCP, Settings]:
    """The server ``main`` would build for ``env``, with the real GitHub provider."""
    settings = Settings.from_env(env)
    return server._build(settings), settings


async def sign_in(built: FastMCP, upstream_token: str, *, ttl: int = 3600) -> str:
    """Store ``upstream_token`` behind a fresh FastMCP JWT as a completed sign-in would; return it.

    Mirrors what the proxy's token exchange writes (upstream token set + JTI mapping), so the
    JWT goes through the provider's real ``load_access_token``.
    """
    provider: Any = built.auth
    jti = uuid.uuid4().hex
    token_id = uuid.uuid4().hex
    now = time.time()
    await provider._upstream_token_store.put(
        key=token_id,
        value=UpstreamTokenSet(
            upstream_token_id=token_id,
            access_token=upstream_token,
            refresh_token=None,
            refresh_token_expires_at=None,
            expires_at=now + ttl,
            token_type="Bearer",
            scope="",
            client_id="mcp-client",
            created_at=now,
        ),
        ttl=ttl,
    )
    await provider._jti_mapping_store.put(
        key=jti,
        value=JTIMapping(jti=jti, upstream_token_id=token_id, created_at=now),
        ttl=ttl,
    )
    return provider.jwt_issuer.issue_access_token(
        client_id="mcp-client", scopes=[], jti=jti, expires_in=ttl
    )
