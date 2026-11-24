"""Tests of the three modes (local, open HTTP, signed-in HTTP), end to end via ``create_server``.

Signed-in tests use a ``JWTVerifier`` with a generated RSA key pair as the stand-in for GitHub
(``tests/auth_support.py``): tokens carry chosen ``sub`` and ``login`` claims, and requests go
straight into the ASGI app, so nothing here reaches github.com or boe.es.

Saved-search rows live in a ``RecordingStore`` that remembers every owner it was asked about, so a
test can show that no row was ever read or written for ``local`` or for somebody else.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from mcp import MCPError

from boe_insights_mcp import saved_searches, server
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.searchstore import SavedSearch, SavedSearchStore
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

from . import auth_support as auth
from .test_server import SAVED_SEARCH_TOOL_NAMES, TOOL_NAMES

SUMMARY_URL = f"{BASE_URL}boe/sumario/"
PROMPT_NAMES = {
    "explain_law",
    "explicar_ley",
    "article_changes",
    "cambios_articulo",
    "check_citations",
    "comprobar_citas",
    "gazette_briefing",
    "resumen_boe",
}
TEMPLATE_URIS = {
    "boe://law/{law_id}",
    "boe://law/{law_id}/article/{block_id}",
    "boe://gazette/{day}",
    "boe://codes/{kind}",
}
RESOURCE_URI = "boe://saved-searches"
OTHER_TOOLS = TOOL_NAMES - SAVED_SEARCH_TOOL_NAMES

ALICE = ("1001", "alice")
BOB = ("2002", "bob")
MALLORY = ("9999", "mallory")


class RecordingStore(SavedSearchStore):
    """An in-memory store that remembers every owner any operation was asked about."""

    def __init__(self) -> None:
        super().__init__(None)
        self.owners_seen: set[str] = set()

    async def add(self, search: SavedSearch) -> None:
        self.owners_seen.add(search.owner)
        await super().add(search)

    async def list(self, owner: str) -> list[SavedSearch]:
        self.owners_seen.add(owner)
        return await super().list(owner)

    async def get(self, owner: str, name: str) -> SavedSearch:
        self.owners_seen.add(owner)
        return await super().get(owner, name)

    async def names(self, owner: str) -> set[str]:
        self.owners_seen.add(owner)
        return await super().names(owner)

    async def replace(self, owner: str, name: str, search: SavedSearch) -> None:
        self.owners_seen.update({owner, search.owner})
        await super().replace(owner, name, search)

    async def update_progress(self, owner: str, name: str, **kwargs: Any) -> bool:
        self.owners_seen.add(owner)
        return await super().update_progress(owner, name, **kwargs)

    async def delete(self, owner: str, name: str) -> None:
        self.owners_seen.add(owner)
        await super().delete(owner, name)


@pytest.fixture(scope="module")
def keys() -> RSAKeyPair:
    """One key pair for the module: generating RSA keys is the slow part."""
    return auth.new_keys()


@pytest.fixture
def store() -> RecordingStore:
    return RecordingStore()


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    """A client over the respx router: a request to boe.es not routed here fails the test."""
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


@pytest.fixture(autouse=True)
def thursday_without_an_issue(
    monkeypatch: pytest.MonkeyPatch,
    router: respx.MockRouter,
    xml_response: Any,
    load_fixture: Any,
) -> respx.Route:
    """Friday-before: it is Thursday 2026-11-19 and today's gazette issue is not out yet.

    Saving or checking a gazette search reads today's issue; this answers it with a 404 and
    fixes the clock the use cases see, exactly as ``tests/test_server_saved_searches.py`` does.
    """
    moment = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(
        saved_searches, "utc_now", lambda now=None: now if now is not None else moment
    )
    return router.get(f"{SUMMARY_URL}20261119").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )


def local_server(boe_client: BoeClient, store: SavedSearchStore) -> FastMCP:
    """Mode local: stdio settings, no authentication."""
    return create_server(client=boe_client, settings=Settings(transport="stdio"), store=store)


def open_http_server(boe_client: BoeClient, store: SavedSearchStore) -> FastMCP:
    """Mode open HTTP: http transport, no authentication."""
    return create_server(client=boe_client, settings=Settings(transport="http"), store=store)


def signed_in_server(
    boe_client: BoeClient,
    store: SavedSearchStore,
    keys: RSAKeyPair,
    allowed: Iterable[str] = ("alice", "bob"),
) -> tuple[FastMCP, Settings]:
    """Mode signed-in HTTP: http transport, the JWT stand-in as the auth provider."""
    settings = Settings(transport="http", allowed_users=tuple(allowed))
    built = create_server(
        client=boe_client, settings=settings, store=store, auth=auth.verifier_for(keys)
    )
    return built, settings


def bearer(keys: RSAKeyPair, who: tuple[str, str], **claims: Any) -> str:
    """A token for the user ``(id, login)``."""
    return auth.token(keys, sub=who[0], login=who[1], **claims)


async def structured(client: Client[Any], tool: str, **arguments: Any) -> dict[str, Any]:
    """Call ``tool`` and return its structured result."""
    result = await client.call_tool(tool, arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def names_of_saved_searches(client: Client[Any]) -> list[str]:
    """The names ``list_saved_searches`` returns to this client."""
    return [row["name"] for row in (await structured(client, "list_saved_searches"))["searches"]]


async def names_in_resource(client: Client[Any]) -> list[str]:
    """The names the ``boe://saved-searches`` resource returns to this client."""
    [content] = await client.read_resource(RESOURCE_URI)
    return [row["name"] for row in json.loads(content.text)["searches"]]  # type: ignore[union-attr]


