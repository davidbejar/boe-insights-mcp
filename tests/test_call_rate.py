"""Tests of the per-caller call limit over HTTP (``callrate``, ``Settings`` and ``create_server``).

The clock the limit reads is replaced by a fake one (``rate_limiting.time``), so refill is tested
without sleeping. Nothing here reaches boe.es or github.com: a call to ``get_law_overview`` with a
malformed identifier fails in the server with ``invalid_argument`` and no request, which is how
a test tells an admitted call (that error) from a refused one (``rate_limited``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from fastmcp.server.middleware import rate_limiting
from fastmcp.server.middleware.authorization import AuthMiddleware
from mcp import MCPError

from boe_insights_mcp import callrate
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

from . import auth_support as auth

PER_MINUTE = "BOE_INSIGHTS_CALLS_PER_MINUTE"
BURST = "BOE_INSIGHTS_CALLS_BURST"

ALICE = ("1001", "alice")
BOB = ("2002", "bob")
MALLORY = ("9999", "mallory")


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        """The current fake time in seconds."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Give FastMCP's limiter a fake clock (only its module's ``time``; the real one stays)."""
    fake = FakeClock()
    monkeypatch.setattr(rate_limiting, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


@pytest.fixture(scope="module")
def keys() -> RSAKeyPair:
    """One key pair for the module: generating RSA keys is the slow part."""
    return auth.new_keys()


@pytest.fixture
def boe_client() -> BoeClient:
    """A client that is never used: every call below fails before reaching it."""
    return BoeClient(
        cache=MemoryCache(),
        limiter=RateLimiter(min_interval=0),
        http=httpx.AsyncClient(),
        max_retries=0,
    )


def limited_settings(transport: str = "http", **changes: Any) -> Settings:
    """Settings with a small burst and one call per second of refill."""
    base: dict[str, Any] = {
        "transport": transport,
        "calls_per_minute": 60,
        "calls_burst": 3,
        "allowed_users": ("alice", "bob"),
    }
    return Settings(**{**base, **changes})


def signed_in(boe_client: BoeClient, keys: RSAKeyPair, settings: Settings) -> FastMCP:
    """A signed-in HTTP server whose users are checked against the JWT stand-in."""
    return create_server(client=boe_client, settings=settings, auth=auth.verifier_for(keys))


def bearer(keys: RSAKeyPair, who: tuple[str, str] | None, **kwargs: Any) -> str:
    """A token for ``(id, login)``, or a forged one when ``kwargs`` has ``sub``."""
    if who is None:
        return auth.token(keys, **kwargs)
    return auth.token(keys, sub=who[0], login=who[1])


async def call(client: Client[Any]) -> str:
    """One tool call; ``admitted`` or ``limited`` by the error text, anything else fails."""
    try:
        await client.call_tool("get_law_overview", {"law_id": "Ley 40/2015"})
    except ToolError as error:
        text = str(error)
        if "rate_limited" in text:
            return "limited"
        assert "invalid_argument" in text, text
        return "admitted"
    raise AssertionError("the malformed identifier should have been refused")


async def burst_of(client: Client[Any], count: int) -> list[str]:
    """``count`` calls in a row."""
    return [await call(client) for _ in range(count)]


def has_limit(server: FastMCP) -> bool:
    """Whether the server carries the call limit."""
    return any(isinstance(m, callrate.CallRateLimit) for m in server.middleware)


# --- Settings -----------------------------------------------------------------------------------


def test_defaults_are_sixty_a_minute_with_a_burst_of_twenty() -> None:
    for settings in (Settings(), Settings.from_env({})):
        assert settings.calls_per_minute == 60
        assert settings.calls_burst == 20


def test_the_variables_are_read() -> None:
    settings = Settings.from_env({PER_MINUTE: "120", BURST: "5"})

    assert (settings.calls_per_minute, settings.calls_burst) == (120, 5)


def test_empty_variables_keep_the_defaults() -> None:
    settings = Settings.from_env({PER_MINUTE: "", BURST: ""})

    assert (settings.calls_per_minute, settings.calls_burst) == (60, 20)


def test_zero_calls_per_minute_is_allowed_and_switches_the_limit_off() -> None:
    assert Settings.from_env({PER_MINUTE: "0"}).calls_per_minute == 0


@pytest.mark.parametrize("value", ["-1", "1.5", "abc", "nan", "1e3"])
def test_bad_calls_per_minute_names_the_variable(value: str) -> None:
    with pytest.raises(ValueError, match=PER_MINUTE):
        Settings.from_env({PER_MINUTE: value})


@pytest.mark.parametrize("value", ["0", "-3", "2.5", "abc"])
def test_bad_burst_names_the_variable(value: str) -> None:
    with pytest.raises(ValueError, match=BURST):
        Settings.from_env({BURST: value})


def test_burst_is_checked_even_when_the_limit_is_off() -> None:
    with pytest.raises(ValueError, match=BURST):
        Settings.from_env({PER_MINUTE: "0", BURST: "0"})


# --- Where the limit exists ---------------------------------------------------------------------


@pytest.mark.parametrize("settings", [None, Settings(transport="stdio", calls_burst=1)])
async def test_local_mode_has_no_limit_middleware_and_never_refuses(
    boe_client: BoeClient, settings: Settings | None
) -> None:
    server = create_server(client=boe_client, settings=settings)

    assert not any("RateLimit" in type(m).__name__ for m in server.middleware)
    # FastMCP's own default is the only middleware a local server has.
    assert [type(m).__name__ for m in server.middleware] == ["DereferenceRefsMiddleware"]
    async with Client(server) as client:
        assert set(await burst_of(client, 30)) == {"admitted"}


async def test_a_limit_of_zero_adds_no_middleware_over_http(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    open_server = create_server(
        client=boe_client, settings=limited_settings(calls_per_minute=0, calls_burst=1)
    )
    signed = signed_in(boe_client, keys, limited_settings(calls_per_minute=0, calls_burst=1))

    assert not has_limit(open_server)
    assert not has_limit(signed)
    async with auth.session(open_server, limited_settings()) as client:
        assert set(await burst_of(client, 10)) == {"admitted"}


# --- Open HTTP: one bucket for everyone ---------------------------------------------------------


async def test_open_http_shares_one_bucket_between_all_callers(boe_client: BoeClient) -> None:
    settings = limited_settings()
    server = create_server(client=boe_client, settings=settings)

    async with auth.serve(server, settings) as gateway:
        async with gateway.session() as first:
            assert await burst_of(first, 2) == ["admitted", "admitted"]
        async with gateway.session() as second:
            assert await burst_of(second, 2) == ["admitted", "limited"]


async def test_open_http_ignores_headers_a_client_could_forge(boe_client: BoeClient) -> None:
    settings = limited_settings()
    server = create_server(client=boe_client, settings=settings)

    async with auth.session(server, settings) as client:
        assert await burst_of(client, 3) == ["admitted"] * 3
        # A forwarded address is client-supplied: it must not give a fresh bucket.
        assert await call(client) == "limited"


# --- Signed-in HTTP: one bucket per GitHub id -----------------------------------------------------


async def test_one_user_exhausting_their_bucket_does_not_affect_another(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    settings = limited_settings()
    server = signed_in(boe_client, keys, settings)

    async with auth.serve(server, settings) as gateway:
        async with gateway.session(bearer(keys, ALICE)) as alice:
            assert await burst_of(alice, 4) == ["admitted"] * 3 + ["limited"]
        async with gateway.session(bearer(keys, BOB)) as bob:
            assert await burst_of(bob, 4) == ["admitted"] * 3 + ["limited"]
        async with gateway.session(bearer(keys, ALICE)) as alice_again:
            # A new session of the same user is the same bucket.
            assert await call(alice_again) == "limited"


async def test_the_bucket_follows_the_id_not_the_login(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    settings = limited_settings(allowed_users=("id:1001",))
    server = signed_in(boe_client, keys, settings)

    async with auth.serve(server, settings) as gateway:
        async with gateway.session(bearer(keys, ("1001", "alice"))) as before:
            await burst_of(before, 3)
        async with gateway.session(bearer(keys, ("1001", "alice-renamed"))) as after:
            assert await call(after) == "limited"


async def test_an_unlisted_but_signed_in_user_is_limited_too(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    settings = limited_settings()
    server = signed_in(boe_client, keys, settings)

    async with auth.session(server, settings, bearer(keys, MALLORY)) as client:
        # The allow-list refuses these (a ToolError without ``rate_limited``), but they count.
        for _ in range(3):
            with pytest.raises(ToolError) as refused:
                await client.call_tool("get_law_overview", {"law_id": "Ley 40/2015"})
            assert "rate_limited" not in str(refused.value)
        assert await call(client) == "limited"


@pytest.mark.parametrize("sub", ["abc", "", "١٢", None, "1001 "], ids=repr)
async def test_callers_without_a_usable_subject_share_one_unidentified_bucket(
    boe_client: BoeClient, keys: RSAKeyPair, sub: Any
) -> None:
    settings = limited_settings()
    server = signed_in(boe_client, keys, settings)

    async with auth.serve(server, settings) as gateway:
        async with gateway.session(bearer(keys, None, sub=sub)) as first:
            for _ in range(2):
                with pytest.raises(ToolError):
                    await first.call_tool("get_law_overview", {"law_id": "x"})
        async with gateway.session(bearer(keys, None, sub="not-a-number")) as second:
            with pytest.raises(ToolError):
                await second.call_tool("get_law_overview", {"law_id": "x"})
            assert await call(second) == "limited"
        async with gateway.session(bearer(keys, ALICE)) as alice:
            assert await call(alice) == "admitted"


def test_the_caller_key_without_a_token_is_the_unidentified_bucket() -> None:
    assert callrate.caller_key() == callrate.UNIDENTIFIED


# --- What is counted ----------------------------------------------------------------------------


async def test_discovery_requests_do_not_use_up_the_budget(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    settings = limited_settings(calls_burst=2)
    server = signed_in(boe_client, keys, settings)

    async with auth.session(server, settings, bearer(keys, ALICE)) as client:
        for _ in range(5):
            await client.list_tools()
            await client.list_resources()
            await client.list_resource_templates()
            await client.list_prompts()
        assert await burst_of(client, 3) == ["admitted", "admitted", "limited"]


async def test_resources_and_prompts_are_counted_and_share_the_bucket_with_tools(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    settings = limited_settings(calls_burst=2)
    server = signed_in(boe_client, keys, settings)

    async with auth.session(server, settings, bearer(keys, ALICE)) as client:
        await client.get_prompt("explain_law", {"law": "Ley 40/2015"})
        await client.get_prompt("explain_law", {"law": "Ley 40/2015"})
        assert await call(client) == "limited"
        with pytest.raises(MCPError, match="rate_limited"):
            await client.read_resource("boe://saved-searches")


# --- What the caller receives -------------------------------------------------------------------


async def test_the_refusal_is_a_curated_message_that_survives_masking(
    boe_client: BoeClient, keys: RSAKeyPair
) -> None:
    settings = limited_settings(calls_burst=1)
    server = signed_in(boe_client, keys, settings)

    async with auth.session(server, settings, bearer(keys, ALICE)) as client:
        await call(client)
        with pytest.raises(ToolError) as caught:
            await client.call_tool("get_law_overview", {"law_id": "Ley 40/2015"})

    text = str(caught.value)
    assert text.startswith("Rate limit reached (rate_limited, retryable): ")
    assert "60 seconds" not in text
    assert "1 second" in text
    for secret in ("1001", "alice", "github", "unidentified"):
        assert secret not in text


async def test_the_advice_is_how_long_one_call_takes_to_come_back(
    boe_client: BoeClient,
) -> None:
    settings = limited_settings(calls_per_minute=2, calls_burst=1)
    server = create_server(client=boe_client, settings=settings)

    async with auth.session(server, settings) as client:
        await call(client)
        with pytest.raises(ToolError, match="30 seconds"):
            await client.call_tool("get_law_overview", {"law_id": "Ley 40/2015"})


async def test_a_refused_resource_or_prompt_carries_the_same_text(
    boe_client: BoeClient,
) -> None:
    settings = limited_settings(calls_burst=1)
    server = create_server(client=boe_client, settings=settings)

    async with auth.session(server, settings) as client:
        await client.get_prompt("explain_law", {"law": "Ley 40/2015"})
        with pytest.raises(Exception, match=r"Rate limit reached \(rate_limited, retryable\)"):
            await client.get_prompt("explain_law", {"law": "Ley 40/2015"})
        with pytest.raises(Exception, match=r"Rate limit reached \(rate_limited, retryable\)"):
            await client.read_resource("boe://law/BOE-A-2015-10566")


# --- Refill -------------------------------------------------------------------------------------


async def test_the_bucket_refills_with_time(
    boe_client: BoeClient, keys: RSAKeyPair, clock: FakeClock
) -> None:
    settings = limited_settings()
    server = signed_in(boe_client, keys, settings)

    async with auth.session(server, settings, bearer(keys, ALICE)) as client:
        assert await burst_of(client, 4) == ["admitted"] * 3 + ["limited"]
        clock.advance(0.5)
        assert await call(client) == "limited"
        clock.advance(0.5)  # one call per second: one token is back
        assert await burst_of(client, 2) == ["admitted", "limited"]
        clock.advance(600)  # never more than the burst
        assert await burst_of(client, 4) == ["admitted"] * 3 + ["limited"]


async def test_an_idle_users_bucket_is_dropped_and_comes_back_full(
    boe_client: BoeClient, keys: RSAKeyPair, clock: FakeClock
) -> None:
    settings = limited_settings()
    server = signed_in(boe_client, keys, settings)
    (middleware,) = (m for m in server.middleware if isinstance(m, callrate.CallRateLimit))

    async with auth.serve(server, settings) as gateway:
        async with gateway.session(bearer(keys, ALICE)) as alice:
            await burst_of(alice, 3)
        assert "github:1001" in middleware.limiters
        clock.advance(10)
        async with gateway.session(bearer(keys, BOB)) as bob:
            await call(bob)
        assert "github:1001" not in middleware.limiters


def test_two_servers_do_not_share_buckets(boe_client: BoeClient, keys: RSAKeyPair) -> None:
    first = signed_in(boe_client, keys, limited_settings())
    second = signed_in(boe_client, keys, limited_settings())
    one = next(m for m in first.middleware if isinstance(m, callrate.CallRateLimit))
    two = next(m for m in second.middleware if isinstance(m, callrate.CallRateLimit))

    assert one is not two
    assert one.limiters is not two.limiters


# --- Order --------------------------------------------------------------------------------------


def test_the_limit_sits_outside_the_allow_list(boe_client: BoeClient, keys: RSAKeyPair) -> None:
    server = signed_in(boe_client, keys, limited_settings())
    kinds = [type(m) for m in server.middleware]

    # The first in the list is the outermost: it runs before the ones after it.
    assert kinds.index(callrate.CallRateLimit) < kinds.index(AuthMiddleware)
