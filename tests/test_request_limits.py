"""Tests of ``httplimits``: the request body limit and the access log without query strings."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.types import Message, Receive, Scope, Send

from boe_insights_mcp import launch
from boe_insights_mcp.httplimits import BodyLimit, QueryStripFilter, strip_query

from . import auth_support as auth
from .login_support import UpstreamGitHub, build, login_env, private_fastmcp_home, sign_in, upstream

__all__ = ["private_fastmcp_home", "upstream"]  # fixtures, imported for pytest

LIMIT = 100


class Recorder:
    """A tiny ASGI app that reads the whole request body and says how many bytes it was."""

    def __init__(self) -> None:
        self.called = False
        self.body = b""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return
            self.body += message.get("body", b"")
            more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(len(self.body)).encode()})


def limited(app: Recorder) -> httpx.AsyncClient:
    """A client of ``app`` behind a ``BodyLimit`` of ``LIMIT`` bytes (``/mcp``: 3 * ``LIMIT``)."""
    wrapped = BodyLimit(app, mcp_path="/mcp", mcp_limit=3 * LIMIT, other_limit=LIMIT)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://x")


async def test_a_declared_length_over_the_limit_is_refused_without_reading() -> None:
    app = Recorder()
    async with limited(app) as http:
        response = await http.post("/register", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413
    assert app.called is False


async def test_a_body_at_the_limit_goes_through() -> None:
    app = Recorder()
    async with limited(app) as http:
        response = await http.post("/register", content=b"x" * LIMIT)

    assert (response.status_code, response.text) == (200, str(LIMIT))


async def test_the_mcp_path_has_its_own_larger_limit() -> None:
    app = Recorder()
    async with limited(app) as http:
        fits = await http.post("/mcp", content=b"x" * (2 * LIMIT))
        too_big = await http.post("/mcp/", content=b"x" * (3 * LIMIT + 1))

    assert fits.status_code == 200
    assert too_big.status_code == 413


async def chunks(count: int, size: int) -> AsyncIterator[bytes]:
    """``count`` chunks of ``size`` bytes, sent with no declared length."""
    for _ in range(count):
        yield b"y" * size


async def test_a_chunked_body_over_the_limit_is_refused_after_reading_only_up_to_it() -> None:
    app = Recorder()
    async with limited(app) as http:
        response = await http.post("/token", content=chunks(50, 10))

    assert response.status_code == 413
    assert app.called is False


async def test_a_chunked_body_within_the_limit_is_replayed_to_the_app() -> None:
    app = Recorder()
    async with limited(app) as http:
        response = await http.post("/token", content=chunks(5, 10))

    assert (response.status_code, response.text) == (200, "50")
    assert app.body == b"y" * 50


async def test_a_request_without_a_body_passes_and_a_bad_length_is_a_400() -> None:
    app = Recorder()
    async with limited(app) as http:
        assert (await http.get("/health")).status_code == 200
        bad = await http.post("/register", headers={"Content-Length": "12abc"}, content=b"")

    assert bad.status_code == 400


async def test_other_scopes_pass_untouched() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    wrapped = BodyLimit(app, mcp_path="/mcp")
    message: Message = {"type": "lifespan.startup"}

    async def receive() -> Message:
        return message

    async def send(_: Message) -> None:
        return None

    await wrapped({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


async def test_a_wrong_content_length_does_not_hide_a_longer_body() -> None:
    """Both framing headers: servers frame on the chunked body, so the length is a lie."""
    app = Recorder()
    wrapped = BodyLimit(app, mcp_path="/mcp", mcp_limit=3 * LIMIT, other_limit=LIMIT)
    sent = [{"type": "http.request", "body": b"z" * 60, "more_body": True}] * 5
    sent.append({"type": "http.request", "body": b"", "more_body": False})
    replies: list[Message] = []

    async def receive() -> Message:
        return sent.pop(0) if sent else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        replies.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/register",
        "headers": [(b"content-length", b"10"), (b"transfer-encoding", b"chunked")],
    }
    await wrapped(scope, receive, send)

    assert replies[0]["status"] == 413
    assert app.called is False


async def test_a_streamed_body_longer_than_declared_is_cut_and_refused() -> None:
    """No transfer encoding, but a server that delivers more than ``Content-Length`` says."""
    app = Recorder()
    wrapped = BodyLimit(app, mcp_path="/mcp", mcp_limit=3 * LIMIT, other_limit=LIMIT)
    sent = [{"type": "http.request", "body": b"z" * 60, "more_body": True}] * 5
    sent.append({"type": "http.request", "body": b"", "more_body": False})
    replies: list[Message] = []

    async def receive() -> Message:
        return sent.pop(0) if sent else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        replies.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/register",
        "headers": [(b"content-length", b"10")],
    }
    await wrapped(scope, receive, send)

    assert [m["status"] for m in replies if m["type"] == "http.response.start"] == [413]
    assert len(app.body) <= LIMIT


async def test_real_sockets_the_two_headers_do_not_get_past_the_limit(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    """Under uvicorn (h11) a body is framed as chunked when both headers are sent.

    The in-process transport cannot show HTTP/1.1 framing, so this one binds 127.0.0.1 on a free
    port. It takes well under a second.
    """
    import asyncio
    import json
    import socket

    import uvicorn

    built, settings = build(login_env(tmp_path))
    app = launch.build_http_app(built, settings)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        body = json.dumps(registration(client_name="A" * 300_000)).encode()
        chunked = (
            b"".join(
                f"{len(body[i : i + 65536]):x}\r\n".encode() + body[i : i + 65536] + b"\r\n"
                for i in range(0, len(body), 65536)
            )
            + b"0\r\n\r\n"
        )
        head = (
            f"POST /register HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 10\r\n"
            "Transfer-Encoding: chunked\r\n\r\n"
        ).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(head + chunked)
            await writer.drain()
        except OSError:
            pass  # the server may close before the whole body is sent
        status = await asyncio.wait_for(reader.readline(), 10)
        writer.close()
    finally:
        server.should_exit = True
        await task
        listener.close()

    assert b" 413 " in status
    assert files_under(private_fastmcp_home) == []


# --- Through the real server --------------------------------------------------------------------


def files_under(root: Path) -> list[Path]:
    """Every file below ``root``."""
    return sorted(path for path in root.rglob("*") if path.is_file())


def registration(**fields: Any) -> dict[str, Any]:
    """A dynamic client registration body for a loopback client."""
    return {
        "redirect_uris": ["http://localhost:5555/cb"],
        "token_endpoint_auth_method": "none",
        **fields,
    }


async def test_a_huge_registration_is_refused_and_nothing_is_stored(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    built, settings = build(login_env(tmp_path))
    async with auth.serve(built, settings) as gateway, gateway.raw() as http:
        before = files_under(private_fastmcp_home)
        huge = await http.post("/register", json=registration(client_name="A" * 1_000_000))
        after_huge = files_under(private_fastmcp_home)
        normal = await http.post("/register", json=registration(client_name="Claude Code"))
        after_normal = files_under(private_fastmcp_home)

    assert huge.status_code == 413
    assert after_huge == before
    assert normal.status_code == 201
    assert before == []
    assert after_normal != []


async def test_a_chunked_registration_over_the_limit_is_refused_too(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    built, settings = build(login_env(tmp_path))

    async def body() -> AsyncIterator[bytes]:
        yield b'{"client_name": "'
        for _ in range(200):
            yield b"A" * 1024
        yield b'"}'

    async with auth.serve(built, settings) as gateway, gateway.raw() as http:
        response = await http.post(
            "/register", content=body(), headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 413


async def test_a_normal_mcp_round_trip_still_works(
    tmp_path: Path, private_fastmcp_home: Path, upstream: UpstreamGitHub
) -> None:
    upstream.add("gho_alice", "1001", "alice")
    built, settings = build(login_env(tmp_path))
    async with auth.serve(built, settings) as gateway:
        jwt = await sign_in(built, "gho_alice")
        async with gateway.session(jwt) as client:
            tools = {tool.name for tool in await client.list_tools()}
            listing = await client.call_tool("list_saved_searches", {})

    assert "save_search" in tools
    assert listing.structured_content is not None


async def test_the_limit_is_part_of_the_app_options() -> None:
    from boe_insights_mcp.settings import Settings

    [middleware] = launch._http_options(Settings())["middleware"]  # type: ignore[misc]

    assert middleware.cls is BodyLimit


# --- Access log -----------------------------------------------------------------------------------


def access_record(target: str) -> logging.LogRecord:
    """A record shaped like the ones uvicorn's access logger emits."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", target, "1.1", 302),
        None,
    )


