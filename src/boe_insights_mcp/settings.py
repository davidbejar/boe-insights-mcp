"""Runtime configuration read from environment variables.

``Settings`` is a frozen dataclass; ``Settings.from_env`` builds it from ``BOE_INSIGHTS_*``
variables and validates them, failing at startup with a message naming the bad variable. It is
consumed by ``server`` when the lifespan creates the cache, rate limiter and client, and it
takes its default cache directory from ``cache``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from platformdirs import user_data_dir

from boe_insights_mcp.cache import default_cache_dir

__all__ = ["CacheBackend", "Settings"]

CacheBackend = Literal["disk", "memory"]

_BACKENDS: tuple[CacheBackend, ...] = ("disk", "memory")

_APP_NAME = "boe-insights-mcp"


def default_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return ``$BOE_INSIGHTS_DATA_DIR`` if set, else the platform's per-user data directory.

    ``env`` is the mapping to read the variable from (the process environment by default), like
    ``cache.default_cache_dir``. The directory is not created.
    """
    override = (os.environ if env is None else env).get("BOE_INSIGHTS_DATA_DIR")
    return Path(override) if override else Path(user_data_dir(_APP_NAME))


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
    """``BOE_INSIGHTS_DATA_DIR`` when given, else ``None`` (the platform data dir, see
    ``data_directory``).

    Separate from ``cache_dir`` on purpose: the cache is safe to delete, saved searches are not.
    Being ``None`` when nothing was given is what lets ``data_dir_set`` be derived: a flag next
    to the path could disagree with it.
    """
    min_interval: float = 1.0
    """``BOE_INSIGHTS_MIN_INTERVAL``: minimum seconds between requests to boe.es."""
    max_concurrency: int = 2
    """``BOE_INSIGHTS_MAX_CONCURRENCY``: maximum simultaneous requests to boe.es."""

    @property
    def data_dir_set(self) -> bool:
        """Whether a data directory was given; with the memory cache and none, searches stay in
        memory too."""
        return self.data_dir is not None

    @property
    def data_directory(self) -> Path:
        """The directory saved searches live in: the given one, else the platform's."""
        # The environment was already read when ``data_dir`` was derived; an empty mapping here
        # gives the platform directory without reading it a second time.
        return self.data_dir if self.data_dir is not None else default_data_dir({})

    @property
    def saved_searches_path(self) -> Path | None:
        """Where the saved-searches file lives; ``None`` means keep them in memory."""
        if self.cache_backend == "memory" and not self.data_dir_set:
            return None
        return self.data_directory / "saved_searches.sqlite3"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        """Build settings from ``env``; unset or empty variables keep their defaults.

        Raises ``ValueError`` naming the variable when a value is invalid.
        """
        defaults = cls()
        cache_dir = env.get("BOE_INSIGHTS_CACHE_DIR")
        return cls(
            cache_backend=_backend(env.get("BOE_INSIGHTS_CACHE"), defaults.cache_backend),
            cache_dir=Path(cache_dir) if cache_dir else default_cache_dir(env),
            data_dir=_explicit_data_dir(env),
            min_interval=_min_interval(env.get("BOE_INSIGHTS_MIN_INTERVAL"), defaults.min_interval),
            max_concurrency=_max_concurrency(
                env.get("BOE_INSIGHTS_MAX_CONCURRENCY"), defaults.max_concurrency
            ),
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
