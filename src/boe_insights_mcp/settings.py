"""Runtime configuration read from environment variables.

``Settings`` is a frozen dataclass; ``Settings.from_env`` builds it from ``BOE_INSIGHTS_*``
variables and validates them, failing at startup with a message naming the bad variable. It is
consumed by ``server`` when the lifespan creates the cache, rate limiter and client, and it
takes its default cache directory from ``cache``. The login variables are validated here, and
the allow-list entries with the parser of ``access``, so a mistake stops the server at startup.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from platformdirs import user_data_dir

from boe_insights_mcp.access import AllowList
from boe_insights_mcp.cache import default_cache_dir

__all__ = ["CacheBackend", "Settings", "Transport"]

CacheBackend = Literal["disk", "memory"]
Transport = Literal["stdio", "http"]

_BACKENDS: tuple[CacheBackend, ...] = ("disk", "memory")

_TRANSPORTS: tuple[Transport, ...] = ("stdio", "http")

_APP_NAME = "boe-insights-mcp"

_CLIENT_ID_VAR = "BOE_INSIGHTS_GITHUB_CLIENT_ID"
_CLIENT_SECRET_VAR = "BOE_INSIGHTS_GITHUB_CLIENT_SECRET"
_SIGNING_KEY_VAR = "BOE_INSIGHTS_AUTH_SIGNING_KEY"
_BASE_URL_VAR = "BOE_INSIGHTS_BASE_URL"
_ALLOWED_USERS_VAR = "BOE_INSIGHTS_ALLOWED_USERS"
_ALLOWED_HOSTS_VAR = "BOE_INSIGHTS_ALLOWED_HOSTS"
_ALLOWED_ORIGINS_VAR = "BOE_INSIGHTS_ALLOWED_ORIGINS"
_ALLOWED_REDIRECTS_VAR = "BOE_INSIGHTS_ALLOWED_REDIRECT_URIS"
_CALLS_PER_MINUTE_VAR = "BOE_INSIGHTS_CALLS_PER_MINUTE"
_CALLS_BURST_VAR = "BOE_INSIGHTS_CALLS_BURST"
_MIN_SIGNING_KEY_LENGTH = 32


def platform_data_dir() -> Path:
    """Return the platform's per-user data directory for this application (not created)."""
    return Path(user_data_dir(_APP_NAME))


def _explicit_data_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """The directory ``BOE_INSIGHTS_DATA_DIR`` names, or ``None`` when it is unset or empty."""
    value = (os.environ if env is None else env).get("BOE_INSIGHTS_DATA_DIR")
    return Path(value) if value else None


