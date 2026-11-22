"""Tests of ``verify_citation`` and ``explore_amendment_chain`` through FastMCP's in-memory client.

HTTP is mocked with respx. Ley 40/2015 comes from recorded responses (its index and article 48);
the chain starts from the recorded analyses of Real Decreto-ley 8/2020 and four of the laws it
amended, with a catalogue built from a recorded hit.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
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
from boe_insights_mcp.schemas import AmendmentChainResult, VerifyCitationResult
from boe_insights_mcp.server import create_server

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
LEY_40 = "BOE-A-2015-10566"
RDL_8_2020 = "BOE-A-2020-3824"
# The laws RDL 8/2020 modified that have a recorded analysis, and the file of each.
AMENDED = {
    "BOE-A-2002-8628": "analisis_ley11_2002_BOE-A-2002-8628.json",
    "BOE-A-2003-13471": "analisis_ley19_2003_BOE-A-2003-13471.json",
    "BOE-A-2011-9617": "analisis_ley14_2011_BOE-A-2011-9617.json",
    "BOE-A-2015-2495": "analisis_orden_2015_BOE-A-2015-2495.json",
}

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def boe_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


@pytest.fixture
async def mcp(boe_client: BoeClient) -> AsyncIterator[Client[Any]]:
    async with Client(create_server(client=boe_client)) as client:
        yield client


@pytest.fixture
def ley_40(
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> None:
    """Ley 40/2015: metadata, index and article 48 (two versions); no initial note."""
    prefix = f"{SEARCH_URL}/id/{LEY_40}"
    router.get(f"{prefix}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{prefix}/texto/indice").mock(
        return_value=json_response(load_fixture("texto_indice_ley40_2015_trimmed.json"))
    )
    router.get(f"{prefix}/texto/bloque/a48").mock(
        return_value=httpx.Response(
            200,
            content=load_fixture("texto_bloque_a48_two_versions.xml"),
            headers={"Content-Type": "application/xml;charset=UTF-8"},
        )
    )
    router.get(f"{prefix}/texto/bloque/no").mock(
        return_value=xml_response(load_fixture("error_404_bloque_no_in_force_law.xml"), 404)
    )


@pytest.fixture
def chain_routes(
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> dict[str, respx.Route]:
    """The analyses of RDL 8/2020 and four laws it amended, and a catalogue with those four."""
    template = json.loads(load_fixture("search_bulk_sample_selected.json"))
    records = []
    for number, law_id in enumerate(AMENDED, start=1):
        record = dict(template["data"][0], identificador=law_id, titulo=f"Ley de prueba {number}")
        records.append(record)
    page = json.dumps({**template, "data": records}).encode()
    empty = json.dumps({**template, "data": []}).encode()

    def search(request: httpx.Request) -> httpx.Response:
        return json_response(empty if request.url.params.get("limit") == "1" else page)

    router.get(SEARCH_URL).mock(side_effect=search)
    routes = {
        law_id: router.get(f"{SEARCH_URL}/id/{law_id}/analisis").mock(
            return_value=json_response(load_fixture(name))
        )
        for law_id, name in {
            RDL_8_2020: "analisis_rdl8_2020_BOE-A-2020-3824.json",
            **AMENDED,
        }.items()
    }
    # Any other analysis (a law outside the sample) would be a request the tool must not make.
    router.get(url__regex=rf"{SEARCH_URL}/id/[^/]+/analisis").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    return routes


async def call(mcp: Client[Any], tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


# --- discovery ---------------------------------------------------------------------------------


async def test_the_instructions_mention_the_two_tools(boe_client: BoeClient) -> None:
    async with Client(create_server(client=boe_client)) as client:
        instructions = client.instructions or ""

    assert (
        "verify_citation takes names directly and checks whether a law and article exist and "
        "whether quoted words match the text."
    ) in instructions
    assert (
        "most_amended_laws, explore_amendment_chain and scan_gazette while complete is false"
        in (instructions)
    )
    assert "and explore_amendment_chain download the collection on first use" in instructions


async def test_the_descriptions_are_the_agreed_ones(mcp: Client[Any]) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    assert " ".join((tools["verify_citation"].description or "").split()) == (
        "Check citations of Spanish laws against the consolidated text: whether the law and "
        "article exist, their state on a date, and whether quoted words match the text, the "
        "current version or an older one. Reports facts, not legal correctness. Compares words, "
        "not meaning: a paraphrase does not match."
    )
    assert " ".join((tools["explore_amendment_chain"].description or "").split()) == (
        "Follow amendments between laws for up to three levels: the laws that amended a law and "
        "the laws that amended those (amended_by), or what a law amended (amends). A law two "
        "levels away amended the intermediate law, not necessarily the parts that touch the "
        "starting law. Examines a limited number of laws per call; when complete is false, call "
        "again to continue."
    )


async def test_the_input_schemas_carry_the_agreed_bounds_and_descriptions(
    mcp: Client[Any],
) -> None:
    tools: dict[str, Tool] = {tool.name: tool for tool in await mcp.list_tools()}

    verify = tools["verify_citation"].input_schema
    assert verify["required"] == ["citations"]
    assert (
        verify["properties"]["citations"]["minItems"],
        verify["properties"]["citations"]["maxItems"],
    ) == (1, 10)
    item = verify["properties"]["citations"]["items"]
    assert item["required"] == ["law"]
    assert item["properties"]["law"]["description"] == (
        "Name, number or BOE identifier of the law, e.g. 'Ley 40/2015'."
    )
    assert item["properties"]["article"]["description"] == (
        "The article or provision, e.g. 'artículo 48' or 'disposición adicional tercera'. "
        "Omit when block_id is given."
    )
    assert item["properties"]["block_id"]["description"] == (
        "Exact block of the law, from the candidates of an ambiguous article; used instead of "
        "article."
    )
    assert item["properties"]["quote"]["description"] == (
        "Words attributed to it, to compare with the text. Use (...) for omissions."
    )
    assert (
        item["properties"]["as_of"]["description"] == "Date the citation refers to; default today."
    )

    chain = tools["explore_amendment_chain"].input_schema
    assert chain["required"] == ["law_id"]
    props = chain["properties"]
    assert (props["depth"]["minimum"], props["depth"]["maximum"], props["depth"]["default"]) == (
        1,
        3,
        2,
    )
    assert (
        props["max_laws"]["minimum"],
        props["max_laws"]["maximum"],
        props["max_laws"]["default"],
    ) == (1, 50, 25)
    assert props["direction"]["enum"] == ["amended_by", "amends"]
    assert props["direction"]["default"] == "amended_by"
    assert props["include"]["enum"] == ["amendments", "all"]
    assert props["details"]["enum"] == ["root", "all", "none"]
    assert props["details"]["default"] == "root"


# --- verify_citation ---------------------------------------------------------------------------


async def test_citations_are_checked_through_the_tool(mcp: Client[Any], ley_40: None) -> None:
    content = await call(
        mcp,
        "verify_citation",
        citations=[
            {
                "law": LEY_40,
                "article": "artículo 48",
                "quote": "Los convenios se perfeccionan por la prestación del consentimiento",
            },
            {
                "law": LEY_40,
                "article": "artículo 48",
                "quote": "las mareas del Cantábrico alcanzarán mínimos históricos",
            },
            {"law": LEY_40, "article": "artículo 999"},
        ],
    )

    result = VerifyCitationResult.model_validate(content)
    assert [c.status for c in result.results] == ["matches", "quote_not_found", "article_not_found"]
    assert (result.checked, result.not_checked) == (3, 0)
    first = content["results"][0]
    assert first["law_id"] == LEY_40 and first["article_label"] == "Artículo 48"
    # Fields that do not apply are left out of the output rather than sent as nulls.
    assert "other_version" not in first and "candidates" not in first and "note" not in first
    assert content["results"][2]["status"] == "article_not_found"
    assert content["provenance"]["attribution"] == (
        "Basado en datos de la Agencia Estatal Boletín Oficial del Estado"
    )
    assert "Consolidated texts" in content["provenance"]["notice"]


async def test_more_than_ten_citations_are_refused_by_the_schema(mcp: Client[Any]) -> None:
    citation = {"law": LEY_40, "article": "artículo 48"}

    with pytest.raises(ToolError, match="citations"):
        await call(mcp, "verify_citation", citations=[citation] * 11)
    with pytest.raises(ToolError, match="citations"):
        await call(mcp, "verify_citation", citations=[])


async def test_a_citation_without_words_to_compare_is_an_invalid_argument(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match=r"invalid_argument.*quote must contain words"):
        await call(
            mcp,
            "verify_citation",
            citations=[{"law": LEY_40, "article": "artículo 48", "quote": "«…»"}],
        )


# --- explore_amendment_chain -------------------------------------------------------------------


async def test_a_chain_is_explored_through_the_tool(
    mcp: Client[Any], chain_routes: dict[str, respx.Route]
) -> None:
    content = await call(
        mcp, "explore_amendment_chain", law_id=RDL_8_2020, direction="amends", depth=2
    )

    result = AmendmentChainResult.model_validate(content)
    assert (result.law_id, result.direction, result.depth) == (RDL_8_2020, "amends", 2)
    assert result.complete is True and result.pending == 0 and result.failed == 0
    # The starting law and the four amended laws that are in the collection were read, and
    # nothing else: the other references point outside the collection.
    assert result.examined == 5
    assert {law_id: route.call_count for law_id, route in chain_routes.items()} == {
        RDL_8_2020: 1,
        **{law_id: 1 for law_id in AMENDED},
    }
    assert result.levels[0] == 1 and result.levels[1] == len(result.nodes) - 1 - result.levels[2]
    by_id = {node.law_id: node for node in result.nodes}
    assert by_id["BOE-A-2002-8628"].title == "Ley de prueba 1"
    assert by_id["BOE-A-2002-8628"].in_collection and by_id["BOE-A-2002-8628"].expanded
    outside = by_id["BOE-A-2020-3580"]
    assert (outside.in_collection, outside.expanded, outside.reason) == (
        False,
        False,
        "unavailable",
    )
    assert all(
        edge.amending == RDL_8_2020
        for edge in result.edges
        if edge.amended in by_id and by_id[edge.amended].depth == 1
    )
    assert {e.kind for e in result.edges} <= {"modifies", "repeals", "adds", "suspends"}
    # Absent optional fields are not sent as nulls.
    root = content["nodes"][0]
    assert "title" not in root and "reason" not in root
    assert content["provenance"]["attribution"].startswith("Basado en datos")


async def test_a_chain_that_is_cut_by_max_laws_says_so_and_a_repeat_completes_it(
    mcp: Client[Any], chain_routes: dict[str, respx.Route]
) -> None:
    first = await call(
        mcp, "explore_amendment_chain", law_id=RDL_8_2020, direction="amends", max_laws=2
    )
    second = await call(
        mcp, "explore_amendment_chain", law_id=RDL_8_2020, direction="amends", max_laws=2
    )
    third = await call(
        mcp, "explore_amendment_chain", law_id=RDL_8_2020, direction="amends", max_laws=2
    )

    assert [first["complete"], second["complete"], third["complete"]] == [False, False, True]
    assert first["pending"] == 3 and first["examined"] == 2
    assert first["note"] == (
        "Examined 2 laws, 3 still to read. Call again with the same arguments to continue."
    )
    assert third["examined"] == 5
    # Five analyses in all, however the work was split.
    assert sum(route.call_count for route in chain_routes.values()) == 5


async def test_an_unknown_starting_law_is_a_not_found_tool_error(
    mcp: Client[Any], chain_routes: dict[str, respx.Route]
) -> None:
    with pytest.raises(ToolError, match="not_found"):
        await call(mcp, "explore_amendment_chain", law_id="BOE-A-1999-9")


async def test_bad_chain_arguments_are_refused(mcp: Client[Any]) -> None:
    with pytest.raises(ToolError, match="depth"):
        await call(mcp, "explore_amendment_chain", law_id=RDL_8_2020, depth=4)
    with pytest.raises(ToolError, match=r"invalid_argument.*identifier"):
        await call(mcp, "explore_amendment_chain", law_id="Ley 40/2015")
