"""Helpers for the login tests: a signing key pair, a JWT verifier standing in for GitHub, and an
in-process HTTP gateway to a server.

No test using these helpers touches the network: tokens are signed here and checked against the
matching public key, and requests go through ``httpx2.ASGITransport`` straight into the ASGI app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.http import StarletteWithLifespan

from boe_insights_mcp import launch
from boe_insights_mcp.settings import Settings

ISSUER = "https://github.test"
AUDIENCE = "boe-insights-test"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def new_keys() -> RSAKeyPair:
    """A fresh RSA key pair; the private half signs tokens, the public half verifies them."""
    return RSAKeyPair.generate()


def verifier_for(keys: RSAKeyPair) -> JWTVerifier:
    """A token verifier that accepts what ``token`` signs with ``keys``: the GitHub stand-in."""
    return JWTVerifier(public_key=keys.public_key, issuer=ISSUER, audience=AUDIENCE)


def token(
    keys: RSAKeyPair,
    sub: Any = "1001",
    login: Any = "alice",
    **claims: Any,
) -> str:
    """A signed token whose claims look like GitHub's: ``sub`` is the id, ``login`` the name.

    ``sub`` and ``login`` may be any JSON value, ``None`` (a JSON null) included, so a test can
    forge the odd tokens an upstream should never send; other claims go in ``claims``.
    """
    extra: dict[str, Any] = {"login": login, **claims}
    return keys.create_token(
        subject=sub if isinstance(sub, str) else "placeholder",
        issuer=ISSUER,
        audience=AUDIENCE,
        additional_claims={"sub": sub, **extra},
    )


class Gateway:
    """An HTTP app served in process; opens MCP sessions and raw requests against it."""

    def __init__(self, app: StarletteWithLifespan) -> None:
        self.app = app

    def raw(self, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        """A plain HTTP client for the app (to look at status codes)."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://localhost",
            headers=headers,
        )

    def session(self, bearer: str | None = None) -> Client[Any]:
        """An MCP client of the app, sending ``bearer`` as its access token when given."""
        app = self.app

        # FastMCP's client is built on httpx2, not httpx (see tests/test_launch.py).
        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            **extra: Any,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                headers=headers,
                timeout=timeout,
                auth=auth,
                **extra,
            )

        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else None
        return Client(
            StreamableHttpTransport(
                "http://localhost/mcp", headers=headers, httpx_client_factory=factory
            )
        )


@asynccontextmanager
async def serve(server: FastMCP, settings: Settings) -> AsyncIterator[Gateway]:
    """Serve ``server`` over HTTP in process, with the app's lifespan entered.

    A context manager rather than a fixture: the lifespan must be entered and left in the same
    task as the test body (see ``tests/test_launch.py``).
    """
    app = launch.build_http_app(server, settings)
    async with app.lifespan(app):
        yield Gateway(app)


@asynccontextmanager
async def session(
    server: FastMCP, settings: Settings, bearer: str | None = None
) -> AsyncIterator[Client[Any]]:
    """Serve ``server`` in process and yield one connected MCP client sending ``bearer``."""
    async with serve(server, settings) as gateway, gateway.session(bearer) as client:
        yield client