async def visible(client: Client[Any]) -> dict[str, set[str]]:
    """Everything this client can list, by kind."""
    return {
        "tools": {tool.name for tool in await client.list_tools()},
        "resources": {str(resource.uri) for resource in await client.list_resources()},
        "templates": {t.uri_template for t in await client.list_resource_templates()},
        "prompts": {prompt.name for prompt in await client.list_prompts()},
    }


EVERYTHING = {
    "tools": TOOL_NAMES,
    "resources": {RESOURCE_URI},
    "templates": TEMPLATE_URIS,
    "prompts": PROMPT_NAMES,
}
NOTHING: dict[str, set[str]] = {
    "tools": set(),
    "resources": set(),
    "templates": set(),
    "prompts": set(),
}


# --- Mode local ----------------------------------------------------------------------------------


@pytest.mark.parametrize("settings", [None, Settings(transport="stdio")], ids=["none", "stdio"])
async def test_local_mode_lists_every_component(
    boe_client: BoeClient, store: RecordingStore, settings: Settings | None
) -> None:
    built = create_server(client=boe_client, settings=settings, store=store)

    async with Client(built) as client:
        assert await visible(client) == EVERYTHING


async def test_local_mode_keeps_the_saved_search_sentence_in_the_instructions(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(local_server(boe_client, store)) as client:
        instructions = client.instructions or ""

    assert "save_search" in instructions
    assert "check_saved_searches" in instructions
    assert "Resources and prompts only mirror these tools." in instructions


async def test_local_mode_owns_every_row_as_local(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(local_server(boe_client, store)) as client:
        await structured(client, "save_search", name="mine", words="Real Decreto")

        assert await names_of_saved_searches(client) == ["mine"]
        assert await names_in_resource(client) == ["mine"]

    assert [row.name for row in await store.list("local")] == ["mine"]
    assert store.owners_seen == {"local"}


# --- Mode open HTTP ------------------------------------------------------------------------------


async def test_open_http_lists_everything_except_the_saved_search_components(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(open_http_server(boe_client, store)) as client:
        listed = await visible(client)

    assert listed["tools"] == OTHER_TOOLS
    assert len(OTHER_TOOLS) == len(TOOL_NAMES) - 5
    assert listed["resources"] == set()
    assert listed["templates"] == TEMPLATE_URIS
    assert listed["prompts"] == PROMPT_NAMES


VALID_ARGUMENTS: dict[str, dict[str, Any]] = {
    "save_search": {"words": "Real Decreto"},
    "list_saved_searches": {},
    "check_saved_searches": {},
    "edit_saved_search": {"name": "mine"},
    "delete_saved_search": {"name": "mine"},
}


@pytest.mark.parametrize("tool", sorted(SAVED_SEARCH_TOOL_NAMES))
async def test_open_http_does_not_know_the_saved_search_tools(
    boe_client: BoeClient, store: RecordingStore, tool: str
) -> None:
    async with Client(open_http_server(boe_client, store)) as client:
        # Valid arguments, so the only reason left to refuse is that the tool is not there.
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool(tool, VALID_ARGUMENTS[tool])

    assert store.owners_seen == set()


async def test_open_http_does_not_know_the_saved_searches_resource(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(open_http_server(boe_client, store)) as client:
        with pytest.raises(MCPError, match="Resource not found"):
            await client.read_resource(RESOURCE_URI)

    assert store.owners_seen == set()


async def test_open_http_instructions_lose_only_the_saved_search_sentence(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(local_server(boe_client, store)) as client:
        full = client.instructions or ""
    async with Client(open_http_server(boe_client, store)) as client:
        open_instructions = client.instructions or ""

    start = full.index("Saved searches (")
    marker = "check_saved_searches and repeat it while complete is false. "
    end = full.index(marker) + len(marker)
    assert full[start:end].startswith("Saved searches (save_search")
    assert open_instructions == full[:start] + full[end:]
    assert "saved search" not in open_instructions.lower()
    assert "check_saved_searches" not in open_instructions
    assert "Resources and prompts only mirror these tools." in open_instructions
    assert "resolve_reference turns a name" in open_instructions


async def test_open_http_keeps_the_other_tools_callable(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(open_http_server(boe_client, store)) as client:
        # The tool runs and rejects the malformed identifier itself: not hidden, not refused.
        with pytest.raises(ToolError, match=r"invalid_argument.*invalid law identifier"):
            await client.call_tool("get_amendment_history", {"law_id": "Ley 40/2015"})


async def test_open_http_serves_everything_else_without_a_token(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    built = open_http_server(boe_client, store)

    async with auth.session(built, Settings(transport="http")) as client:
        listed = await visible(client)

    assert listed["tools"] == OTHER_TOOLS
    assert listed["prompts"] == PROMPT_NAMES


# --- Mode signed-in HTTP: who sees what -----------------------------------------------------------


async def test_an_allowed_user_sees_every_component(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.session(built, settings, bearer(keys, ALICE)) as client:
        assert await visible(client) == EVERYTHING
        assert "save_search" in (client.instructions or "")


async def test_a_user_allowed_by_a_case_different_username_sees_every_component(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=["ALICE"])

    async with auth.session(built, settings, bearer(keys, ALICE)) as client:
        assert await visible(client) == EVERYTHING


async def test_a_user_allowed_by_id_sees_every_component_whatever_the_login(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=["id:1001"])

    async with auth.session(built, settings, bearer(keys, ("1001", "a-new-name"))) as client:
        assert await visible(client) == EVERYTHING


async def test_a_star_allows_every_signed_in_user(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=["*"])

    async with auth.session(built, settings, bearer(keys, MALLORY)) as client:
        assert await visible(client) == EVERYTHING


async def test_a_user_who_is_not_on_the_list_sees_nothing(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=["alice", "id:2002"])

    async with auth.session(built, settings, bearer(keys, MALLORY)) as client:
        assert await visible(client) == NOTHING


@pytest.mark.parametrize(
    "who",
    [("1001", "alic"), ("1001", "alice2"), ("1001", "xalice"), ("100", "x"), ("10011", "x")],
)
async def test_near_misses_of_a_listed_name_or_id_see_nothing(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair, who: tuple[str, str]
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=["alice", "id:1001"])
    # ("1001", "alic") still matches by id; the other rows do not match either entry.
    expected = EVERYTHING if who[0] == "1001" else NOTHING

    async with auth.session(built, settings, bearer(keys, who)) as client:
        assert await visible(client) == expected


async def test_an_empty_list_lets_nobody_in(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=[])

    async with auth.session(built, settings, bearer(keys, ALICE)) as client:
        assert await visible(client) == NOTHING


# --- Mode signed-in HTTP: refusals ----------------------------------------------------------------


async def test_no_token_is_a_401_at_the_door(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway, gateway.raw() as http:
        response = await http.post("/mcp", headers=auth.MCP_HEADERS, json=auth.INITIALIZE)

    assert response.status_code == 401
    assert store.owners_seen == set()


async def test_a_token_that_does_not_verify_is_a_401(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)
    forged = auth.token(auth.new_keys(), sub="1001", login="alice")
    expired = auth.token(keys, sub="1001", login="alice", exp=1)

    async with auth.serve(built, settings) as gateway:
        for bad in ("not-a-token", forged, expired):
            headers = {**auth.MCP_HEADERS, "Authorization": f"Bearer {bad}"}
            async with gateway.raw() as http:
                response = await http.post("/mcp", headers=headers, json=auth.INITIALIZE)
            assert response.status_code == 401


async def test_a_valid_token_is_not_a_401(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)
    headers = {**auth.MCP_HEADERS, "Authorization": f"Bearer {bearer(keys, ALICE)}"}

    async with auth.serve(built, settings) as gateway, gateway.raw() as http:
        response = await http.post("/mcp", headers=headers, json=auth.INITIALIZE)

    assert response.status_code == 200


async def test_health_needs_no_token_in_signed_in_mode(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair, router: respx.MockRouter
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway, gateway.raw() as http:
        response = await http.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_an_unlisted_user_is_refused_on_every_kind_of_call(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair, router: respx.MockRouter
) -> None:
    built, settings = signed_in_server(boe_client, store, keys, allowed=["alice", "id:2002"])

    async with auth.session(built, settings, bearer(keys, MALLORY)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("list_saved_searches", {})
        with pytest.raises(ToolError):
            await client.call_tool("search_legislation", {})
        with pytest.raises(MCPError):
            await client.read_resource(RESOURCE_URI)
        with pytest.raises(MCPError):
            await client.read_resource("boe://law/BOE-A-2015-10566")
        with pytest.raises(MCPError):
            await client.get_prompt("explain_law", {"law": "Ley 40/2015"})

    assert store.owners_seen == set()
    assert router.calls.call_count == 0


async def test_a_refusal_does_not_reveal_who_is_on_the_list(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(
        boe_client, store, keys, allowed=["alice", "id:4242", "zed-the-admin"]
    )
    texts: list[str] = []

    async with auth.session(built, settings, bearer(keys, MALLORY)) as client:
        for call in (
            client.call_tool("list_saved_searches", {}),
            client.read_resource(RESOURCE_URI),
            client.get_prompt("explain_law", {"law": "x"}),
        ):
            with pytest.raises((ToolError, MCPError)) as caught:
                await call
            texts.append(str(caught.value))

    assert len(texts) == 3
    for text in texts:
        for secret in ("alice", "4242", "zed", "id:"):
            assert secret not in text


@pytest.mark.parametrize(
    ("sub", "must_connect"),
    [
        ("abc", True),
        ("alice", True),
        ("", True),
        (" 1001", True),
        ("1001 ", True),
        ("1001\n", True),
        ("10.01", True),
        ("-1001", True),
        ("local", True),
        ("user:1001", True),
        ("github:1001", True),
        ("١٢", True),
        (None, True),
        (1001, False),  # a non-string sub may already fail the token's own validation
    ],
    ids=repr,
)
async def test_a_token_with_an_unusable_subject_never_gets_in_and_never_touches_the_store(
    boe_client: BoeClient,
    store: RecordingStore,
    keys: RSAKeyPair,
    router: respx.MockRouter,
    sub: Any,
    must_connect: bool,
) -> None:
    # "*" and the login both allow this user; only the unusable subject stands in the way.
    built, settings = signed_in_server(boe_client, store, keys, allowed=["*", "alice"])
    forged = auth.token(keys, sub=sub, login="alice")
    headers = {**auth.MCP_HEADERS, "Authorization": f"Bearer {forged}"}

    async with auth.serve(built, settings) as gateway:
        async with gateway.raw() as http:
            status = (await http.post("/mcp", headers=headers, json=auth.INITIALIZE)).status_code
        if status == 401:
            assert not must_connect
        else:
            async with gateway.session(forged) as client:
                assert await visible(client) == NOTHING
                with pytest.raises(ToolError):
                    await client.call_tool("save_search", {"name": "mine", "words": "x"})
                with pytest.raises(ToolError):
                    await client.call_tool("list_saved_searches", {})
                with pytest.raises(MCPError):
                    await client.read_resource(RESOURCE_URI)

    assert store.owners_seen == set()
    assert await store.list("local") == []


async def test_without_a_token_in_process_nothing_is_visible_and_nothing_is_local(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    # A signed-in server reached without any token (no HTTP layer to say 401) must fail closed.
    built, _ = signed_in_server(boe_client, store, keys, allowed=["*"])

    async with Client(built) as client:
        assert await visible(client) == NOTHING
        with pytest.raises(ToolError):
            await client.call_tool("save_search", {"name": "mine", "words": "x"})
        with pytest.raises(ToolError):
            await client.call_tool("list_saved_searches", {})
        with pytest.raises(MCPError):
            await client.read_resource(RESOURCE_URI)

    assert store.owners_seen == set()
    assert await store.list("local") == []


# --- Mode signed-in HTTP: isolation ---------------------------------------------------------------


async def test_two_users_save_under_their_own_github_owner_and_cannot_see_each_other(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, ALICE)) as alice:
            await structured(alice, "save_search", name="mine", words="Real Decreto")
        async with gateway.session(bearer(keys, BOB)) as bob:
            # The same name is free for Bob: names are per owner.
            await structured(bob, "save_search", name="mine", words="Ley Organica")
            await structured(bob, "save_search", name="bobs-only", words="Orden")

        async with gateway.session(bearer(keys, ALICE)) as alice:
            assert await names_of_saved_searches(alice) == ["mine"]
            assert await names_in_resource(alice) == ["mine"]
            listed = (await structured(alice, "list_saved_searches"))["searches"]
            assert [row["words"] for row in listed] == ["Real Decreto"]
        async with gateway.session(bearer(keys, BOB)) as bob:
            assert sorted(await names_of_saved_searches(bob)) == ["bobs-only", "mine"]
            assert sorted(await names_in_resource(bob)) == ["bobs-only", "mine"]

    assert {row.name: row.words for row in await store.list("github:1001")} == {
        "mine": "Real Decreto"
    }
    assert {row.name: row.words for row in await store.list("github:2002")} == {
        "mine": "Ley Organica",
        "bobs-only": "Orden",
    }
    # Only the two real owners were ever asked about: not "local", not anybody else.
    assert store.owners_seen == {"github:1001", "github:2002"}
    assert await store.list("local") == []


async def test_a_user_cannot_check_edit_or_delete_the_other_users_search(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, BOB)) as bob:
            await structured(bob, "save_search", name="bobs-only", words="Orden")
        async with gateway.session(bearer(keys, ALICE)) as alice:
            await structured(alice, "save_search", name="mine", words="Real Decreto")

            missing = "no saved search named 'bobs-only'"
            with pytest.raises(ToolError, match=missing):
                await alice.call_tool("check_saved_searches", {"name": "bobs-only"})
            with pytest.raises(ToolError, match=missing):
                await alice.call_tool(
                    "edit_saved_search", {"name": "bobs-only", "new_name": "stolen"}
                )
            with pytest.raises(ToolError, match=missing):
                await alice.call_tool("delete_saved_search", {"name": "bobs-only"})

            checked = await structured(alice, "check_saved_searches")
            assert [row["name"] for row in checked["results"]] == ["mine"]

        async with gateway.session(bearer(keys, BOB)) as bob:
            assert await names_of_saved_searches(bob) == ["bobs-only"]
            assert await names_in_resource(bob) == ["bobs-only"]

    bobs = await store.list("github:2002")
    assert [(row.name, row.words) for row in bobs] == [("bobs-only", "Orden")]


async def test_deleting_or_renaming_a_same_named_search_leaves_the_other_users_one(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, ALICE)) as alice:
            await structured(alice, "save_search", name="same", words="Real Decreto")
        async with gateway.session(bearer(keys, BOB)) as bob:
            await structured(bob, "save_search", name="same", words="Orden")
        async with gateway.session(bearer(keys, ALICE)) as alice:
            await structured(alice, "delete_saved_search", name="same")
            await structured(alice, "save_search", name="again", words="Ley")
        async with gateway.session(bearer(keys, BOB)) as bob:
            kept = (await structured(bob, "list_saved_searches"))["searches"]
            assert [(row["name"], row["words"]) for row in kept] == [("same", "Orden")]
            await structured(bob, "edit_saved_search", name="same", new_name="renamed")

    assert [row.name for row in await store.list("github:1001")] == ["again"]
    assert [row.name for row in await store.list("github:2002")] == ["renamed"]


async def test_a_renamed_login_with_the_same_id_keeps_the_same_data(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    # Allowed by id on purpose: after the rename the username entry would no longer match.
    built, settings = signed_in_server(boe_client, store, keys, allowed=["id:1001"])

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, ("1001", "alice"))) as before:
            await structured(before, "save_search", name="mine", words="Real Decreto")
        async with gateway.session(bearer(keys, ("1001", "alice-renamed"))) as after:
            assert await names_of_saved_searches(after) == ["mine"]
            assert await names_in_resource(after) == ["mine"]

    assert [row.owner for row in await store.list("github:1001")] == ["github:1001"]
    assert store.owners_seen == {"github:1001"}


async def test_a_new_account_that_takes_over_an_old_login_does_not_inherit_its_data(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    # GitHub lets a login be released and re-registered: the new person has another numeric id.
    built, settings = signed_in_server(boe_client, store, keys, allowed=["alice"])

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, ("1001", "alice"))) as old:
            await structured(old, "save_search", name="mine", words="Real Decreto")
        async with gateway.session(bearer(keys, ("5555", "alice"))) as new:
            assert await names_of_saved_searches(new) == []
            assert await names_in_resource(new) == []

    assert [row.name for row in await store.list("github:1001")] == ["mine"]
    assert await store.list("github:5555") == []


async def test_the_limit_of_twenty_searches_is_per_user(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, ALICE)) as alice:
            for number in range(20):
                await structured(alice, "save_search", words=f"palabra{number}")
            with pytest.raises(ToolError):
                await alice.call_tool("save_search", {"words": "one too many"})
        async with gateway.session(bearer(keys, BOB)) as bob:
            saved = await structured(bob, "save_search", words="una")

    assert saved["total"] == 1


async def test_a_user_without_searches_reads_an_empty_resource_whatever_others_saved(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair
) -> None:
    built, settings = signed_in_server(boe_client, store, keys)

    async with auth.serve(built, settings) as gateway:
        async with gateway.session(bearer(keys, ALICE)) as alice:
            await structured(alice, "save_search", name="mia", words="vivienda")
        async with gateway.session(bearer(keys, BOB)) as bob:
            [content] = await bob.read_resource(RESOURCE_URI)

    listing = json.loads(content.text)  # type: ignore[union-attr]
    assert listing["searches"] == []
    assert listing["note"] == "No saved searches. Create one with save_search."


# --- A server built by _build -----------------------------------------------------------------


class FakeGitHubProvider:
    """Stands in for ``GitHubProvider``: records its arguments, returns the JWT stand-in."""

    def __init__(self, keys: RSAKeyPair) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verifier: JWTVerifier = auth.verifier_for(keys)

    def __call__(self, **kwargs: Any) -> JWTVerifier:
        self.calls.append(kwargs)
        return self.verifier


def build_settings(tmp_path: Path, **changes: Any) -> Settings:
    """Settings for ``_build``: memory cache and private directories, so nothing leaves tmp."""
    base: dict[str, Any] = {
        "cache_backend": "memory",
        "cache_dir": tmp_path / "cache",
        "data_dir": tmp_path / "data",
        "min_interval": 0.0,
        "transport": "http",
        "host": "127.0.0.1",
        "github_client_id": "Iv1.client",
        "github_client_secret": "client-secret-VALUE-1234",
        "auth_signing_key": "signing-key-VALUE-" + "k" * 32,
        "base_url": "https://boe.example.org",
        "allowed_users": ("alice",),
    }
    return Settings(**{**base, **changes})


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch, keys: RSAKeyPair) -> FakeGitHubProvider:
    """Replace the GitHub provider that ``server`` uses (no network, no stored state)."""
    fake = FakeGitHubProvider(keys)
    monkeypatch.setattr(server, "AllowListedGitHubProvider", fake)
    return fake


def test_build_constructs_the_github_provider_from_the_settings(
    tmp_path: Path, fake_provider: FakeGitHubProvider
) -> None:
    settings = build_settings(tmp_path)

    built = server._build(settings)

    [call] = fake_provider.calls
    assert call["client_id"] == settings.github_client_id
    assert call["client_secret"] == settings.github_client_secret
    assert call["base_url"] == "https://boe.example.org"
    assert call["jwt_signing_key"] == settings.auth_signing_key
    assert call["cache_ttl_seconds"] == 300
    assert built.auth is fake_provider.verifier


def test_build_asks_github_for_no_scope_that_can_write(
    tmp_path: Path, fake_provider: FakeGitHubProvider
) -> None:
    server._build(build_settings(tmp_path))

    [call] = fake_provider.calls
    # Explicit, because the provider's own default is ["user"], which grants profile write access.
    assert "required_scopes" in call
    scopes = call["required_scopes"]
    assert scopes is not None
    assert "user" not in scopes
    assert set(scopes) <= {"read:user"}


def test_build_passes_the_normalised_base_url(
    tmp_path: Path, fake_provider: FakeGitHubProvider
) -> None:
    settings = Settings.from_env(
        {
            "BOE_INSIGHTS_TRANSPORT": "http",
            "BOE_INSIGHTS_CACHE": "memory",
            "BOE_INSIGHTS_CACHE_DIR": str(tmp_path / "cache"),
            "BOE_INSIGHTS_DATA_DIR": str(tmp_path / "data"),
            "BOE_INSIGHTS_GITHUB_CLIENT_ID": "id",
            "BOE_INSIGHTS_GITHUB_CLIENT_SECRET": "secret-value-0123",
            "BOE_INSIGHTS_AUTH_SIGNING_KEY": "k" * 40,
            "BOE_INSIGHTS_BASE_URL": "https://boe.example.org/",
        }
    )

    server._build(settings)

    assert fake_provider.calls[0]["base_url"] == "https://boe.example.org"


def test_build_creates_no_provider_over_stdio_even_with_login_variables(
    tmp_path: Path, fake_provider: FakeGitHubProvider
) -> None:
    built = server._build(build_settings(tmp_path, transport="stdio"))

    assert fake_provider.calls == []
    assert built.auth is None


def test_build_creates_no_provider_when_login_is_off(
    tmp_path: Path, fake_provider: FakeGitHubProvider
) -> None:
    settings = build_settings(
        tmp_path,
        github_client_id=None,
        github_client_secret=None,
        auth_signing_key=None,
        base_url=None,
    )

    built = server._build(settings)

    assert fake_provider.calls == []
    assert built.auth is None


async def test_a_built_signed_in_server_applies_the_allow_list_and_serves_health(
    tmp_path: Path,
    fake_provider: FakeGitHubProvider,
    keys: RSAKeyPair,
    router: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = build_settings(tmp_path, allowed_users=("alice",))
    built = server._build(settings)

    with caplog.at_level(logging.DEBUG):
        async with auth.serve(built, settings) as gateway:
            async with gateway.raw() as http:
                assert (await http.get("/health")).status_code == 200
                no_token = await http.post("/mcp", headers=auth.MCP_HEADERS, json=auth.INITIALIZE)
                assert no_token.status_code == 401
            async with gateway.session(bearer(keys, ALICE)) as alice:
                assert await visible(alice) == EVERYTHING
                await structured(alice, "save_search", name="mine", words="Real Decreto")
                assert await names_of_saved_searches(alice) == ["mine"]
            async with gateway.session(bearer(keys, MALLORY)) as mallory:
                assert await visible(mallory) == NOTHING

    # Neither the client secret nor the signing key may reach a log record.
    assert settings.github_client_secret and settings.auth_signing_key
    assert settings.github_client_secret not in caplog.text
    assert settings.auth_signing_key not in caplog.text


async def test_a_built_open_http_server_has_no_saved_search_components(
    tmp_path: Path, fake_provider: FakeGitHubProvider, router: respx.MockRouter
) -> None:
    settings = build_settings(
        tmp_path,
        github_client_id=None,
        github_client_secret=None,
        auth_signing_key=None,
        base_url=None,
        allowed_users=(),
    )
    built = server._build(settings)

    async with Client(built) as client:
        listed = await visible(client)

    assert listed["tools"] == OTHER_TOOLS
    assert listed["resources"] == set()
    assert "save_search" not in (client.instructions or "")


async def test_a_built_stdio_server_is_local_with_everything(
    tmp_path: Path, fake_provider: FakeGitHubProvider, router: respx.MockRouter
) -> None:
    built = server._build(build_settings(tmp_path, transport="stdio"))

    async with Client(built) as client:
        assert await visible(client) == EVERYTHING


# --- A server built without settings but served over HTTP ---------------------------------------


async def test_a_server_built_without_settings_never_uses_the_local_owner_over_http(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    # What ``fastmcp run module:create_server`` over HTTP would do: no settings, so the server
    # believes it is local and exposes the saved-search tools, yet every request is remote.
    built = create_server(client=boe_client, store=store)

    async with auth.session(built, Settings(transport="http")) as client:
        listing_error = None
        try:
            await client.call_tool("list_saved_searches", {})
        except ToolError as error:
            listing_error = str(error)
        save_error = None
        try:
            await client.call_tool("save_search", {"name": "mia", "words": "vivienda"})
        except ToolError as error:
            save_error = str(error)

    expected = (
        "Not signed in (unauthorized, not retryable): saved searches need an identified user."
    )
    assert listing_error == expected
    assert save_error == expected
    assert store.owners_seen == set()
    assert await store.list("local") == []


async def test_the_local_owner_still_works_over_stdio_and_in_memory(
    boe_client: BoeClient, store: RecordingStore
) -> None:
    async with Client(local_server(boe_client, store)) as client:
        await structured(client, "save_search", name="mia", words="vivienda")
        assert await names_of_saved_searches(client) == ["mia"]

    assert store.owners_seen == {"local"}


# --- An authentication provider means nothing over stdio ----------------------------------------


@pytest.mark.parametrize("settings", [Settings(transport="stdio"), None])
async def test_a_provider_is_ignored_when_the_server_is_not_served_over_http(
    boe_client: BoeClient, store: RecordingStore, keys: RSAKeyPair, settings: Settings | None
) -> None:
    built = create_server(
        client=boe_client, settings=settings, store=store, auth=auth.verifier_for(keys)
    )

    # No authentication on the server and no allow-list middleware: a client that presents no
    # token still sees every component, saved searches included, and owns ``local`` rows.
    assert built.auth is None
    async with Client(built) as client:
        assert await visible(client) == EVERYTHING
        await structured(client, "save_search", name="mia", words="vivienda")
    assert store.owners_seen == {"local"}
