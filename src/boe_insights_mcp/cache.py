"""Async cache for raw BOE HTTP response bodies.

Stores bytes keyed by strings so repeated questions do not hit the BOE API again. Two
backends share the :class:`Cache` protocol: :class:`MemoryCache` (in-process, LRU) and
:class:`DiskCache` (persistent, backed by the ``diskcache`` library). :func:`make_key` derives
a stable key from a request, and :func:`default_cache_dir` picks the on-disk location.

The cache holds raw response bodies, not parsed models. It sits below ``client.BoeClient``, the
only place that reads or writes it; ``settings`` supplies the backend choice and directory, and
``server`` builds the instance. Time-to-live lengths are decided by the client, per endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

import diskcache
from platformdirs import user_cache_dir

__all__ = [
    "Cache",
    "DiskCache",
    "MemoryCache",
    "default_cache_dir",
    "make_key",
]

_APP_NAME = "boe-insights-mcp"
_CACHE_DIR_ENV = "BOE_INSIGHTS_CACHE_DIR"


@runtime_checkable
class Cache(Protocol):
    """Asynchronous byte cache with optional per-entry time-to-live in seconds."""

    async def get(self, key: str) -> bytes | None:
        """Return the live value for ``key``, or ``None`` on a miss."""
        ...

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        """Store ``value`` under ``key``; ``ttl=None`` never expires, ``ttl<=0`` stores nothing."""
        ...

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present."""
        ...

    async def clear(self) -> None:
        """Remove every entry."""
        ...

    async def close(self) -> None:
        """Release resources. Safe to call more than once."""
        ...


class MemoryCache:
    """In-process cache with LRU eviction and per-entry expiry.

    At most ``max_entries`` live entries are kept. Both a successful ``get`` and a ``set``
    count as use; misses and reads of expired entries do not.
    """

    def __init__(
        self,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty cache; raises ``ValueError`` if ``max_entries < 1``."""
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._clock = clock
        # key -> (value, expires_at); expires_at is None for entries that never expire.
        self._entries: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()

    def _is_live(self, expires_at: float | None) -> bool:
        """Whether an entry with this expiry time is still valid."""
        return expires_at is None or self._clock() < expires_at

    async def get(self, key: str) -> bytes | None:
        """Return the live value for ``key`` and mark it as recently used."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if not self._is_live(expires_at):
            del self._entries[key]
            return None
        # The OrderedDict is kept in recency order: the oldest-used key first, the newest last.
        self._entries.move_to_end(key)
        return value

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        """Store ``value`` under ``key``, evicting the least recently used entries if full."""
        if ttl is not None and ttl <= 0:
            # "Already expired": store nothing, and drop any older value so it cannot outlive the
            # write that replaced it.
            self._entries.pop(key, None)
            return
        expires_at = None if ttl is None else self._clock() + ttl
        self._entries[key] = (value, expires_at)
        self._entries.move_to_end(key)
        if len(self._entries) > self._max_entries:
            self._evict()

    def _evict(self) -> None:
        """Shrink to ``max_entries``: expired entries go first, then the least recently used."""
        # Expired entries are only removed lazily on ``get``, so they may still occupy space;
        # dropping them first avoids throwing out a live entry to make room for a dead one.
        now = self._clock()
        expired = [
            key
            for key, (_, expires_at) in self._entries.items()
            if expires_at is not None and now >= expires_at
        ]
        for key in expired:
            del self._entries[key]
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present."""
        self._entries.pop(key, None)

    async def clear(self) -> None:
        """Remove every entry."""
        self._entries.clear()

    async def close(self) -> None:
        """Drop all entries."""
        self._entries.clear()


class DiskCache:
    """Persistent cache stored in ``directory`` via ``diskcache``.

    Blocking ``diskcache`` calls run in a worker thread so the event loop is never blocked.

    ``diskcache`` does synchronous SQLite and file I/O, which would stall every other coroutine
    (including the rate limiter's timing) for the duration of a disk read, hence
    ``asyncio.to_thread`` around each call. ``diskcache`` is itself safe to use from threads.
    It applies the expiry and the size limit (least recently stored entries are culled).
    """

    def __init__(self, directory: Path, size_limit_bytes: int = 256 * 1024 * 1024) -> None:
        """Open (and create if missing) the cache at ``directory``."""
        self._cache = diskcache.Cache(str(directory), size_limit=size_limit_bytes)

    def _read(self, key: str) -> bytes | None:
        """Blocking read of ``key``; returns ``None`` on a miss or a non-bytes value."""
        # diskcache is untyped; keep only values that are actually bytes.
        value = self._cache.get(key)
        return value if isinstance(value, bytes) else None

    async def get(self, key: str) -> bytes | None:
        """Return the live value for ``key``, or ``None`` on a miss."""
        return await asyncio.to_thread(self._read, key)

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        """Store ``value`` under ``key``; ``ttl <= 0`` removes the key instead of storing."""
        if ttl is not None and ttl <= 0:
            await self.delete(key)
            return
        await asyncio.to_thread(self._cache.set, key, value, expire=ttl)

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present."""
        await asyncio.to_thread(self._cache.delete, key)

    async def clear(self) -> None:
        """Remove every entry."""
        await asyncio.to_thread(self._cache.clear)

    async def close(self) -> None:
        """Release the underlying database files. Safe to call more than once."""
        await asyncio.to_thread(self._cache.close)


def make_key(
    url: str,
    params: Mapping[str, str | int | None] | None = None,
    accept: str = "application/json",
) -> str:
    """Return a SHA-256 hex digest identifying a request.

    Parameter order does not matter, ``None`` values are dropped, and values are compared as
    strings (so ``50`` and ``"50"`` match). The digest is taken over a JSON encoding, so name
    and value boundaries cannot collide.
    """
    # Sorting makes the key independent of dict order; JSON (rather than joining strings with a
    # separator) keeps ("a", "b=c") and ("a=b", "c") from producing the same text.
    pairs = sorted(
        [name, str(value)] for name, value in (params or {}).items() if value is not None
    )
    canonical = json.dumps([url, pairs, accept], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the cache directory: ``$BOE_INSIGHTS_CACHE_DIR`` if set, else the platform cache dir.

    ``env`` is the mapping to read the variable from (the process environment by default).
    The directory is not created.
    """
    override = (os.environ if env is None else env).get(_CACHE_DIR_ENV)
    if override:
        return Path(override)
    return Path(user_cache_dir(_APP_NAME))
