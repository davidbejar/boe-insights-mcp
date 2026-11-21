"""MCP-level tests of ``get_law_structure`` and of ``first_published`` in ``get_law_overview``.

The server runs in memory through FastMCP's ``Client``; HTTP is mocked with respx and answers from
recorded fixtures (``texto_full_*`` for the text, the probe-3 ``metadatos_*``/``analisis_*`` bodies
for the publication gazette).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Tool

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import LawOverview, LawStructureResult
from boe_insights_mcp.server import create_server

PREFIX = f"{BASE_URL}legislacion-consolidada/id"
LEY_40 = "BOE-A-2015-10566"
ASTURIAS = "BOE-A-2026-23272"
DOGC = "DOGC-f-2017-90436"

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
async def mcp(router: respx.MockRouter, http: httpx.AsyncClient) -> AsyncIterator[Client[Any]]:
    boe = BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )
    async with Client(create_server(client=boe)) as client:
        yield client


@pytest.fixture
def serve(
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> Callable[..., None]:
    """Factory mocking the endpoints of one law; ``None`` leaves an endpoint unmocked."""

    def _serve(
        law_id: str,
        *,
        metadata: str | None = None,
        text: str | None = None,
        analysis: str | None = None,
    ) -> None:
        if metadata:
            router.get(f"{PREFIX}/{law_id}/metadatos").mock(
                return_value=json_response(load_fixture(metadata))
            )
        if text:
            router.get(f"{PREFIX}/{law_id}/texto").mock(
                return_value=xml_response(load_fixture(text), 200)
            )
        if analysis:
            router.get(f"{PREFIX}/{law_id}/analisis").mock(
                return_value=json_response(load_fixture(analysis))
            )
            # No law of these tests has an initial note: the BOE answers 404 for block "no".
            router.get(f"{PREFIX}/{law_id}/texto/bloque/no").mock(
                return_value=xml_response(load_fixture("error_404_bloque_no_in_force_law.xml"), 404)
            )

    return _serve


async def call(mcp: Client[Any], name: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool and return its structured content."""
    result = await mcp.call_tool(name, arguments)
    assert result.structured_content is not None
    return result.structured_content


# --- discovery -----------------------------------------------------------------------------------


async def test_description_is_the_agreed_one_and_the_parameters_are_bounded(
    mcp: Client[Any],
) -> None:
    tool: Tool = next(t for t in await mcp.list_tools() if t.name == "get_law_structure")

    expected = (
        "Table of contents of a law: books, titles, chapters, sections and article headings, each "
        "with the block_id to pass to the article tools. Use it to find which article deals with "
        "a matter before reading it. Large codes need paging or detail='headings'."
    )
    assert " ".join((tool.description or "").split()) == expected
    properties = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["law_id"]
    assert properties["detail"]["enum"] == ["headings", "all"]
    assert properties["detail"]["default"] == "all"
    assert (properties["limit"]["minimum"], properties["limit"]["maximum"]) == (1, 400)
    assert properties["limit"]["default"] == 200
    assert properties["offset"]["minimum"] == 0
    assert "ISO" in properties["as_of"]["description"]


async def test_instructions_name_the_tool(mcp: Client[Any]) -> None:
    assert "get_law_structure" in (mcp.instructions or "")


# --- calls ---------------------------------------------------------------------------------------


async def test_default_call_returns_the_outline_and_provenance(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        LEY_40,
        metadata="metadatos_in_force_ley40_2015.json",
        text="texto_full_ley40_2015_trimmed.xml",
    )

    content = await call(mcp, "get_law_structure", law_id=LEY_40)

    result = LawStructureResult.model_validate(content)
    assert result.total_entries == 22
    assert result.entries[0].label == "TÍTULO PRELIMINAR"
    assert result.provenance.attribution.startswith("Basado en datos")
    assert "state" not in content["entries"][0]
    assert content["entries"][2] == {
        "block_id": "a1",
        "kind": "article",
        "level": 3,
        "label": "Artículo 1",
        "heading": "Objeto",
    }


async def test_paging_and_headings_through_the_tool(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        LEY_40,
        metadata="metadatos_in_force_ley40_2015.json",
        text="texto_full_ley40_2015_trimmed.xml",
    )

    page = await call(mcp, "get_law_structure", law_id=LEY_40, limit=5, offset=5)
    headings = await call(mcp, "get_law_structure", law_id=LEY_40, detail="headings")

    assert (page["returned"], page["has_more"], page["next_offset"]) == (5, True, 10)
    assert page["counts"]["article"] == 9
    assert headings["total_entries"] == 11
    assert {e["kind"] for e in headings["entries"]} == {
        "title",
        "chapter",
        "section",
        "subsection",
    }


async def test_a_law_not_yet_in_force_is_marked_state_by_state_and_in_the_note(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        ASTURIAS,
        metadata="metadatos_asturias_ley8_2026.json",
        text="texto_full_asturias_ley8_2026_not_yet_in_force_trimmed.xml",
    )

    content = await call(mcp, "get_law_structure", law_id=ASTURIAS, as_of="2026-11-21")

    assert content["as_of"] == "2026-11-21"
    assert all(e["state"] == "not_yet_in_force" for e in content["entries"])
    assert "not yet in force" in content["note"]


