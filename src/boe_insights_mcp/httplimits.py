"""What the HTTP transport refuses or leaves out of its logs before anything else sees a request.

Two small protections, wired in by ``launch``: ``BodyLimit``, an ASGI middleware that answers 413
to a request body larger than a fixed limit, and ``QueryStripFilter``, a logging filter that keeps
query strings out of uvicorn's access log. They sit here, apart from ``launch``, because they know
nothing about the server or its settings. The sign-in routes of the OAuth proxy (``/register``,
``/authorize``, ``/token``, ``/consent``) are reachable without a token, so unlike ``/mcp`` they
must not accept an arbitrarily large body, and their query strings carry authorisation codes and
transaction ids that have no business in a log file.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["MCP_BODY_LIMIT", "OTHER_BODY_LIMIT", "BodyLimit", "QueryStripFilter", "strip_query"]

MCP_BODY_LIMIT = 1024 * 1024
"""Largest body of a request to the MCP endpoint (bytes): generous for tool arguments."""
OTHER_BODY_LIMIT = 64 * 1024
"""Largest body of any other request (bytes): sign-in forms and registrations are tiny."""


class BodyLimit:
    """Pure ASGI middleware: 413 for a request body larger than the limit.

    The limit for the MCP path is ``mcp_limit``, for every other path ``other_limit``. A declared
    ``Content-Length`` over the limit is refused without reading anything (a fast path only: the
    header is never trusted to bound the body). A request that declares a ``Transfer-Encoding`` is
    read into memory up to the limit plus one byte, so at most that much is held, and replayed to
    the application when it fits. Every other request has the bytes actually delivered counted as
    the application reads them; past the limit the application is told the client went away and
    whatever it answers is replaced by 413. Responses, streamed ones included, pass through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        mcp_path: str,
        mcp_limit: int = MCP_BODY_LIMIT,
        other_limit: int = OTHER_BODY_LIMIT,
    ) -> None:
        """Wrap ``app``; ``mcp_path`` is the endpoint that gets the larger limit."""
        self.app = app
        self.mcp_path = mcp_path
        self.mcp_limit = mcp_limit
        self.other_limit = other_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse an oversized body with 413 (or a malformed length with 400); else run the app."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.mcp_limit if scope["path"].rstrip("/") == self.mcp_path else self.other_limit
        headers = {name: value for name, value in scope["headers"]}
        declared = headers.get(b"content-length")
        if declared is not None:
            if not (declared.isascii() and declared.isdigit()):
                await PlainTextResponse("Bad Request", status_code=400)(scope, receive, send)
                return
            if int(declared) > limit:
                await self._too_large(scope, receive, send)
                return
        if b"transfer-encoding" in headers:
            # uvicorn (h11) frames a request that has both headers on the chunked body and ignores
            # the length, so the declared length proves nothing here: read what really arrives.
            buffered = await self._read_up_to(limit, receive)
            if buffered is None:
                await self._too_large(scope, receive, send)
                return
            await self.app(scope, _replay(buffered, receive), send)
            return
        await self._run_counted(limit, scope, receive, send)

    async def _run_counted(self, limit: int, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the app, counting the body bytes it reads; past ``limit`` answer 413 instead.

        The server enforces a declared length, but this does not rely on it: whatever framing the
        server used, the count is of delivered bytes. Once over, the over-long chunk is withheld
        and the app sees a disconnect, so it never gets the whole body (``Request.body`` raises),
        and anything it then sends or raises is discarded in favour of the 413.
        """
        size = 0
        exceeded = False
        started = False

        async def counting_receive() -> Message:
            """The server's next message, or a disconnect once the body is over the limit."""
            nonlocal size, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            """Forward the app's response unless the body has gone over the limit."""
            nonlocal started
            if exceeded:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            # An app that reacts to the disconnect by failing must not turn the refusal into a 500.
            if not exceeded:
                raise
        # An answer already started before the limit was passed cannot be replaced; it was cut.
        if exceeded and not started:
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _read_up_to(limit: int, receive: Receive) -> list[Message] | None:
        """The messages of a body of at most ``limit`` bytes, or ``None`` if it is longer."""
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                return messages  # the client went away; the app will see the disconnect
            size += len(message.get("body", b""))
            if size > limit:
                return None
            if not message.get("more_body", False):
                return messages

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        """Send 413 and ask the server to close the connection, since the body is not consumed."""
        response = PlainTextResponse(
            "Request Entity Too Large", status_code=413, headers={"Connection": "close"}
        )
        await response(scope, receive, send)


def _replay(messages: list[Message], receive: Receive) -> Receive:
    """A ``receive`` that yields ``messages`` first, then whatever the server sends afterwards."""
    pending = list(messages)

    async def replayed() -> Message:
        """The next buffered message, or the server's (the disconnect) once they are used up."""
        return pending.pop(0) if pending else await receive()

    return replayed


def strip_query(target: str) -> str:
    """``target`` without its query string: ``/consent?txn_id=abc`` becomes ``/consent``."""
    return target.partition("?")[0]


class QueryStripFilter(logging.Filter):
    """Logging filter for ``uvicorn.access`` that removes the query string from the request line.

    uvicorn logs ``client - "METHOD path?query HTTP/x" status`` with the full target as the third
    argument (uvicorn/protocols/http/h11_impl.py, ``access_logger.info``). The OAuth routes carry
    one-time codes and transaction ids there. Method, path and status stay. A record whose shape
    is not recognised, but whose text has a query string, is dropped rather than risked.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the path argument of ``record``; return whether to keep the record."""
        args: Any = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], strip_query(args[2]), *args[3:])
            return True
        return "?" not in record.getMessage()
