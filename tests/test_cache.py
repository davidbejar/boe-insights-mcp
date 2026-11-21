"""Contract tests for ``boe_insights_mcp.cache``."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from platformdirs import user_cache_dir

from boe_insights_mcp.cache import (
    Cache,
    DiskCache,
    MemoryCache,
    default_cache_dir,
    make_key,
)


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=["memory", "disk"])
async def cache(
    request: pytest.FixtureRequest, clock: FakeClock, tmp_path: Path
) -> AsyncIterator[Cache]:
    backend: Cache
    if request.param == "memory":
        backend = MemoryCache(clock=clock)
    else:
        backend = DiskCache(tmp_path / "cache")
    yield backend
    await backend.close()


# --- Shared behaviour (both backends) -------------------------------------------------


async def test_satisfies_cache_protocol(cache: Cache) -> None:
    assert isinstance(cache, Cache)


async def test_get_unknown_key_returns_none(cache: Cache) -> None:
    assert await cache.get("missing") is None


async def test_set_then_get_returns_same_bytes(cache: Cache) -> None:
    await cache.set("k", b"hello")
    assert await cache.get("k") == b"hello"


async def test_set_overwrites_existing_value(cache: Cache) -> None:
    await cache.set("k", b"one")
    await cache.set("k", b"two")
    assert await cache.get("k") == b"two"


async def test_keys_are_independent(cache: Cache) -> None:
    await cache.set("a", b"1")
    await cache.set("b", b"2")
    assert await cache.get("a") == b"1"
    assert await cache.get("b") == b"2"


async def test_empty_bytes_is_distinguishable_from_miss(cache: Cache) -> None:
    await cache.set("k", b"")
    assert await cache.get("k") == b""


async def test_non_utf8_bytes_round_trip(cache: Cache) -> None:
    payload = bytes(range(256))
    await cache.set("k", payload)
    assert await cache.get("k") == payload


async def test_large_value_round_trips(cache: Cache) -> None:
    payload = b"x" * 1_000_000
    await cache.set("k", payload)
    assert await cache.get("k") == payload


async def test_ttl_none_never_expires(cache: Cache, clock: FakeClock) -> None:
    await cache.set("k", b"v", ttl=None)
    clock.advance(10**9)
    assert await cache.get("k") == b"v"


async def test_zero_ttl_is_not_stored(cache: Cache) -> None:
    await cache.set("k", b"v", ttl=0)
    assert await cache.get("k") is None


async def test_negative_ttl_is_not_stored(cache: Cache) -> None:
    await cache.set("k", b"v", ttl=-5)
    assert await cache.get("k") is None


async def test_non_positive_ttl_removes_existing_value(cache: Cache) -> None:
    await cache.set("k", b"old")
    await cache.set("k", b"new", ttl=0)
    assert await cache.get("k") is None


async def test_set_without_ttl_clears_previous_ttl(cache: Cache) -> None:
    await cache.set("k", b"v1", ttl=0.03)
    await cache.set("k", b"v2")
    await asyncio.sleep(0.06)
    assert await cache.get("k") == b"v2"


async def test_delete_removes_entry(cache: Cache) -> None:
    await cache.set("k", b"v")
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_delete_missing_key_is_noop(cache: Cache) -> None:
    await cache.delete("nope")
    assert await cache.get("nope") is None


async def test_delete_leaves_other_keys(cache: Cache) -> None:
    await cache.set("a", b"1")
    await cache.set("b", b"2")
    await cache.delete("a")
    assert await cache.get("b") == b"2"


async def test_clear_removes_everything(cache: Cache) -> None:
    await cache.set("a", b"1")
    await cache.set("b", b"2")
    await cache.clear()
    assert await cache.get("a") is None
    assert await cache.get("b") is None


async def test_clear_on_empty_cache_is_noop(cache: Cache) -> None:
    await cache.clear()
    assert await cache.get("a") is None


async def test_close_is_idempotent(cache: Cache) -> None:
    await cache.close()
    await cache.close()


# --- MemoryCache specifics ------------------------------------------------------------


@pytest.fixture
def memory(clock: FakeClock) -> Callable[[int], MemoryCache]:
    return lambda max_entries: MemoryCache(max_entries=max_entries, clock=clock)


async def test_memory_entry_alive_before_ttl(clock: FakeClock) -> None:
    mem = MemoryCache(clock=clock)
    await mem.set("k", b"v", ttl=10)
    clock.advance(9.99)
    assert await mem.get("k") == b"v"


async def test_memory_entry_gone_exactly_at_ttl(clock: FakeClock) -> None:
    mem = MemoryCache(clock=clock)
    await mem.set("k", b"v", ttl=10)
    clock.advance(10)
    assert await mem.get("k") is None


async def test_memory_entry_gone_after_ttl(clock: FakeClock) -> None:
    mem = MemoryCache(clock=clock)
    await mem.set("k", b"v", ttl=10)
    clock.advance(60)
    assert await mem.get("k") is None


async def test_memory_overwrite_replaces_ttl(clock: FakeClock) -> None:
    mem = MemoryCache(clock=clock)
    await mem.set("k", b"v1", ttl=10)
    clock.advance(5)
    await mem.set("k", b"v2", ttl=10)
    clock.advance(7)  # 12s after first set, 7s after second
    assert await mem.get("k") == b"v2"


async def test_memory_get_does_not_extend_ttl(clock: FakeClock) -> None:
    mem = MemoryCache(clock=clock)
    await mem.set("k", b"v", ttl=10)
    clock.advance(6)
    assert await mem.get("k") == b"v"
    clock.advance(4)
    assert await mem.get("k") is None


async def test_memory_evicts_least_recently_set(memory: Callable[[int], MemoryCache]) -> None:
    mem = memory(2)
    await mem.set("a", b"1")
    await mem.set("b", b"2")
    await mem.set("c", b"3")
    assert await mem.get("a") is None
    assert await mem.get("b") == b"2"
    assert await mem.get("c") == b"3"


async def test_memory_get_hit_counts_as_use(memory: Callable[[int], MemoryCache]) -> None:
    mem = memory(2)
    await mem.set("a", b"1")
    await mem.set("b", b"2")
    assert await mem.get("a") == b"1"
    await mem.set("c", b"3")
    assert await mem.get("a") == b"1"
    assert await mem.get("b") is None


async def test_memory_overwrite_counts_as_use(memory: Callable[[int], MemoryCache]) -> None:
    mem = memory(2)
    await mem.set("a", b"1")
    await mem.set("b", b"2")
    await mem.set("a", b"1b")
    await mem.set("c", b"3")
    assert await mem.get("a") == b"1b"
    assert await mem.get("b") is None


async def test_memory_overwrite_does_not_evict(memory: Callable[[int], MemoryCache]) -> None:
    mem = memory(2)
    await mem.set("a", b"1")
    await mem.set("b", b"2")
    await mem.set("a", b"1b")
    assert await mem.get("b") == b"2"


async def test_memory_miss_does_not_affect_recency(
    memory: Callable[[int], MemoryCache],
) -> None:
    mem = memory(2)
    await mem.set("a", b"1")
    await mem.set("b", b"2")
    assert await mem.get("zzz") is None
    await mem.set("c", b"3")
    assert await mem.get("a") is None


async def test_memory_expired_entry_is_not_a_hit_for_recency(
    clock: FakeClock,
) -> None:
    mem = MemoryCache(max_entries=2, clock=clock)
    await mem.set("a", b"1", ttl=5)
    await mem.set("b", b"2")
    clock.advance(5)
    assert await mem.get("a") is None  # expired: a miss, not a use
    await mem.set("c", b"3")
    assert await mem.get("b") == b"2"
    assert await mem.get("c") == b"3"


async def test_memory_max_entries_one(memory: Callable[[int], MemoryCache]) -> None:
    mem = memory(1)
    await mem.set("a", b"1")
    await mem.set("b", b"2")
    assert await mem.get("a") is None
    assert await mem.get("b") == b"2"


@pytest.mark.parametrize("bad", [0, -1])
def test_memory_rejects_max_entries_below_one(bad: int) -> None:
    with pytest.raises(ValueError):
        MemoryCache(max_entries=bad)


async def test_memory_is_empty_after_close(clock: FakeClock) -> None:
    mem = MemoryCache(clock=clock)
    await mem.set("k", b"v")
    await mem.close()
    assert await mem.get("k") is None


# --- DiskCache specifics --------------------------------------------------------------


async def test_disk_creates_missing_directory(tmp_path: Path) -> None:
    directory = tmp_path / "a" / "b" / "cache"
    disk = DiskCache(directory)
    try:
        assert directory.is_dir()
    finally:
        await disk.close()


async def test_disk_data_survives_reopen(tmp_path: Path) -> None:
    first = DiskCache(tmp_path)
    await first.set("k", b"persisted")
    await first.close()
    second = DiskCache(tmp_path)
    try:
        assert await second.get("k") == b"persisted"
    finally:
        await second.close()


async def test_disk_entry_expires_after_short_ttl(tmp_path: Path) -> None:
    disk = DiskCache(tmp_path)
    try:
        await disk.set("k", b"v", ttl=0.03)
        assert await disk.get("k") == b"v"
        await asyncio.sleep(0.06)
        assert await disk.get("k") is None
    finally:
        await disk.close()


async def test_disk_accepts_custom_size_limit(tmp_path: Path) -> None:
    disk = DiskCache(tmp_path, size_limit_bytes=10 * 1024 * 1024)
    try:
        await disk.set("k", b"v")
        assert await disk.get("k") == b"v"
    finally:
        await disk.close()


# --- make_key -------------------------------------------------------------------------

URL = "https://www.boe.es/datosabiertos/api/legislacion-consolidada"


def test_make_key_is_sha256_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", make_key(URL))


def test_make_key_is_deterministic() -> None:
    assert make_key(URL, {"a": "1"}) == make_key(URL, {"a": "1"})


def test_make_key_ignores_param_order() -> None:
    assert make_key(URL, {"a": "1", "b": "2"}) == make_key(URL, {"b": "2", "a": "1"})


def test_make_key_ignores_none_params() -> None:
    assert make_key(URL, {"a": "1", "b": None}) == make_key(URL, {"a": "1"})


def test_make_key_none_and_empty_params_are_equal() -> None:
    assert make_key(URL, None) == make_key(URL, {}) == make_key(URL)


def test_make_key_all_none_params_equals_no_params() -> None:
    assert make_key(URL, {"a": None}) == make_key(URL)


def test_make_key_int_and_str_values_are_equal() -> None:
    assert make_key(URL, {"limit": 50}) == make_key(URL, {"limit": "50"})


def test_make_key_differs_by_url() -> None:
    assert make_key(URL) != make_key(URL + "/other")


def test_make_key_differs_by_param_value() -> None:
    assert make_key(URL, {"a": "1"}) != make_key(URL, {"a": "2"})


def test_make_key_differs_by_param_name() -> None:
    assert make_key(URL, {"a": "1"}) != make_key(URL, {"b": "1"})


def test_make_key_differs_by_added_param() -> None:
    assert make_key(URL, {"a": "1"}) != make_key(URL, {"a": "1", "b": "2"})


def test_make_key_differs_by_accept() -> None:
    assert make_key(URL, accept="application/json") != make_key(URL, accept="application/xml")


def test_make_key_default_accept_is_json() -> None:
    assert make_key(URL) == make_key(URL, accept="application/json")


def test_make_key_does_not_confuse_name_and_value_boundaries() -> None:
    assert make_key(URL, {"ab": "c"}) != make_key(URL, {"a": "bc"})


# --- default_cache_dir ----------------------------------------------------------------


def test_default_cache_dir_uses_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom"
    monkeypatch.setenv("BOE_INSIGHTS_CACHE_DIR", str(target))
    assert default_cache_dir() == target
    assert not target.exists()


def test_default_cache_dir_falls_back_to_platformdirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOE_INSIGHTS_CACHE_DIR", raising=False)
    result = default_cache_dir()
    assert isinstance(result, Path)
    assert result == Path(user_cache_dir("boe-insights-mcp"))


def test_default_cache_dir_ignores_empty_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOE_INSIGHTS_CACHE_DIR", "")
    assert default_cache_dir() == Path(user_cache_dir("boe-insights-mcp"))
