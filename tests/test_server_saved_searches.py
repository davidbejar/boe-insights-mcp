"""Tests of the saved-search tools through FastMCP's in-memory client; HTTP is mocked with respx.

The clock is fixed by replacing ``utc_now`` as ``saved_searches`` sees it, so "today" moves only
when a test moves it. The cache never stores anything, so a changed mocked response is seen by
the next call (a real cache would keep the old one). Nothing here writes to the real user
directories: servers get an injected client, or settings pointing into ``tmp_path``.
"""

from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp import MCPError
from mcp.types import Tool

from boe_insights_mcp import saved_searches
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import CheckSearchesResult, SavedSearchList, SaveSearchResult
from boe_insights_mcp.searchstore import SavedSearchStore
from boe_insights_mcp.server import create_server
from boe_insights_mcp.settings import Settings

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
SUMMARY_URL = f"{BASE_URL}boe/sumario/"
LEY_40 = "BOE-A-2015-10566"
Loader = Callable[[str], bytes]
Xml = Callable[..., httpx.Response]
Json = Callable[..., httpx.Response]


class NoCache(MemoryCache):
    """A cache that remembers nothing, so every call reaches the (mocked) BOE."""

    async def set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        return None


class Clock:
    """The current moment of the saved-search use cases."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Friday 2026-11-19, ten o'clock UTC; tests move ``clock.moment``."""
    fixed = Clock(datetime(2026, 11, 19, 10, 0, tzinfo=UTC))
    monkeypatch.setattr(
        saved_searches, "utc_now", lambda now=None: now if now is not None else fixed.moment
    )
    return fixed


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(cache=NoCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0)


@pytest.fixture
async def mcp(boe_client: BoeClient, clock: Clock) -> AsyncIterator[Client[Any]]:
    async with Client(create_server(client=boe_client)) as client:
        yield client


