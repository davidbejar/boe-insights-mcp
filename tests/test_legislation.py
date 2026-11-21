"""Tests for the use cases of ``boe_insights_mcp.legislation`` against a mocked BOE API.

``derive_status`` has its own contract tests in ``test_legislation_status.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.legislation import (
    get_law_overview,
    resolve_reference,
    search_legislation,
)
from boe_insights_mcp.ratelimit import RateLimiter

SEARCH_URL = f"{BASE_URL}legislacion-consolidada"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

LEY_40 = "BOE-A-2015-10566"
LEY_30_1992 = "BOE-A-1992-26318"

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http)


def raw_hit(
    identifier: str,
    *,
    rank: str | None = "1300",
    number: str | None = "1/2020",
    updated: str | None = None,
) -> dict[str, Any]:
    """A search hit as the API serialises it, with only the fields a test cares about."""
    hit: dict[str, Any] = {"identificador": identifier, "titulo": f"Title of {identifier}"}
    if rank is not None:
        hit["rango"] = {"codigo": rank, "texto": f"rank {rank}"}
    if number is not None:
        hit["numero_oficial"] = number
    if updated is not None:
        hit["fecha_actualizacion"] = updated
    return hit


def search_body(*hits: dict[str, Any]) -> bytes:
    """The API's envelope for a search result (an empty result has ``data`` as ``""``)."""
    data: Any = list(hits) if hits else ""
    return json.dumps({"status": {"code": "200", "text": "ok"}, "data": data}).encode()


def mock_searches(
    router: respx.MockRouter, json_response: JsonResponse, *bodies: bytes
) -> respx.Route:
    """Answer successive search requests with ``bodies``, in order."""
    return router.get(SEARCH_URL).mock(side_effect=[json_response(body) for body in bodies])


def sent_query(route: respx.Route, call: int = 0) -> str:
    """The Lucene expression of the ``call``-th search request."""
    document = json.loads(route.calls[call].request.url.params["query"])
    return document["query"]["query_string"]["query"]


def sent_params(route: respx.Route, call: int = 0) -> httpx.QueryParams:
    return route.calls[call].request.url.params


# --- search_legislation: mapping and provenance --------------------------------------------