@pytest.mark.parametrize(
    ("target", "kept"),
    [
        ("/auth/callback?code=SECRET&state=abc", "/auth/callback"),
        ("/consent?txn_id=SECRET", "/consent"),
        ("/mcp", "/mcp"),
        ("/authorize?client_id=a?b", "/authorize"),
    ],
)
def test_the_access_log_keeps_method_path_and_status_but_not_the_query(
    target: str, kept: str
) -> None:
    record = access_record(target)

    assert QueryStripFilter().filter(record) is True
    text = record.getMessage()
    assert f'"GET {kept} HTTP/1.1" 302' in text
    assert "SECRET" not in text
    assert strip_query(target) == kept


def test_a_record_of_an_unknown_shape_with_a_query_is_dropped_not_logged() -> None:
    odd = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, "GET /consent?txn_id=SECRET", (), None
    )
    plain = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "started", (), None)

    assert QueryStripFilter().filter(odd) is False
    assert QueryStripFilter().filter(plain) is True


def test_running_over_http_installs_the_filter_once_on_the_access_logger() -> None:
    access = logging.getLogger("uvicorn.access")
    for existing in [f for f in access.filters if isinstance(f, QueryStripFilter)]:
        access.removeFilter(existing)

    class Server:
        def run(self, *args: object, **kwargs: object) -> None:
            self.kwargs = kwargs

    for _ in range(2):
        launch.run(["--transport", "http"], {}, lambda settings: Server())  # type: ignore[arg-type,return-value]

    assert len([f for f in access.filters if isinstance(f, QueryStripFilter)]) == 1