@dataclass(frozen=True)
class Settings:
    """Server settings; ``from_env`` reads them from ``BOE_INSIGHTS_*`` variables."""

    cache_backend: CacheBackend = "disk"
    """``BOE_INSIGHTS_CACHE``: ``disk`` (persistent) or ``memory`` (per process)."""
    cache_dir: Path = field(default_factory=default_cache_dir)
    """``BOE_INSIGHTS_CACHE_DIR``: where the disk cache lives; the platform cache dir if unset."""
    data_dir: Path | None = field(default_factory=_explicit_data_dir)
    """``BOE_INSIGHTS_DATA_DIR`` when given, else ``None``; see ``saved_searches_dir``.

    Separate from ``cache_dir`` on purpose: the cache is safe to delete, saved searches are not.
    """
    min_interval: float = 1.0
    """``BOE_INSIGHTS_MIN_INTERVAL``: minimum seconds between requests to boe.es."""
    max_concurrency: int = 2
    """``BOE_INSIGHTS_MAX_CONCURRENCY``: maximum simultaneous requests to boe.es."""

    transport: Transport = "stdio"
    """``BOE_INSIGHTS_TRANSPORT``: ``stdio`` (default) or ``http``."""
    host: str = "127.0.0.1"
    """``BOE_INSIGHTS_HOST``: the address the HTTP transport binds to."""
    port: int = 8000
    """``BOE_INSIGHTS_PORT``: the port the HTTP transport listens on (1-65535)."""
    allowed_hosts: tuple[str, ...] = ()
    """``BOE_INSIGHTS_ALLOWED_HOSTS``: comma-separated public host names accepted over HTTP.

    Each entry is a bare host name or IP literal: no scheme, port, path or wildcard. Added to the
    loopback names, which are always accepted (and, with login on, to the host of
    ``base_url``); there is no way to switch the Host check off.
    """
    allowed_origins: tuple[str, ...] = ()
    """``BOE_INSIGHTS_ALLOWED_ORIGINS``: comma-separated browser origins accepted over HTTP.

    Each entry is exactly ``http(s)://host[:port]``, without wildcards. With login on, the origin
    of ``base_url`` is accepted too.
    """

    github_client_id: str | None = None
    """``BOE_INSIGHTS_GITHUB_CLIENT_ID``: the GitHub OAuth app that signs people in."""
    github_client_secret: str | None = field(default=None, repr=False)
    """``BOE_INSIGHTS_GITHUB_CLIENT_SECRET``: that app's secret (never shown in ``repr``)."""
    auth_signing_key: str | None = field(default=None, repr=False)
    """``BOE_INSIGHTS_AUTH_SIGNING_KEY``: key for the tokens the server issues (>= 32 characters;
    never shown in ``repr``)."""
    base_url: str | None = None
    """``BOE_INSIGHTS_BASE_URL``: the server's public URL, without path or trailing slash."""
    allowed_users: tuple[str, ...] = ()
    """``BOE_INSIGHTS_ALLOWED_USERS``: comma-separated allow-list entries (see ``access``)."""
    allowed_redirect_uris: tuple[str, ...] = ()
    """``BOE_INSIGHTS_ALLOWED_REDIRECT_URIS``: extra redirect URI patterns for OAuth clients.

    Added to the loopback ones that are always allowed (see ``login``).
    """

    calls_per_minute: int = 60
    """``BOE_INSIGHTS_CALLS_PER_MINUTE``: calls per caller per minute over HTTP; 0 is no limit."""
    calls_burst: int = 20
    """``BOE_INSIGHTS_CALLS_BURST``: how many calls a caller may make at once before waiting."""

    @property
    def login_enabled(self) -> bool:
        """Whether GitHub login is configured (all four variables; ``from_env`` allows no less)."""
        return None not in (
            self.github_client_id,
            self.github_client_secret,
            self.auth_signing_key,
            self.base_url,
        )

    @property
    def saved_searches_dir(self) -> Path | None:
        """The directory the saved searches live in; ``None`` means keep them in memory."""
        if self.data_dir is not None:
            return self.data_dir
        # The memory cache is for throwaway runs: without a directory asked for, it does not
        # leave a file in the person's data directory either.
        return None if self.cache_backend == "memory" else platform_data_dir()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        """Build settings from ``env``; unset or empty variables keep their defaults.

        Raises ``ValueError`` naming the variable when a value is invalid.
        """
        defaults = cls()
        cache_dir = env.get("BOE_INSIGHTS_CACHE_DIR")
        client_id, client_secret, signing_key, base_url = _login(env)
        allowed_users = _csv(env.get(_ALLOWED_USERS_VAR))
        # Parsed only to fail at startup on a typo; ``server`` parses it again to enforce it.
        AllowList.parse(allowed_users)
        if allowed_users and client_id is None:
            # A list nobody enforces would leave the server open to everyone while the operator
            # believes it is restricted, so it must stop the server rather than be ignored.
            raise ValueError(
                f"{_ALLOWED_USERS_VAR} is set but GitHub login is not configured, so it would "
                "restrict nothing; set the four GitHub login variables or remove it"
            )
        return cls(
            cache_backend=_backend(env.get("BOE_INSIGHTS_CACHE"), defaults.cache_backend),
            cache_dir=Path(cache_dir) if cache_dir else default_cache_dir(env),
            data_dir=_explicit_data_dir(env),
            min_interval=_min_interval(env.get("BOE_INSIGHTS_MIN_INTERVAL"), defaults.min_interval),
            max_concurrency=_max_concurrency(
                env.get("BOE_INSIGHTS_MAX_CONCURRENCY"), defaults.max_concurrency
            ),
            transport=_transport(env.get("BOE_INSIGHTS_TRANSPORT"), defaults.transport),
            host=(env.get("BOE_INSIGHTS_HOST") or "").strip() or defaults.host,
            port=_port(env.get("BOE_INSIGHTS_PORT"), defaults.port),
            allowed_hosts=tuple(_host(e) for e in _csv(env.get(_ALLOWED_HOSTS_VAR))),
            allowed_origins=tuple(_origin(e) for e in _csv(env.get(_ALLOWED_ORIGINS_VAR))),
            github_client_id=client_id,
            github_client_secret=client_secret,
            auth_signing_key=signing_key,
            base_url=base_url,
            allowed_users=allowed_users,
            allowed_redirect_uris=tuple(
                _redirect_pattern(e) for e in _csv(env.get(_ALLOWED_REDIRECTS_VAR))
            ),
            calls_per_minute=_calls_per_minute(
                env.get(_CALLS_PER_MINUTE_VAR), defaults.calls_per_minute
            ),
            calls_burst=_calls_burst(env.get(_CALLS_BURST_VAR), defaults.calls_burst),
        )