async def test_search_maps_hits_to_compact_records(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_page_limit10.json"))

    result = await search_legislation(client, now=NOW)

    first = result.items[0]
    assert (first.identifier, first.rank, first.official_number) == (
        "BOE-A-2022-10675",
        "Real Decreto",
        "502/2022",
    )
    assert first.scope == "Estatal"
    assert first.published_on is not None
    assert first.validity_exhausted is False
    assert first.url is not None
    assert result.returned == len(result.items) == 10


async def test_search_provenance_has_attribution_and_newest_update(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_page_limit10.json"))

    provenance = (await search_legislation(client, now=NOW)).provenance

    assert provenance.retrieved_at == NOW
    assert provenance.data_updated_at == datetime(2026, 11, 20, 13, 8, 24, tzinfo=UTC)
    assert (
        provenance.attribution == "Basado en datos de la Agencia Estatal Boletín Oficial del Estado"
    )
    assert provenance.source_url == "https://www.boe.es"
    assert "no official legal value" in provenance.notice


async def test_search_with_no_hits_has_no_update_stamp(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_empty.json"))

    result = await search_legislation(client, title="energía", now=NOW)

    assert result.items == []
    assert result.returned == 0
    assert result.has_more is False
    assert result.next_offset is None
    assert result.provenance.data_updated_at is None


async def test_search_defaults_to_newest_published_first(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(router, json_response, search_body())

    await search_legislation(client)

    document = json.loads(sent_params(route)["query"])
    assert document["sort"] == [{"fecha_publicacion": "desc"}]
    assert "query" not in document


# --- search_legislation: paging -------------------------------------------------------------


async def test_search_requests_one_extra_record_to_detect_more(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(router, json_response, search_body())

    await search_legislation(client, limit=7, offset=14)

    assert sent_params(route)["limit"] == "8"
    assert sent_params(route)["offset"] == "14"


async def test_search_reports_next_page_when_upstream_has_an_extra_record(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_page_limit10.json"))

    result = await search_legislation(client, limit=9, offset=30)

    assert result.returned == 9
    assert result.has_more is True
    assert result.next_offset == 39


async def test_search_with_exactly_limit_hits_has_no_more(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_page_limit10.json"))

    result = await search_legislation(client, limit=10)

    assert result.returned == 10
    assert result.has_more is False
    assert result.next_offset is None


@pytest.mark.parametrize("limit", [0, 51, -1])
async def test_search_rejects_out_of_range_limit_without_a_request(
    client: BoeClient, router: respx.MockRouter, limit: int
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await search_legislation(client, limit=limit)

    assert router.calls.call_count == 0


async def test_search_rejects_inverted_date_range(client: BoeClient) -> None:
    with pytest.raises(ValueError, match="published_from"):
        await search_legislation(
            client, published_from=date(2021, 1, 1), published_to=date(2020, 1, 1)
        )


# --- search_legislation: rank filter --------------------------------------------------------


@pytest.mark.parametrize(
    ("rank", "code", "name"),
    [
        ("constitucion", "1070", "Constitución"),
        ("ley_organica", "1290", "Ley Orgánica"),
        ("ley", "1300", "Ley"),
        ("real_decreto_ley", "1320", "Real Decreto-ley"),
        ("real_decreto_legislativo", "1310", "Real Decreto Legislativo"),
        ("real_decreto", "1340", "Real Decreto"),
        ("orden", "1350", "Orden"),
        ("resolucion", "1370", "Resolución"),
        ("circular", "1390", "Circular"),
        ("instruccion", "1410", "Instrucción"),
    ],
)
async def test_rank_names_map_to_codes_of_the_boe_rank_table(
    client: BoeClient,
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_json: Callable[[str], Any],
    rank: Any,
    code: str,
    name: str,
) -> None:
    route = mock_searches(router, json_response, search_body())

    await search_legislation(client, rank=rank)

    assert load_json("aux_rangos.json")["data"][code] == name
    assert f"rango@codigo:{code}" in sent_query(route)


@pytest.mark.parametrize(("scope", "code"), [("state", "1"), ("regional", "2")])
async def test_scope_filter_maps_to_ambito_code(
    client: BoeClient,
    router: respx.MockRouter,
    json_response: JsonResponse,
    scope: Any,
    code: str,
) -> None:
    route = mock_searches(router, json_response, search_body())

    await search_legislation(client, scope=scope)

    assert f"ambito@codigo:{code}" in sent_query(route)


async def test_scope_any_adds_no_scope_filter(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(router, json_response, search_body())

    await search_legislation(client, title="agua", scope="any")

    assert "ambito" not in sent_query(route)


async def test_rank_filter_drops_hits_of_another_rank(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(
            raw_hit("BOE-A-2000-1", rank="1300"),
            raw_hit("BOE-A-2000-2", rank="1450"),
            raw_hit("BOE-A-2000-3", rank="1300"),
        ),
    )

    result = await search_legislation(client, rank="ley", limit=10)

    assert [item.identifier for item in result.items] == ["BOE-A-2000-1", "BOE-A-2000-3"]
    assert result.returned == 2


async def test_rank_filter_drops_hits_without_a_rank(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(raw_hit("BOE-A-2000-1", rank=None), raw_hit("BOE-A-2000-2", rank="1300")),
    )

    result = await search_legislation(client, rank="ley", limit=10)

    assert [item.identifier for item in result.items] == ["BOE-A-2000-2"]


async def test_without_rank_filter_every_rank_is_kept(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(raw_hit("BOE-A-2000-1", rank="1300"), raw_hit("BOE-A-2000-2", rank="1450")),
    )

    result = await search_legislation(client, limit=10)

    assert result.returned == 2


async def test_rank_filter_with_extra_matching_hit_points_next_offset_at_it(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(raw_hit("BOE-A-2000-1"), raw_hit("BOE-A-2000-2")),
    )

    result = await search_legislation(client, rank="ley", limit=1, offset=40)

    assert [item.identifier for item in result.items] == ["BOE-A-2000-1"]
    assert result.has_more is True
    assert result.next_offset == 41


async def test_rank_filter_counts_dropped_hits_before_the_extra_one(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(
            raw_hit("BOE-A-2000-1"),
            raw_hit("BOE-A-2000-2", rank="1450"),
            raw_hit("BOE-A-2000-3"),
        ),
    )

    result = await search_legislation(client, rank="ley", limit=1, offset=10)

    assert [item.identifier for item in result.items] == ["BOE-A-2000-1"]
    assert result.has_more is True
    assert result.next_offset == 12  # the third upstream record is where the next page starts


async def test_rank_filter_on_a_full_upstream_page_may_have_more(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    """The dropped hit hides how many records remain, so the next page is offered."""
    mock_searches(
        router,
        json_response,
        search_body(raw_hit("BOE-A-2000-1"), raw_hit("BOE-A-2000-2", rank="1450")),
    )

    result = await search_legislation(client, rank="ley", limit=1, offset=10)

    assert result.returned == 1
    assert result.has_more is True
    assert result.next_offset == 12


async def test_rank_filter_on_a_short_upstream_page_has_no_more(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(raw_hit("BOE-A-2000-1"), raw_hit("BOE-A-2000-2", rank="1450")),
    )

    result = await search_legislation(client, rank="ley", limit=5, offset=10)

    assert result.returned == 1
    assert result.has_more is False
    assert result.next_offset is None


async def test_rank_filter_dropping_everything_yields_an_empty_page(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(router, json_response, search_body(raw_hit("BOE-A-2000-1", rank="1450")))

    result = await search_legislation(client, rank="ley", limit=5)

    assert result.items == []
    assert result.has_more is False
    assert result.provenance.data_updated_at is None


async def test_data_updated_at_ignores_dropped_hits(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(
            raw_hit("BOE-A-2000-1", updated="20200101T000000Z"),
            raw_hit("BOE-A-2000-2", rank="1450", updated="20250101T000000Z"),
        ),
    )

    result = await search_legislation(client, rank="ley", limit=5)

    assert result.provenance.data_updated_at == datetime(2020, 1, 1, tzinfo=UTC)


# --- resolve_reference: identifiers ---------------------------------------------------------


async def test_identifier_that_exists_is_resolved(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )

    result = await resolve_reference(client, LEY_40, now=NOW)

    assert result.status == "resolved"
    assert [match.identifier for match in result.matches] == [LEY_40]
    assert result.matches[0].rank == "Ley"
    assert result.hint is None
    assert result.interpreted_as == f"BOE identifier {LEY_40}"
    assert result.provenance.retrieved_at == NOW
    assert result.provenance.data_updated_at == datetime(2026, 11, 20, 8, 50, tzinfo=UTC)


async def test_identifier_that_does_not_exist_is_not_found(
    client: BoeClient, router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(f"{SEARCH_URL}/id/BOE-A-1999-1/metadatos").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    result = await resolve_reference(client, "BOE-A-1999-1")

    assert result.status == "not_found"
    assert result.matches == []
    assert result.hint is not None
    assert result.provenance.data_updated_at is None


async def test_lowercase_boe_identifier_is_requested_in_upper_case(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    route = router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )

    result = await resolve_reference(client, f"  {LEY_40.lower()} ")

    assert route.called
    assert result.status == "resolved"


# --- resolve_reference: numbered references -------------------------------------------------


async def test_rank_and_number_with_one_state_match_is_resolved(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    route = mock_searches(
        router, json_response, load_fixture("search_numero_oficial_rango_single.json")
    )

    result = await resolve_reference(client, "LO 3/2018", now=NOW)

    assert result.status == "resolved"
    assert [match.identifier for match in result.matches] == ["BOE-A-2018-16673"]
    assert result.interpreted_as == "Ley Orgánica 3/2018 (state)"
    assert route.call_count == 1
    query = sent_query(route)
    assert 'numero_oficial:"3/2018"' in query
    assert "rango@codigo:1290" in query
    assert "ambito@codigo:1" in query


async def test_hits_of_another_rank_are_ignored_when_matching_a_rank(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_numero_oficial_ambiguous.json"))

    result = await resolve_reference(client, "Ley Orgánica 3/2018")

    assert result.status == "resolved"
    assert [match.identifier for match in result.matches] == ["BOE-A-2018-16673"]


async def test_hits_with_another_number_are_ignored(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(
            raw_hit("BOE-A-2000-1", number="39/2015"),
            raw_hit("BOE-A-2000-2", number="40/2015"),
            raw_hit("BOE-A-2000-3", number=None),
        ),
    )

    result = await resolve_reference(client, "Ley 40/2015")

    assert [match.identifier for match in result.matches] == ["BOE-A-2000-2"]
    assert result.status == "resolved"


async def test_several_state_matches_are_ambiguous_and_capped_at_ten(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_numero_oficial_ambiguous.json"))

    result = await resolve_reference(client, "Ley 3/2018")

    assert result.status == "ambiguous"
    assert len(result.matches) == 10
    assert result.hint is not None


async def test_no_state_match_retries_without_scope_and_resolves_a_regional_law(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(
        router,
        json_response,
        search_body(),
        search_body(raw_hit("DOGC-f-2010-90008", number="3/2010")),
    )

    result = await resolve_reference(client, "Ley 3/2010")

    assert result.status == "resolved"
    assert result.interpreted_as == "Ley 3/2010 (any scope)"
    assert "ambito" in sent_query(route, 0)
    assert "ambito" not in sent_query(route, 1)


async def test_scope_retry_with_several_matches_is_ambiguous(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(
        router,
        json_response,
        search_body(),
        search_body(
            raw_hit("BOE-A-2010-1", number="3/2010"), raw_hit("DOGC-f-2010-2", number="3/2010")
        ),
    )

    result = await resolve_reference(client, "Ley 3/2010")

    assert result.status == "ambiguous"
    assert len(result.matches) == 2


async def test_number_found_nowhere_is_not_found_after_two_searches(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(router, json_response, search_body(), search_body())

    result = await resolve_reference(client, "Ley 99/2015")

    assert result.status == "not_found"
    assert result.matches == []
    assert result.hint is not None
    assert route.call_count == 2


async def test_bare_number_with_one_match_is_resolved_without_a_rank_filter(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(
        router, json_response, search_body(raw_hit("BOE-A-2015-10566", number="40/2015"))
    )

    result = await resolve_reference(client, "40/2015")

    assert result.status == "resolved"
    assert "rango" not in sent_query(route)
    assert result.interpreted_as is not None
    assert "rank not given" in result.interpreted_as


async def test_bare_number_with_several_matches_asks_for_the_rank(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_numero_oficial_ambiguous.json"))

    result = await resolve_reference(client, "3/2018")

    assert result.status == "ambiguous"
    assert result.hint is not None
    assert "rank" in result.hint


async def test_reference_is_echoed_as_given(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(router, json_response, search_body(raw_hit("BOE-A-2015-10566", number="40/2015")))

    result = await resolve_reference(client, "  ley 40/2015 ")

    assert result.reference == "  ley 40/2015 "


# --- resolve_reference: free text and invalid input -----------------------------------------


async def test_unparseable_text_falls_back_to_a_title_search(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    route = mock_searches(router, json_response, load_fixture("search_page_limit10.json"))

    result = await resolve_reference(client, "régimen jurídico")

    assert result.status == "ambiguous"
    assert len(result.matches) == 10
    assert result.interpreted_as is None
    assert result.hint is not None
    assert "title" in result.hint
    assert sent_query(route).startswith("titulo:")
    assert sent_params(route)["limit"] == "11"


async def test_title_search_without_hits_is_not_found_and_lists_formats(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    mock_searches(router, json_response, search_body())

    result = await resolve_reference(client, "nothing like this")

    assert result.status == "not_found"
    assert result.matches == []
    assert result.hint is not None
    assert "Ley 40/2015" in result.hint


@pytest.mark.parametrize("reference", ["", "   ", "\n"])
async def test_blank_reference_is_rejected_without_a_request(
    client: BoeClient, router: respx.MockRouter, reference: str
) -> None:
    with pytest.raises(ValueError, match="empty"):
        await resolve_reference(client, reference)

    assert router.calls.call_count == 0


# --- get_law_overview -----------------------------------------------------------------------


def mock_law(
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_fixture: Loader,
    law_id: str,
    metadata: str,
    analysis: str = "analisis_ley40_2015.json",
) -> None:
    router.get(f"{SEARCH_URL}/id/{law_id}/metadatos").mock(
        return_value=json_response(load_fixture(metadata))
    )
    router.get(f"{SEARCH_URL}/id/{law_id}/analisis").mock(
        return_value=json_response(load_fixture(analysis))
    )


@pytest.fixture
def ley_40(router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader) -> None:
    mock_law(router, json_response, load_fixture, LEY_40, "metadatos_in_force_ley40_2015.json")


@pytest.mark.usefixtures("ley_40")
async def test_overview_describes_the_law(client: BoeClient) -> None:
    overview = await get_law_overview(client, LEY_40, today=date(2026, 11, 21), now=NOW)

    assert overview.identifier == LEY_40
    assert overview.title.startswith("Ley 40/2015, de 1 de octubre")
    assert (overview.rank, overview.official_number) == ("Ley", "40/2015")
    assert (overview.department, overview.scope) == ("Jefatura del Estado", "Estatal")
    assert overview.enacted_on == date(2015, 10, 1)
    assert overview.published_on == date(2015, 10, 2)
    assert overview.effective_from == date(2016, 10, 2)
    assert overview.consolidation_state == "Finalizado"
    assert overview.url == f"https://www.boe.es/buscar/act.php?id={LEY_40}"
    assert overview.eli_url == "https://www.boe.es/eli/es/l/2015/10/01/40"
    assert overview.repealed_on is None


@pytest.mark.usefixtures("ley_40")
async def test_overview_status_comes_from_the_metadata(client: BoeClient) -> None:
    overview = await get_law_overview(client, LEY_40, today=date(2026, 11, 21), now=NOW)

    assert overview.status == "in_force"
    assert "amended" in overview.status_note


@pytest.mark.usefixtures("ley_40")
async def test_overview_uses_the_given_date_to_judge_effectiveness(client: BoeClient) -> None:
    overview = await get_law_overview(client, LEY_40, today=date(2016, 1, 1), now=NOW)

    assert overview.status == "not_yet_in_force"


@pytest.mark.usefixtures("ley_40")
async def test_overview_without_a_date_uses_the_current_moment(client: BoeClient) -> None:
    overview = await get_law_overview(client, LEY_40, now=datetime(2016, 1, 1, tzinfo=UTC))

    assert overview.status == "not_yet_in_force"


@pytest.mark.usefixtures("ley_40")
async def test_overview_counts_amendments_of_ley_40_2015(client: BoeClient) -> None:
    amendments = (await get_law_overview(client, LEY_40, now=NOW)).amendments

    assert amendments.affected_by_count == 24
    assert amendments.affects_count == 19
    assert len(amendments.affected_by_sample) == 10
    assert len(amendments.affects_sample) == 10


@pytest.mark.usefixtures("ley_40")
async def test_overview_groups_relations_by_count_then_text(client: BoeClient) -> None:
    amendments = (await get_law_overview(client, LEY_40, now=NOW)).amendments

    assert [(r.relation, r.count) for r in amendments.affected_by_relations] == [
        ("SE MODIFICA", 10),
        ("SE DICTA DE CONFORMIDAD", 5),
        ("SE CORRIGEN errores", 2),
        ("SE DECLARA", 2),
        ("SE DICTA EN RELACIÓN", 2),
        ("CORRECCIÓN de errores", 1),
        ("SE DEROGA", 1),
        ("SE DESARROLLA", 1),
    ]
    assert [(r.relation, r.count) for r in amendments.affects_relations] == [
        ("MODIFICA", 10),
        ("DEROGA", 8),
        ("CITA", 1),
    ]


@pytest.mark.usefixtures("ley_40")
async def test_overview_samples_keep_the_api_order(client: BoeClient) -> None:
    amendments = (await get_law_overview(client, LEY_40, now=NOW)).amendments

    first = amendments.affected_by_sample[0]
    assert (first.law_id, first.relation) == ("BOE-A-2020-4859", "SE DEROGA")
    assert first.text.startswith("la disposición final 5")
    assert amendments.affects_sample[0].law_id == "BOE-A-2014-9467"


@pytest.mark.usefixtures("ley_40")
async def test_overview_provenance_uses_the_metadata_update_stamp(client: BoeClient) -> None:
    provenance = (await get_law_overview(client, LEY_40, now=NOW)).provenance

    assert provenance.retrieved_at == NOW
    assert provenance.data_updated_at == datetime(2026, 11, 20, 8, 50, tzinfo=UTC)


async def test_overview_of_a_repealed_law_has_the_repeal_date(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_law(router, json_response, load_fixture, LEY_30_1992, "metadatos_repealed_ley30_1992.json")

    overview = await get_law_overview(client, LEY_30_1992, now=NOW)

    assert overview.status == "repealed"
    assert overview.repealed_on == date(2021, 4, 2)
    assert "2021-04-02" in overview.status_note


async def test_overview_of_an_unknown_law_raises_not_found(
    client: BoeClient, router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(url__regex=rf"{SEARCH_URL}/id/BOE-A-1999-1/.*").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    with pytest.raises(BoeNotFoundError):
        await get_law_overview(client, "BOE-A-1999-1")


async def test_overview_rejects_a_malformed_identifier(
    client: BoeClient, router: respx.MockRouter
) -> None:
    with pytest.raises(ValueError, match="identifier"):
        await get_law_overview(client, "not-an-id")

    assert router.calls.call_count == 0
