"""Tests for ``boe_insights_mcp.launch``: command line, HTTP app, health and Host/Origin guard.

Everything runs in process: the HTTP app is driven through ``httpx.ASGITransport`` and no test
opens a socket or reaches boe.es.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
import respx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from boe_insights_mcp import launch
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

from .test_server import TOOL_NAMES

# A plausible public name an operator would configure behind a reverse proxy.
PUBLIC_HOST = "boe.example.org"
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


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    """A client over the respx router: any call to boe.es would fail the test."""
    return BoeClient(cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http)


def _http_settings(**changes: Any) -> Settings:
    return Settings(transport="http", **changes)


@asynccontextmanager
async def _serve(
    boe_client: BoeClient,
    settings: Settings,
    bound_to: tuple[str, int] = ("127.0.0.1", 8000),
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client wired in process to the app built for ``settings``.

    The app's lifespan is entered so MCP sessions work. A context manager rather than a
    fixture: the lifespan must be entered and left in the same task as the test body.
    """
    app = launch.build_http_app(create_server(client=boe_client), settings)

    async def bound(scope: Any, receive: Any, send: Any) -> None:
        # uvicorn puts the socket's local address in scope["server"]; the guard reads it, so a
        # test has to say which address the server is bound to (0.0.0.0 inside a container).
        if scope["type"] == "http":
            scope = {**scope, "server": bound_to}
        await app(scope, receive, send)

    transport = httpx.ASGITransport(app=bound)
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=transport, base_url="http://localhost") as http,
    ):
        yield http


# --- Command line -----------------------------------------------------------------------------


def test_defaults_are_stdio() -> None:
    assert launch.resolve_settings([], {}).transport == "stdio"


def test_flags_override_environment() -> None:
    env = {
        "BOE_INSIGHTS_TRANSPORT": "stdio",
        "BOE_INSIGHTS_HOST": "10.0.0.1",
        "BOE_INSIGHTS_PORT": "1111",
    }

    settings = launch.resolve_settings(
        ["--transport", "http", "--host", "0.0.0.0", "--port", "2"], env
    )

    assert (settings.transport, settings.host, settings.port) == ("http", "0.0.0.0", 2)


