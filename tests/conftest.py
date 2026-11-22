"""Shared fixtures and helpers for the BOE client layer tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.ratelimit import RateLimiter

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "boe"

JSON_HEADERS = {"Content-Type": "application/json;charset=UTF-8"}
XML_HEADERS = {"Content-Type": "application/xml;charset=UTF-8"}


@pytest.fixture(autouse=True)
def private_user_directories(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the saved-search data directory at a temporary one for every test.

    A test that starts a server without injecting a client would otherwise create the saved-search
    database in the real user data directory. Tests that care about these
    variables set them again themselves.
    """
    root = tmp_path_factory.mktemp("user-dirs")
    monkeypatch.setenv("BOE_INSIGHTS_DATA_DIR", str(root / "data"))


@pytest.fixture
def load_fixture() -> Callable[[str], bytes]:
    """Return a function reading the raw bytes of a recorded BOE API response."""

    def _load(name: str) -> bytes:
        return (FIXTURE_DIR / name).read_bytes()

    return _load


@pytest.fixture
def load_json(load_fixture: Callable[[str], bytes]) -> Callable[[str], Any]:
    """Return a function parsing a recorded JSON fixture."""

    def _load(name: str) -> Any:
        return json.loads(load_fixture(name))

    return _load


@pytest.fixture
def json_response() -> Callable[..., httpx.Response]:
    """Build a JSON ``httpx.Response`` from raw bytes (e.g. a fixture body)."""

    def _build(
        body: bytes, status: int = 200, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return httpx.Response(status, content=body, headers={**JSON_HEADERS, **(headers or {})})

    return _build


@pytest.fixture
def xml_response() -> Callable[..., httpx.Response]:
    """Build an XML ``httpx.Response``; the BOE API sends errors as XML even for JSON requests."""

    def _build(body: bytes, status: int, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, content=body, headers={**XML_HEADERS, **(headers or {})})

    return _build


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    """Mock every httpx request; anything not explicitly routed fails the test."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


class RecordingCache(MemoryCache):
    """``MemoryCache`` that remembers every ``set`` call as ``(key, ttl)``."""

    def __init__(self) -> None:
        super().__init__()
        self.sets: list[tuple[str, float | None]] = []

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        self.sets.append((key, ttl))
        await super().set(key, value, ttl)


@pytest.fixture
def cache() -> RecordingCache:
    return RecordingCache()


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(min_interval=0)


class SleepRecorder:
    """Injectable ``sleep`` that records the requested delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def http(router: respx.MockRouter) -> AsyncIterator[httpx.AsyncClient]:
    """An injectable ``httpx.AsyncClient``, closed after the test."""
    async with httpx.AsyncClient() as client:
        yield client