async def call(mcp: Client[Any], tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


@pytest.fixture(autouse=True)
def thursday_without_an_issue(
    router: respx.MockRouter, xml_response: Xml, load_fixture: Loader
) -> respx.Route:
    """Saving a gazette search reads today's issue (Thursday 2026-11-19): not out yet."""
    return router.get(f"{SUMMARY_URL}20261119").mock(
        return_value=xml_response(load_fixture("error_404_sumario_sunday_20261115.xml"), 404)
    )


def friday_route(router: respx.MockRouter, xml_response: Xml, load_fixture: Loader) -> respx.Route:
    """2026-11-20 answers with the recorded ordinary day."""
    return router.get(f"{SUMMARY_URL}20261120").mock(
        return_value=xml_response(load_fixture("sumario_boe_20261120_trimmed.xml"), 200)
    )


def ley_40_routes(
    router: respx.MockRouter, json_response: Json, load_fixture: Loader, *extra: dict[str, Any]
) -> respx.Route:
    """Metadata and analysis of Ley 40/2015; ``extra`` references are added to its later ones."""
    prefix = f"{SEARCH_URL}/id/{LEY_40}"
    router.get(f"{prefix}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    body = json.loads(load_fixture("analisis_ley40_2015.json"))
    later = body["data"][0]["referencias"]["posteriores"][0]["posterior"]
    later.extend(extra)
    return router.get(f"{prefix}/analisis").mock(
        return_value=json_response(json.dumps(body).encode())
    )


def new_reference(law_id: str, code: str, relation: str, text: str) -> dict[str, Any]:
    """A reference in the shape of the BOE analysis."""
    return {"id_norma": law_id, "relacion": {"codigo": code, "texto": relation}, "texto": text}


# --- discovery ---------------------------------------------------------------------------------


async def test_the_tools_carry_the_agreed_descriptions(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    def words(name: str) -> str:
        return " ".join((tools[name].description or "").split())

    assert words("save_search") == (
        "Save a search to re-check later: gazette items by title words, section or department, "
        "or one law to follow for amendments. Nothing is sent automatically; call "
        "check_saved_searches to see what is new. A name is proposed when none is given."
    )
    assert words("list_saved_searches") == (
        "The saved searches, with their filters and when each was last checked."
    )
    assert words("check_saved_searches") == (
        "What is new for the saved searches since each was last checked, and mark it as seen. "
        "Checks at most the last 31 days; pass since to re-read up to 90 days back without "
        "marking anything. When complete is false, call again to continue."
    )
    assert words("edit_saved_search") == (
        "Rename a saved search or change its gazette filters. Its last-checked point is kept."
    )
    assert words("delete_saved_search") == "Delete a saved search. This cannot be undone."


async def test_the_parameters_carry_the_agreed_descriptions(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    def described(name: str) -> dict[str, str]:
        properties = tools[name].input_schema["properties"]
        return {key: schema["description"] for key, schema in properties.items()}

    save = described("save_search")
    assert save["name"] == "Name for the search; default: one built from its filters."
    assert save["law_id"] == (
        "BOE identifier of a consolidated law to follow, e.g. BOE-A-2015-10566 "
        "(use resolve_reference)."
    )
    assert save["words"] == (
        "Words that must all appear in the title (whole words; singular and plural forms both "
        "match)."
    )
    assert save["section"] == "Only this section code, e.g. '1', '2A', '3', '5B'."
    assert save["department_code"] == "Only this department code. Look codes up with find_codes."
    check = described("check_saved_searches")
    assert check["name"] == "Name of the saved search (see list_saved_searches)."
    assert check["since"] == (
        "Re-read from this date (at most 90 days back) without marking anything as seen."
    )
    assert check["limit"] == "Items or changes shown per search."
    edit = described("edit_saved_search")
    for filter_name in ("words", "section", "department_code"):
        assert edit[filter_name] == "New value; an empty string removes this filter."
    assert edit["name"] == "Name of the saved search (see list_saved_searches)."
    assert described("delete_saved_search") == {
        "name": "Name of the saved search (see list_saved_searches)."
    }


async def test_no_tool_takes_an_owner(mcp: Client[Any]) -> None:
    # The owner is whoever the request authenticates, never something the model can write.
    for tool in await mcp.list_tools():
        assert "owner" not in tool.input_schema.get("properties", {})


async def test_the_required_arguments_and_the_limit_bounds(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    assert tools["save_search"].input_schema.get("required", []) == []
    assert tools["check_saved_searches"].input_schema.get("required", []) == []
    assert tools["edit_saved_search"].input_schema["required"] == ["name"]
    assert tools["delete_saved_search"].input_schema["required"] == ["name"]
    limit = tools["check_saved_searches"].input_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 50, 20)


async def test_the_instructions_tell_the_model_how_saved_searches_work(
    mcp: Client[Any],
) -> None:
    instructions = mcp.instructions or ""

    assert "nothing is sent automatically" in instructions
    assert "call check_saved_searches and repeat it while complete is false" in instructions
    assert "live on the server" in instructions


# --- the full round trip: a gazette search -----------------------------------------------------


async def test_a_gazette_search_from_save_to_delete(
    mcp: Client[Any],
    thursday_without_an_issue: respx.Route,
    clock: Clock,
    router: respx.MockRouter,
    xml_response: Xml,
    load_fixture: Loader,
) -> None:
    thursday = thursday_without_an_issue
    # Thursday 2026-11-19: save. Today's issue is read once, for the baseline: it is not out.
    saved = SaveSearchResult.model_validate(await call(mcp, "save_search", words="Real Decreto"))
    assert saved.search.name == "real-decreto"
    assert saved.name_was_proposed is True
    assert saved.total == 1
    assert saved.note is not None
    assert "The first check reports what is published from now on." in saved.note
    assert thursday.call_count == 1

    listed = SavedSearchList.model_validate(await call(mcp, "list_saved_searches"))
    assert [s.name for s in listed.searches] == ["real-decreto"]
    assert listed.searches[0].checked_through == date(2026, 11, 18)
    assert listed.searches[0].last_checked_at is None

    # Check the same day: nothing is new; today's issue is asked for again.
    first = CheckSearchesResult.model_validate(await call(mcp, "check_saved_searches"))
    assert first.complete is True
    assert first.results[0].matched == 0
    assert first.note is not None and "No issue for today" in first.note
    assert thursday.call_count == 2

    # Friday: a new issue appears. The check reports it.
    clock.moment = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    friday = friday_route(router, xml_response, load_fixture)
    second = CheckSearchesResult.model_validate(await call(mcp, "check_saved_searches"))
    gazette_check = second.results[0]
    assert (gazette_check.status, second.complete) == ("checked", True)
    assert (gazette_check.from_date, gazette_check.to_date) == (
        date(2026, 11, 19),
        date(2026, 11, 20),
    )
    assert gazette_check.matched is not None and gazette_check.matched >= 4
    assert gazette_check.items is not None
    assert all("Real Decreto" in entry.title for entry in gazette_check.items)
    assert {entry.date for entry in gazette_check.items} == {date(2026, 11, 20)}
    assert friday.call_count == 1
    assert second.provenance.data_updated_at == datetime(2026, 11, 20, tzinfo=UTC)

    # Checking again reports nothing new; today's issue is read again (it can still grow).
    third = CheckSearchesResult.model_validate(await call(mcp, "check_saved_searches"))
    assert third.results[0].matched == 0
    assert third.results[0].items == []
    assert friday.call_count == 2

    # since re-reads without moving anything.
    again = CheckSearchesResult.model_validate(
        await call(mcp, "check_saved_searches", since="2026-11-20")
    )
    assert again.results[0].matched == gazette_check.matched
    after = SavedSearchList.model_validate(await call(mcp, "list_saved_searches"))
    # Thursday answered "no issue" and a weekday's 404 is not trusted for a week, so the
    # bookmark waits at Wednesday while what was delivered for Friday is remembered.
    assert after.searches[0].checked_through == date(2026, 11, 18)
    assert after.searches[0].last_checked_at == third.provenance.generated_at

    # Edit: rename and add a section; the bookmark stays.
    edited = SaveSearchResult.model_validate(
        await call(mcp, "edit_saved_search", name="real-decreto", new_name="decretos", section="1")
    )
    assert edited.search.name == "decretos"
    assert edited.search.section == "1"
    assert edited.search.checked_through == date(2026, 11, 18)
    assert edited.name_was_proposed is False

    # Delete.
    deleted = await call(mcp, "delete_saved_search", name="decretos")
    assert (deleted["deleted"], deleted["remaining"]) == ("decretos", 0)
    empty = await call(mcp, "list_saved_searches")
    assert empty["searches"] == []
    assert empty["note"] == "No saved searches. Create one with save_search."


# --- the full round trip: a law search ---------------------------------------------------------


async def test_a_law_search_reports_a_new_amendment_once(
    mcp: Client[Any],
    router: respx.MockRouter,
    json_response: Json,
    load_fixture: Loader,
) -> None:
    ley_40_routes(router, json_response, load_fixture)

    saved = SaveSearchResult.model_validate(
        await call(mcp, "save_search", law_id="boe-a-2015-10566")
    )
    assert saved.search.name == "ley-40-2015"
    assert saved.search.kind == "law"
    assert saved.search.law_id == LEY_40
    assert saved.note is not None
    assert (
        "The first check reports changes recorded from now on "
        "(the BOE data read can be up to 6 hours old)."
    ) in saved.note

    quiet = CheckSearchesResult.model_validate(await call(mcp, "check_saved_searches"))
    assert quiet.results[0].changes == []
    assert quiet.results[0].other_new is None

    # The BOE records a new amendment and a new court ruling on the law.
    ley_40_routes(
        router,
        json_response,
        load_fixture,
        new_reference("BOE-A-2026-9001", "270", "SE MODIFICA", "Art. 5"),
        new_reference("BOE-A-2026-9002", "470", "SENTENCIA", "STC 1/2026"),
    )
    news = CheckSearchesResult.model_validate(await call(mcp, "check_saved_searches"))
    law_check = news.results[0]
    assert [(c.law_id, c.kind, c.relation) for c in law_check.changes or []] == [
        ("BOE-A-2026-9001", "modifies", "SE MODIFICA")
    ]
    assert law_check.changes_total == 1
    assert law_check.other_new == {"court_rulings": 1}
    assert law_check.status == "checked"
    assert news.complete is True

    nothing = CheckSearchesResult.model_validate(await call(mcp, "check_saved_searches"))
    assert nothing.results[0].changes == []
    assert nothing.results[0].other_new is None

    # A law search can only be renamed.
    with pytest.raises(ToolError, match="a law search can only be renamed"):
        await call(mcp, "edit_saved_search", name="ley-40-2015", words="vivienda")
    renamed = await call(mcp, "edit_saved_search", name="ley-40-2015", new_name="lrjsp")
    assert renamed["search"]["name"] == "lrjsp"
    assert renamed["search"]["law_id"] == LEY_40


async def test_since_on_a_law_search_stores_nothing(
    mcp: Client[Any],
    router: respx.MockRouter,
    json_response: Json,
    load_fixture: Loader,
) -> None:
    ley_40_routes(router, json_response, load_fixture)
    await call(mcp, "save_search", law_id=LEY_40)
    ley_40_routes(
        router,
        json_response,
        load_fixture,
        new_reference("BOE-A-2026-9001", "270", "SE MODIFICA", "Art. 5"),
    )

    peek = await call(mcp, "check_saved_searches", since="2026-10-01")
    real = await call(mcp, "check_saved_searches")

    # A law has no dated history to re-read: since leaves it alone and says where to look.
    assert peek["results"][0]["changes"] == []
    assert "get_amendment_history" in peek["results"][0]["note"]
    assert [c["law_id"] for c in real["results"][0]["changes"]] == ["BOE-A-2026-9001"]


# --- errors ------------------------------------------------------------------------------------


async def test_saving_nothing_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*give either law_id"):
        await call(mcp, "save_search")


async def test_an_unknown_name_is_an_invalid_argument(mcp: Client[Any]) -> None:
    for tool in ("check_saved_searches", "edit_saved_search", "delete_saved_search"):
        arguments = {"name": "nada", **({"new_name": "x"} if tool == "edit_saved_search" else {})}
        with pytest.raises(ToolError, match=r"invalid_argument.*no saved search named 'nada'"):
            await call(mcp, tool, **arguments)


async def test_a_taken_name_is_an_invalid_argument(mcp: Client[Any]) -> None:
    await call(mcp, "save_search", words="vivienda", name="mia")

    with pytest.raises(ToolError, match=r"invalid_argument.*named 'mia' already exists"):
        await call(mcp, "save_search", words="otra", name="mia")


async def test_since_out_of_range_is_an_invalid_argument(mcp: Client[Any]) -> None:
    await call(mcp, "save_search", words="vivienda")

    with pytest.raises(ToolError, match=r"invalid_argument.*since may be at most 90 days"):
        await call(mcp, "check_saved_searches", since="2026-01-01")
    with pytest.raises(ToolError, match=r"invalid_argument.*since is in the future"):
        await call(mcp, "check_saved_searches", since="2026-12-01")


@pytest.mark.parametrize("limit", [0, 51])
async def test_the_limit_is_checked_by_the_schema(mcp: Client[Any], limit: int) -> None:
    with pytest.raises(ToolError):
        await call(mcp, "check_saved_searches", limit=limit)


async def test_an_unknown_law_is_reported_as_not_found(
    mcp: Client[Any], router: respx.MockRouter, xml_response: Xml, load_fixture: Loader
) -> None:
    router.get(f"{SEARCH_URL}/id/BOE-A-2000-1/metadatos").mock(
        return_value=xml_response(load_fixture("error_404_bloque_no_in_force_law.xml"), 404)
    )

    with pytest.raises(ToolError, match="not_found"):
        await call(mcp, "save_search", law_id="BOE-A-2000-1")

    assert (await call(mcp, "list_saved_searches"))["searches"] == []


async def test_the_twenty_first_search_is_an_invalid_argument(mcp: Client[Any]) -> None:
    for number in range(20):
        await call(mcp, "save_search", words=f"palabra{number}")

    with pytest.raises(ToolError, match=r"invalid_argument.*limit of 20 saved searches"):
        await call(mcp, "save_search", words="una mas")


# --- progress ----------------------------------------------------------------------------------


async def test_a_check_reports_progress(
    mcp: Client[Any],
    clock: Clock,
    router: respx.MockRouter,
    xml_response: Xml,
    load_fixture: Loader,
) -> None:
    await call(mcp, "save_search", words="Real Decreto")
    clock.moment = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    friday_route(router, xml_response, load_fixture)
    reports: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        reports.append((progress, total, message))

    await mcp.call_tool("check_saved_searches", {}, progress_handler=on_progress)

    assert reports == [
        (1, 2, "Read 1 of 2 laws and days."),
        (2, 2, "Read 2 of 2 laws and days."),
    ]


# --- owners ------------------------------------------------------------------------------------


def authenticate_as(monkeypatch: pytest.MonkeyPatch, subject: str | None) -> None:
    """Make the server see a request authenticated as ``subject`` (``None``: no token)."""
    token = None if subject is None else SimpleNamespace(claims={"sub": subject})
    monkeypatch.setattr("boe_insights_mcp.owner.get_access_token", lambda: token)


async def test_each_user_sees_only_their_own_searches(
    mcp: Client[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticate_as(monkeypatch, "alice")
    await call(mcp, "save_search", words="vivienda", name="privada")
    authenticate_as(monkeypatch, "bob")
    await call(mcp, "save_search", words="otra cosa", name="privada")

    bobs = await call(mcp, "list_saved_searches")
    assert [s["words"] for s in bobs["searches"]] == ["otra cosa"]
    authenticate_as(monkeypatch, "carol")
    assert (await call(mcp, "list_saved_searches"))["searches"] == []
    for tool in ("check_saved_searches", "delete_saved_search"):
        with pytest.raises(ToolError, match="no saved search named 'privada'"):
            await call(mcp, tool, name="privada")
    with pytest.raises(ToolError, match="no saved search named 'privada'"):
        await call(mcp, "edit_saved_search", name="privada", new_name="mia")
    authenticate_as(monkeypatch, "alice")
    alices = await call(mcp, "list_saved_searches")
    assert [s["words"] for s in alices["searches"]] == ["vivienda"]


async def test_the_limit_of_twenty_is_per_user(
    mcp: Client[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticate_as(monkeypatch, "alice")
    for number in range(20):
        await call(mcp, "save_search", words=f"palabra{number}")
    authenticate_as(monkeypatch, "bob")

    saved = await call(mcp, "save_search", words="una")

    assert saved["total"] == 1


async def test_a_token_without_a_subject_is_refused_not_shared(
    mcp: Client[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "boe_insights_mcp.owner.get_access_token", lambda: SimpleNamespace(claims={})
    )

    with pytest.raises(ToolError):
        await call(mcp, "list_saved_searches")


# --- lifespan and the store --------------------------------------------------------------------


async def test_an_injected_store_is_used_and_left_open(boe_client: BoeClient, clock: Clock) -> None:
    store = SavedSearchStore(None)
    async with Client(create_server(client=boe_client, store=store)) as client:
        await call(client, "save_search", words="vivienda", name="mia")

    # Still usable: the server did not close what it did not create.
    assert await store.names("local") == {"mia"}
    await store.close()


async def test_an_injected_client_without_a_store_gets_an_in_memory_one(
    boe_client: BoeClient, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOE_INSIGHTS_DATA_DIR", str(tmp_path / "data"))

    async with Client(create_server(client=boe_client)) as client:
        await call(client, "save_search", words="vivienda")
    async with Client(create_server(client=boe_client)) as client:
        assert (await call(client, "list_saved_searches"))["searches"] == []

    assert not (tmp_path / "data").exists()


async def test_the_store_the_server_created_is_closed_on_shutdown(
    boe_client: BoeClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[SavedSearchStore] = []
    original = SavedSearchStore.close

    async def recording_close(self: SavedSearchStore) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(SavedSearchStore, "close", recording_close)

    async with Client(create_server(client=boe_client)) as client:
        await call(client, "list_saved_searches")
        assert closed == []

    assert len(closed) == 1


async def test_an_injected_store_is_never_closed_by_the_server(
    boe_client: BoeClient, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[SavedSearchStore] = []
    original = SavedSearchStore.close

    async def recording_close(self: SavedSearchStore) -> None:
        closed.append(self)
        await original(self)

    monkeypatch.setattr(SavedSearchStore, "close", recording_close)
    store = SavedSearchStore(None)

    async with Client(create_server(client=boe_client, store=store)) as client:
        await call(client, "list_saved_searches")

    assert closed == []
    await store.close()


async def test_the_settings_decide_where_the_store_lives_and_it_survives_a_restart(
    clock: Clock, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(
        cache_backend="memory", data_dir=data_dir, data_dir_set=True, min_interval=0
    )

    async with Client(create_server(settings=settings)) as client:
        await call(client, "save_search", words="vivienda", name="mia")
    path = data_dir / "saved_searches.sqlite3"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700

    async with Client(create_server(settings=settings)) as client:
        listed = await call(client, "list_saved_searches")

    assert [s["name"] for s in listed["searches"]] == ["mia"]


async def test_the_memory_backend_without_a_data_dir_keeps_searches_in_memory(
    clock: Clock, tmp_path: Path
) -> None:
    settings = Settings(
        cache_backend="memory", data_dir=tmp_path / "data", data_dir_set=False, min_interval=0
    )

    async with Client(create_server(settings=settings)) as client:
        await call(client, "save_search", words="vivienda")

    assert not (tmp_path / "data").exists()


# --- resources and prompts on the real server --------------------------------------------------


async def test_the_server_registers_the_resources_and_prompts(mcp: Client[Any]) -> None:
    assert [str(r.uri) for r in await mcp.list_resources()] == ["boe://saved-searches"]
    templates = {t.uri_template for t in await mcp.list_resource_templates()}
    assert templates == {
        "boe://law/{law_id}",
        "boe://law/{law_id}/article/{block_id}",
        "boe://gazette/{day}",
        "boe://codes/{kind}",
    }
    assert len(await mcp.list_prompts()) == 8


async def test_the_saved_searches_resource_mirrors_the_tool(
    mcp: Client[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticate_as(monkeypatch, "alice")
    await call(mcp, "save_search", words="vivienda", name="mia")

    contents = await mcp.read_resource("boe://saved-searches")
    tool = await call(mcp, "list_saved_searches")

    assert contents[0].mime_type == "application/json"
    text = getattr(contents[0], "text", "")
    resource = json.loads(text)
    assert resource["searches"] == tool["searches"]
    assert resource["total"] == 1
    assert resource["provenance"]["attribution"].startswith("Basado en datos")


async def test_the_saved_searches_resource_is_per_owner(
    mcp: Client[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticate_as(monkeypatch, "alice")
    await call(mcp, "save_search", words="vivienda", name="mia")
    authenticate_as(monkeypatch, "bob")

    contents = await mcp.read_resource("boe://saved-searches")
    bobs = json.loads(getattr(contents[0], "text", ""))

    assert bobs["searches"] == []
    assert bobs["note"] == "No saved searches. Create one with save_search."


async def test_the_saved_searches_resource_refuses_a_token_without_a_subject(
    mcp: Client[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "boe_insights_mcp.owner.get_access_token", lambda: SimpleNamespace(claims={})
    )

    with pytest.raises(MCPError):
        await mcp.read_resource("boe://saved-searches")