async def test_as_of_in_the_past_through_the_tool(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        "BOE-A-1889-4763",
        metadata="metadatos_in_force_ley40_2015.json",
        text="texto_full_codigo_civil_trimmed.xml",
    )

    content = await call(mcp, "get_law_structure", law_id="BOE-A-1889-4763", as_of="1900-01-01")

    first_title = next(e for e in content["entries"] if e["block_id"] == "tpreliminar")
    assert first_title["heading"].startswith("De las Leyes")


async def test_unknown_law_is_a_curated_not_found_error(
    mcp: Client[Any], router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    for endpoint in ("metadatos", "texto"):
        router.get(f"{PREFIX}/BOE-A-2000-1/{endpoint}").mock(
            return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
        )

    with pytest.raises(ToolError, match=r"not_found.*resolve_reference"):
        await mcp.call_tool("get_law_structure", {"law_id": "BOE-A-2000-1"})


@pytest.mark.parametrize(
    "arguments",
    [
        {"law_id": "not-an-id"},
        {"law_id": LEY_40, "limit": 0},
        {"law_id": LEY_40, "limit": 401},
        {"law_id": LEY_40, "offset": -1},
        {"law_id": LEY_40, "detail": "everything"},
        {"law_id": LEY_40, "as_of": "yesterday"},
    ],
)
async def test_bad_input_is_a_tool_error_and_sends_no_request(
    mcp: Client[Any], router: respx.MockRouter, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        await mcp.call_tool("get_law_structure", arguments)
    assert router.calls.call_count == 0


async def test_offset_beyond_the_end_is_an_invalid_argument_error(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        LEY_40,
        metadata="metadatos_in_force_ley40_2015.json",
        text="texto_full_ley40_2015_trimmed.xml",
    )

    with pytest.raises(ToolError, match=r"invalid_argument.*offset"):
        await mcp.call_tool("get_law_structure", {"law_id": LEY_40, "offset": 99})


# --- first_published in get_law_overview ---------------------------------------------------------


async def test_regional_law_reprinted_in_the_boe_gets_the_gazette_from_the_note(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        ASTURIAS,
        metadata="metadatos_asturias_ley8_2026.json",
        analysis="analisis_asturias_ley8_2026.json",
    )

    overview = LawOverview.model_validate(await call(mcp, "get_law_overview", law_id=ASTURIAS))

    assert overview.published_on == date(2026, 11, 6)  # the BOE reprint
    assert overview.first_published is not None
    assert (
        overview.first_published.gazette,
        overview.first_published.number,
        overview.first_published.date,
    ) == ("BOPA", "206", date(2026, 10, 23))


async def test_law_with_a_regional_gazette_identifier_is_its_own_first_publication(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        DOGC,
        metadata="metadatos_dogc_f_2017_90436.json",
        analysis="analisis_dogc_f_2017_90436.json",
    )

    content = await call(mcp, "get_law_overview", law_id=DOGC)

    assert content["first_published"] == {
        "gazette": "Diari Oficial de la Generalitat de Catalunya",
        "number": "7426",
        "date": "2017-08-03",
    }
    assert content["published_on"] == "2017-08-03"


async def test_state_law_has_no_first_publication(
    mcp: Client[Any], serve: Callable[..., None]
) -> None:
    serve(
        LEY_40,
        metadata="metadatos_in_force_ley40_2015.json",
        analysis="analisis_ley40_2015.json",
    )

    content = await call(mcp, "get_law_overview", law_id=LEY_40)

    assert content["first_published"] is None


async def test_regional_boe_law_without_a_matching_note_has_no_first_publication(
    mcp: Client[Any],
    router: respx.MockRouter,
    serve: Callable[..., None],
    json_response: JsonResponse,
) -> None:
    serve(
        ASTURIAS,
        metadata="metadatos_asturias_ley8_2026.json",
        analysis="analisis_asturias_ley8_2026.json",
    )
    router.get(f"{PREFIX}/{ASTURIAS}/analisis").mock(
        return_value=json_response(
            b'{"status": {"code": "200"}, "data": [{"notas": {"nota": "Vigencia hasta 2030."}}]}'
        )
    )

    content = await call(mcp, "get_law_overview", law_id=ASTURIAS)

    assert content["first_published"] is None


async def test_field_descriptions_of_the_publication_dates(mcp: Client[Any]) -> None:
    tools = {t.name: t for t in await mcp.list_tools()}
    overview = tools["get_law_overview"].output_schema
    assert overview is not None
    properties = overview["properties"]
    assert properties["published_on"]["description"] == (
        "Publication date in the gazette of record: the BOE for state laws and for regional "
        "laws reprinted there."
    )
    assert properties["first_published"]["description"] == (
        "For regional laws: the regional gazette, issue and date of first publication, which "
        "governs entry into force. Null for state laws or when unknown."
    )
    version_text = (
        "Date this version was published; for regional laws this is the regional gazette date."
    )
    for name in ("read_article", "get_article_history"):
        schema = tools[name].output_schema
        assert schema is not None
        assert version_text in published_on_descriptions(schema), name


def published_on_descriptions(schema: Any) -> list[str]:
    """Every description given to a ``published_on`` property anywhere in an output schema."""
    found: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "published_on" and isinstance(value, dict) and "description" in value:
                found.append(value["description"])
            else:
                found.extend(published_on_descriptions(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(published_on_descriptions(item))
    return found