def _backend(value: str | None, default: CacheBackend) -> CacheBackend:
    """Parse ``BOE_INSIGHTS_CACHE`` (case-insensitive); unset or empty gives ``default``."""
    if not value:
        return default
    for backend in _BACKENDS:
        if value.strip().lower() == backend:
            return backend
    raise ValueError(f"BOE_INSIGHTS_CACHE must be 'disk' or 'memory', got {value!r}")


def _min_interval(value: str | None, default: float) -> float:
    """Parse ``BOE_INSIGHTS_MIN_INTERVAL`` as a finite number of seconds >= 0."""
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        # NaN fails the isfinite check below, so unparseable text and "nan"/"inf" share one error.
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(
            f"BOE_INSIGHTS_MIN_INTERVAL must be a number of seconds >= 0, got {value!r}"
        )
    return seconds


def _max_concurrency(value: str | None, default: int) -> int:
    """Parse ``BOE_INSIGHTS_MAX_CONCURRENCY`` as an integer >= 1."""
    if not value:
        return default
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"BOE_INSIGHTS_MAX_CONCURRENCY must be an integer >= 1, got {value!r}")
    return count


def _transport(value: str | None, default: Transport) -> Transport:
    """Parse ``BOE_INSIGHTS_TRANSPORT`` (case-insensitive); unset or empty gives ``default``."""
    if not value:
        return default
    for transport in _TRANSPORTS:
        if value.strip().lower() == transport:
            return transport
    raise ValueError(f"BOE_INSIGHTS_TRANSPORT must be 'stdio' or 'http', got {value!r}")


def _port(value: str | None, default: int) -> int:
    """Parse ``BOE_INSIGHTS_PORT`` as an integer from 1 to 65535."""
    if not value:
        return default
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError(f"BOE_INSIGHTS_PORT must be an integer from 1 to 65535, got {value!r}")
    return port


def _calls_per_minute(value: str | None, default: int) -> int:
    """Parse ``BOE_INSIGHTS_CALLS_PER_MINUTE`` as an integer >= 0 (0 switches the limit off)."""
    if not value:
        return default
    try:
        count = int(value)
    except ValueError:
        count = -1
    if count < 0:
        raise ValueError(f"{_CALLS_PER_MINUTE_VAR} must be an integer >= 0, got {value!r}")
    return count


def _calls_burst(value: str | None, default: int) -> int:
    """Parse ``BOE_INSIGHTS_CALLS_BURST`` as an integer >= 1."""
    if not value:
        return default
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"{_CALLS_BURST_VAR} must be an integer >= 1, got {value!r}")
    return count


def _csv(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated list, dropping blank entries; unset or empty gives ``()``."""
    return tuple(item for item in (part.strip() for part in (value or "").split(",")) if item)


def _login(env: Mapping[str, str]) -> tuple[str | None, str | None, str | None, str | None]:
    """Read and validate the four login variables: all unset (login off) or all set and valid.

    A value that is only whitespace counts as unset. Raises ``ValueError`` naming every missing
    variable when only some are set. No message here repeats a value: two of them are secrets
    and the URL may carry a password.
    """
    names = (_CLIENT_ID_VAR, _CLIENT_SECRET_VAR, _SIGNING_KEY_VAR, _BASE_URL_VAR)
    values = [(env.get(name) or "").strip() or None for name in names]
    if all(value is None for value in values):
        return None, None, None, None
    missing = [name for name, value in zip(names, values, strict=True) if value is None]
    if missing:
        # A half-configured login must never start as an open server, whatever the transport.
        raise ValueError(f"GitHub login needs all four variables; missing {', '.join(missing)}")
    client_id, client_secret, signing_key, base_url = values
    assert client_id and client_secret and signing_key and base_url  # narrowed by the check above
    if len(signing_key) < _MIN_SIGNING_KEY_LENGTH:
        raise ValueError(
            f"{_SIGNING_KEY_VAR} must be at least {_MIN_SIGNING_KEY_LENGTH} characters long"
        )
    return client_id, client_secret, signing_key, _base_url(base_url)


def _base_url(value: str) -> str:
    """Parse ``BOE_INSIGHTS_BASE_URL``: ``http(s)://host[:port]``, the trailing slash removed."""
    problem = f"{_BASE_URL_VAR} must look like https://host[:port], without path or credentials"
    parts = urlsplit(value)
    try:
        has_port_problem = parts.port is not None and not 0 < parts.port < 65536
    except ValueError:
        has_port_problem = True
    # "?" and "#" are checked on the text: ``urlsplit`` reports an empty query as no query.
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or has_port_problem
        or "@" in parts.netloc
        or parts.path not in ("", "/")
        or "?" in value
        or "#" in value
    ):
        raise ValueError(problem)
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        # Sign-in codes and tokens travel over this URL's redirects; they must not cross a
        # network in clear text. A TLS-terminating proxy still gives the public https URL.
        raise ValueError(f"{_BASE_URL_VAR} must use https unless its host is loopback")
    return value.removesuffix("/")