def test_environment_is_used_without_flags() -> None:
    env = {"BOE_INSIGHTS_TRANSPORT": "http", "BOE_INSIGHTS_PORT": "1111"}

    settings = launch.resolve_settings([], env)

    assert (settings.transport, settings.port) == ("http", 1111)


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        launch.resolve_settings(["--help"], {})

    assert exit_info.value.code == 0
    assert "--transport" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "env", "name"),
    [
        (["--port", "99999"], {}, "--port"),
        (["--port", "abc"], {}, "--port"),
        (["--transport", "sse"], {}, "--transport"),
        ([], {"BOE_INSIGHTS_PORT": "x"}, "BOE_INSIGHTS_PORT"),
        ([], {"BOE_INSIGHTS_TRANSPORT": "ftp"}, "BOE_INSIGHTS_TRANSPORT"),
    ],
)
def test_invalid_settings_exit_with_a_one_line_message(
    argv: list[str], env: dict[str, str], name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        launch.resolve_settings(argv, env)

    assert exit_info.value.code == 2
    message = capsys.readouterr().err.strip().splitlines()[-1]
    assert name in message
    assert "Traceback" not in message


class _Recorder:
    """Stands in for the server and records how ``launch.run`` started it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, transport: str | None = None, **kwargs: Any) -> None:
        self.calls.append((transport or "stdio", kwargs))


def test_run_defaults_to_stdio_exactly_as_before() -> None:
    recorder = _Recorder()

    launch.run(["--transport", "stdio"], {}, lambda settings: recorder)  # type: ignore[arg-type,return-value]

    assert recorder.calls == [("stdio", {"show_banner": False})]


def test_run_dispatches_to_http_with_host_port_and_guard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = _Recorder()
    env = {"BOE_INSIGHTS_ALLOWED_HOSTS": PUBLIC_HOST}

    with caplog.at_level(logging.INFO, logger="boe_insights_mcp.launch"):
        launch.run(
            ["--transport", "http", "--host", "0.0.0.0", "--port", "8123"],
            env,
            lambda settings: recorder,  # type: ignore[arg-type,return-value]
        )

    [(transport, kwargs)] = recorder.calls
    assert transport == "http"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8123
    assert kwargs["path"] == "/mcp"
    assert kwargs["host_origin_protection"] is True
    assert kwargs["allowed_hosts"] == [PUBLIC_HOST]
    assert kwargs["allowed_origins"] == []
    assert kwargs["stateless_http"] is False
    assert any("0.0.0.0" in r.message and "8123" in r.message for r in caplog.records)


# --- Health -----------------------------------------------------------------------------------


async def test_health_returns_only_status_ok(
    boe_client: BoeClient, router: respx.MockRouter
) -> None:
    async with _serve(boe_client, _http_settings()) as http:
        response = await http.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert router.calls.call_count == 0


# --- Host and Origin guard --------------------------------------------------------------------


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:9", "[::1]:8000"])
async def test_loopback_hosts_are_accepted(boe_client: BoeClient, host: str) -> None:
    async with _serve(boe_client, _http_settings()) as http:
        response = await http.get("/health", headers={"Host": host})

        assert response.status_code == 200


async def test_foreign_host_is_refused_even_when_bound_to_all_interfaces(
    boe_client: BoeClient,
) -> None:
    # Binding 0.0.0.0 gives the guard no server address to trust; only loopback names remain.
    settings = _http_settings(host="0.0.0.0")
    async with _serve(boe_client, settings, bound_to=("0.0.0.0", 8000)) as http:
        for path in ("/health", "/mcp"):
            response = await http.get(path, headers={"Host": PUBLIC_HOST})
            assert response.status_code == 421


async def test_foreign_host_cannot_reach_the_mcp_endpoint(boe_client: BoeClient) -> None:
    async with _serve(boe_client, _http_settings()) as http:
        response = await http.post(
            "/mcp", headers={**MCP_HEADERS, "Host": "evil.example"}, json=INITIALIZE
        )

        assert response.status_code == 421


async def test_operator_host_is_accepted_with_or_without_port(boe_client: BoeClient) -> None:
    async with _serve(
        boe_client,
        _http_settings(host="0.0.0.0", allowed_hosts=(PUBLIC_HOST,)),
        bound_to=("0.0.0.0", 8000),
    ) as http:
        for host in (PUBLIC_HOST, f"{PUBLIC_HOST}:8443"):
            assert (await http.get("/health", headers={"Host": host})).status_code == 200
        assert (await http.get("/health", headers={"Host": "evil.example"})).status_code == 421


async def test_foreign_origin_is_refused_on_a_loopback_host(boe_client: BoeClient) -> None:
    async with _serve(boe_client, _http_settings()) as http:
        response = await http.post(
            "/mcp",
            headers={**MCP_HEADERS, "Host": "localhost:8000", "Origin": "https://evil.example"},
            json=INITIALIZE,
        )

        assert response.status_code == 403


async def test_loopback_origin_is_accepted(boe_client: BoeClient) -> None:
    async with _serve(boe_client, _http_settings()) as http:
        response = await http.get(
            "/health", headers={"Host": "localhost:8000", "Origin": "http://localhost:3000"}
        )

        assert response.status_code == 200


async def test_operator_origin_is_accepted_only_when_listed(boe_client: BoeClient) -> None:
    settings = _http_settings(
        host="0.0.0.0",
        allowed_hosts=(PUBLIC_HOST,),
        allowed_origins=(f"https://{PUBLIC_HOST}",),
    )
    async with _serve(boe_client, settings) as http:
        headers = {"Host": PUBLIC_HOST}

        listed = await http.get("/health", headers={**headers, "Origin": f"https://{PUBLIC_HOST}"})
        unlisted = await http.get("/health", headers={**headers, "Origin": "https://evil.example"})

        assert (listed.status_code, unlisted.status_code) == (200, 403)


# --- MCP over HTTP ----------------------------------------------------------------------------


async def test_mcp_client_lists_the_tools_over_http(boe_client: BoeClient) -> None:
    app = launch.build_http_app(create_server(client=boe_client), _http_settings())

    # FastMCP's client is built on httpx2, not httpx, so the in-process transport must be too.
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

    transport = StreamableHttpTransport("http://localhost/mcp", httpx_client_factory=factory)
    async with app.lifespan(app), Client(transport) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert names == TOOL_NAMES
