"""How the server is started: the command line, the stdio default and the opt-in HTTP transport.

``server.main`` is the console-script entry point and delegates here. This module turns the
command line and ``BOE_INSIGHTS_*`` variables into :class:`~boe_insights_mcp.settings.Settings`,
builds the server through a factory it is given (so it never imports ``server``; the factory
decides about authentication), and then starts it over stdio or HTTP, warning about login
settings that do nothing or let nobody in. For HTTP it also owns the ``/health`` route, the
Host/Origin guard configuration and the wiring of ``httplimits`` (request body limit, access log
without query strings).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from boe_insights_mcp.httplimits import BodyLimit, QueryStripFilter
from boe_insights_mcp.settings import PORT_REQUIREMENT, TRANSPORTS, Settings, port_in_range

__all__ = ["build_http_app", "register_health", "resolve_settings", "run"]

logger = logging.getLogger(__name__)

# FastMCP's default; clients and reverse proxies are configured with it, so it is fixed here.
MCP_PATH = "/mcp"

ServerFactory = Callable[[Settings], FastMCP]


def register_health(server: FastMCP) -> None:
    """Add ``GET /health`` to the server's HTTP routes (it is inert over stdio).

    The answer is a constant: no version, paths or upstream check, so it exposes nothing and
    never calls boe.es.
    """

    # custom_route bypasses MCP authentication on purpose, so load balancers can probe it;
    # the Host/Origin guard still covers it.
    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Report that the process is up."""
        return JSONResponse({"status": "ok"})


def _unique(items: Sequence[str]) -> list[str]:
    """``items`` without repeats, in order."""
    return list(dict.fromkeys(items))


def _http_options(settings: Settings) -> dict[str, object]:
    """The Host/Origin guard and middleware options, the same for ``build_http_app`` and ``run``."""
    hosts, origins = list(settings.allowed_hosts), list(settings.allowed_origins)
    if settings.login_enabled and settings.base_url:
        # The OAuth routes are reached under the public URL, usually behind a proxy that
        # terminates TLS: the app then sees http, so the guard's same-origin fallback does not
        # apply and the browser's ``Origin: https://...`` of the consent form would get 403.
        # Whoever sets the public URL means to be served there, so it is trusted without a
        # second setting.
        public = urlsplit(settings.base_url)
        if public.hostname:
            hosts.append(public.hostname)
        origins.append(f"{public.scheme}://{public.netloc}")
    return {
        # True, not "auto": "auto" only validates when bound to loopback, so binding 0.0.0.0
        # (needed in a container) would switch the check off. True validates every request
        # (fastmcp/server/http.py, HostOriginGuardMiddleware). Loopback names are always
        # allowed; matching strips any :port, so entries are plain host names.
        "host_origin_protection": True,
        "allowed_hosts": _unique(hosts),
        "allowed_origins": _unique(origins),
        # Stateful sessions are the default; pinned so a FASTMCP_STATELESS_HTTP variable cannot
        # change behaviour behind the operator's back.
        "stateless_http": False,
        # Added after FastMCP's guard and authentication middleware, which do not read bodies
        # (fastmcp/server/http.py:605-609); anything that does read one runs inside this.
        "middleware": [Middleware(BodyLimit, mcp_path=MCP_PATH)],
    }


def build_http_app(server: FastMCP, settings: Settings) -> StarletteWithLifespan:
    """Return the ASGI app serving ``server`` over HTTP with the Host/Origin guard on."""
    return server.http_app(path=MCP_PATH, **_http_options(settings))  # type: ignore[arg-type]


def _parser() -> argparse.ArgumentParser:
    """The command line parser; defaults are None so an absent flag leaves the environment."""
    parser = argparse.ArgumentParser(
        prog="boe-insights-mcp",
        description=(
            "MCP server for analytics over Spain's BOE open data. Runs over stdio unless HTTP "
            "is requested. Each flag overrides its BOE_INSIGHTS_* variable."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        type=str.lower,
        help="stdio (default) or http; overrides BOE_INSIGHTS_TRANSPORT",
    )
    parser.add_argument("--host", help="address to bind over HTTP; overrides BOE_INSIGHTS_HOST")
    parser.add_argument(
        "--port", type=int, help="port to listen on over HTTP; overrides BOE_INSIGHTS_PORT"
    )
    return parser


def resolve_settings(argv: Sequence[str], env: Mapping[str, str]) -> Settings:
    """Combine ``env`` and the command line, the flags winning.

    Exits with status 2 and a one-line message naming the variable or flag when a value is
    invalid, or with status 0 after ``--help``.
    """
    parser = _parser()
    args = parser.parse_args(list(argv))
    try:
        settings = Settings.from_env(env)
    except ValueError as error:
        parser.error(str(error))
    overrides: dict[str, object] = {}
    if args.transport is not None:
        overrides["transport"] = args.transport
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        if not port_in_range(args.port):
            parser.error(f"argument --port: {PORT_REQUIREMENT}, got {args.port}")
        overrides["port"] = args.port
    return dataclasses.replace(settings, **overrides)  # type: ignore[arg-type]


def run(argv: Sequence[str], env: Mapping[str, str], factory: ServerFactory) -> None:
    """Resolve settings, build the server with ``factory`` and start it on the chosen transport.

    stdio keeps the exact behaviour it always had; logs go to stderr so stdout carries only
    the protocol.
    """
    settings = resolve_settings(argv, env)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Warned here, not in ``Settings``: it is the launcher that knows the transport in the end
    # (``--transport`` overrides the variable). Neither message contains a value.
    if settings.login_enabled and settings.transport == "stdio":
        logger.warning(
            "GitHub login variables are ignored over stdio; it only applies with --transport http"
        )
    elif settings.login_enabled and not settings.allowed_users:
        logger.warning(
            "GitHub login is on but BOE_INSIGHTS_ALLOWED_USERS is empty: nobody can sign in "
            "until it is set"
        )
    server = factory(settings)
    if settings.transport == "stdio":
        server.run(show_banner=False)
        return
    # uvicorn logs every request line, query string included, and the OAuth routes carry one-time
    # codes there. The filter is on the logger, which uvicorn's logging setup leaves in place
    # (it replaces handlers, not filters); a duplicate would only repeat a harmless rewrite.
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QueryStripFilter) for f in access_log.filters):
        access_log.addFilter(QueryStripFilter())
    logger.info(
        "Serving MCP over HTTP on host %s, port %d, path %s",
        settings.host,
        settings.port,
        MCP_PATH,
    )
    server.run(
        transport="http",
        show_banner=False,
        host=settings.host,
        port=settings.port,
        path=MCP_PATH,
        **_http_options(settings),  # type: ignore[arg-type]
    )