def _is_loopback(host: str) -> bool:
    """Whether ``host`` is ``localhost`` or a loopback IP literal (127.0.0.0/8 or ::1)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_HOST_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
_HOSTNAME = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*")


def _bare_host(entry: str) -> str | None:
    """The host of ``entry`` if it is a bare host name or IP literal, else ``None``.

    IPv6 is returned without brackets, the form that FastMCP's matcher compares with: it strips
    the brackets of both the ``Host`` header and the configured pattern
    (fastmcp/server/http.py, ``_normalize_host`` and ``_host_matches``), but would read an
    unbracketed ``::1`` or a bracketed one with a port differently, so only these two forms pass.
    """
    candidate = entry[1:-1] if entry.startswith("[") and entry.endswith("]") else entry
    if ":" in candidate:
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            return None
        return candidate
    return entry if _HOSTNAME.fullmatch(entry) else None


def _host(entry: str) -> str:
    """Validate one ``BOE_INSIGHTS_ALLOWED_HOSTS`` entry; return it (IPv6 without brackets)."""
    host = _bare_host(entry)
    if host is None:
        raise ValueError(
            f"{_ALLOWED_HOSTS_VAR} entries must be a bare host name or IP address "
            f"(no scheme, port, path or wildcard), got {entry!r}"
        )
    return host


def _origin(entry: str) -> str:
    """Validate one ``BOE_INSIGHTS_ALLOWED_ORIGINS`` entry: exactly ``http(s)://host[:port]``."""
    problem = f"{_ALLOWED_ORIGINS_VAR} entries must look like https://host[:port], got {entry!r}"
    if not re.fullmatch(r"https?://[^/?#@\s*\[\]]+|https?://\[[0-9A-Fa-f:]+\](?::[0-9]+)?", entry):
        # Anything else is a path, query, userinfo, whitespace or a wildcard.
        raise ValueError(problem)
    parts = urlsplit(entry)
    try:
        port = parts.port
    except ValueError:
        raise ValueError(problem) from None
    if (
        parts.hostname is None
        or _bare_host(parts.hostname) is None
        or (port is not None and not 0 < port < 65536)
        or entry.endswith(":")
    ):
        raise ValueError(problem)
    return entry


def _redirect_pattern(entry: str) -> str:
    """Validate one ``BOE_INSIGHTS_ALLOWED_REDIRECT_URIS`` entry; return it unchanged.

    The entry is a pattern for FastMCP's matcher (fastmcp/server/auth/redirect_validation.py,
    ``matches_allowed_pattern``), which understands a ``*`` as the whole port, a ``*.`` prefix on
    the host (subdomains only) and ``*`` inside the path. Only those are accepted here, so an
    entry cannot be made to match more than it reads like: no wildcard in the scheme or inside a
    host label, and the host must keep at least a registrable name behind ``*.``. Outside loopback
    the scheme must be https, because the authorisation code is carried in this redirect.
    """
    problem = (
        f"{_ALLOWED_REDIRECTS_VAR} entries must be absolute https URLs (http only for loopback) "
        f"such as https://app.example.org/callback, got {entry!r}"
    )
    if re.search(r"[\s?#\[\]\\@]", entry) or not re.match(r"https?://", entry):
        raise ValueError(problem)
    parts = urlsplit(entry)
    host = parts.hostname
    if host is None:
        raise ValueError(problem)
    port = parts.netloc.rpartition(":")[2] if ":" in parts.netloc else ""
    if port and port != "*" and not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        raise ValueError(problem)
    base = host[2:] if host.startswith("*.") else host
    if "*" in base or (host != base and base.count(".") < 1) or not _HOSTNAME.fullmatch(base):
        raise ValueError(problem)
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError(problem)
    return entry
