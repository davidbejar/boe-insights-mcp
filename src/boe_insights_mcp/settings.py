"""Runtime configuration read from environment variables."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from boe_insights_mcp.cache import default_cache_dir

__all__ = ["CacheBackend", "Settings"]

CacheBackend = Literal["disk", "memory"]

_BACKENDS: tuple[CacheBackend, ...] = ("disk", "memory")


@dataclass(frozen=True)
class Settings:
    """Server settings; ``from_env`` reads them from ``BOE_INSIGHTS_*`` variables."""

    cache_backend: CacheBackend = "disk"
    """``BOE_INSIGHTS_CACHE``: ``disk`` (persistent) or ``memory`` (per process)."""
    cache_dir: Path = field(default_factory=default_cache_dir)
    """``BOE_INSIGHTS_CACHE_DIR``: where the disk cache lives; the platform cache dir if unset."""
    min_interval: float = 1.0
    """``BOE_INSIGHTS_MIN_INTERVAL``: minimum seconds between requests to boe.es."""
    max_concurrency: int = 2
    """``BOE_INSIGHTS_MAX_CONCURRENCY``: maximum simultaneous requests to boe.es."""

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        """Build settings from ``env``; unset or empty variables keep their defaults.

        Raises ``ValueError`` naming the variable when a value is invalid.
        """
        defaults = cls()
        cache_dir = env.get("BOE_INSIGHTS_CACHE_DIR")
        return cls(
            cache_backend=_backend(env.get("BOE_INSIGHTS_CACHE"), defaults.cache_backend),
            cache_dir=Path(cache_dir) if cache_dir else defaults.cache_dir,
            min_interval=_min_interval(env.get("BOE_INSIGHTS_MIN_INTERVAL"), defaults.min_interval),
            max_concurrency=_max_concurrency(
                env.get("BOE_INSIGHTS_MAX_CONCURRENCY"), defaults.max_concurrency
            ),
        )


def _backend(value: str | None, default: CacheBackend) -> CacheBackend:
    if not value:
        return default
    for backend in _BACKENDS:
        if value.strip().lower() == backend:
            return backend
    raise ValueError(f"BOE_INSIGHTS_CACHE must be 'disk' or 'memory', got {value!r}")


def _min_interval(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(
            f"BOE_INSIGHTS_MIN_INTERVAL must be a number of seconds >= 0, got {value!r}"
        )
    return seconds


def _max_concurrency(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"BOE_INSIGHTS_MAX_CONCURRENCY must be an integer >= 1, got {value!r}")
    return count
