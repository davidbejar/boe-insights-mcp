"""Tests for the use cases of ``boe_insights_mcp.legislation`` against a mocked BOE API.

``derive_status`` has its own contract tests in ``test_legislation_status.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.errors import BoeNotFoundError, BoeUnavailableError
from boe_insights_mcp.legislation import (
    find_codes,
    get_amendment_history,
    get_law_overview,
    relation_group,
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
    scope: str | None = None,
) -> dict[str, Any]:
    """A search hit as the API serialises it, with only the fields a test cares about."""
    hit: dict[str, Any] = {"identificador": identifier, "titulo": f"Title of {identifier}"}
    if rank is not None:
        hit["rango"] = {"codigo": rank, "texto": f"rank {rank}"}
    if number is not None:
        hit["numero_oficial"] = number
    if updated is not None:
        hit["fecha_actualizacion"] = updated
    if scope is not None:
        hit["ambito"] = {"codigo": scope, "texto": "Estatal" if scope == "1" else "Autonómico"}
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

    assert provenance.generated_at == NOW
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
    assert result.provenance.generated_at == NOW
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


# (reference, hits of the first search, status, searches made, rank filter, state scope searched)
SCOPE_STRATEGY = [
    pytest.param(
        "Ley 40/2015",
        [raw_hit("BOE-A-2015-10566", number="40/2015", scope="1")],
        "resolved",
        1,
        "1300",
        True,
        id="plain-state-reference-searches-state-first",
    ),
    pytest.param(
        "Real Decreto Legislativo 1/2010",
        [raw_hit("BOE-A-2010-1", rank="1310", number="1/2010", scope="1")],
        "resolved",
        1,
        "1310",
        True,
        id="state-decreto-legislativo-keeps-its-state-rank",
    ),
    pytest.param(
        "Decreto Legislativo 1/2010",
        [raw_hit("DOGC-f-2010-1", rank="1470", number="1/2010", scope="2")],
        "resolved",
        1,
        "1470",
        False,
        id="regional-rank-is-searched-in-any-scope",
    ),
    pytest.param(
        "Decreto Legislativo 1/2010",
        [
            raw_hit("BOE-A-2010-1", rank="1310", number="1/2010", scope="1"),
            raw_hit("DOGC-f-2010-1", rank="1470", number="1/2010", scope="2"),
            raw_hit("BOJA-b-2010-1", rank="1470", number="1/2010", scope="2"),
        ],
        "ambiguous",
        1,
        "1470",
        False,
        id="regional-rank-with-two-regional-hits-is-ambiguous",
    ),
    pytest.param(
        "Decreto-ley 1/2010",
        [raw_hit("DOGV-r-2010-1", rank="1500", number="1/2010", scope="2")],
        "resolved",
        1,
        "1500",
        False,
        id="decreto-ley-is-regional",
    ),
    pytest.param(
        "Ley 2/2020 de Andalucía",
        [
            raw_hit("BOE-A-2020-1", number="2/2020", scope="1"),
            raw_hit("BOJA-b-2020-1", number="2/2020", scope="2"),
        ],
        "ambiguous",
        1,
        "1300",
        False,
        id="region-mention-with-state-and-regional-hits-is-ambiguous",
    ),
    pytest.param(
        "Ley 2/2020 de Andalucía",
        [raw_hit("BOE-A-2020-1", number="2/2020", scope="1")],
        "ambiguous",
        1,
        "1300",
        False,
        id="region-mention-never-resolves-to-a-lone-state-law",
    ),
    pytest.param(
        "Ley 2/2020 de Andalucía",
        [raw_hit("BOJA-b-2020-1", number="2/2020", scope="2")],
        "resolved",
        1,
        "1300",
        False,
        id="region-mention-with-one-regional-hit-resolves",
    ),
    pytest.param(
        "Decreto Foral 3/2010",
        [raw_hit("BON-b-2010-1", number="3/2010", scope="2")],
        "ambiguous",
        1,
        None,
        False,
        id="unrecognised-rank-word-never-resolves",
    ),
]


@pytest.mark.parametrize(
    ("reference", "hits", "status", "searches", "rank_filter", "state_first"), SCOPE_STRATEGY
)
async def test_scope_strategy_never_resolves_a_state_law_for_a_regional_signal(
    client: BoeClient,
    router: respx.MockRouter,
    json_response: JsonResponse,
    reference: str,
    hits: list[dict[str, Any]],
    status: str,
    searches: int,
    rank_filter: str | None,
    state_first: bool,
) -> None:
    route = mock_searches(router, json_response, search_body(*hits))

    result = await resolve_reference(client, reference)

    assert result.status == status
    assert route.call_count == searches
    query = sent_query(route)
    assert ("ambito@codigo:1" in query) is state_first
    assert (f"rango@codigo:{rank_filter}" in query) if rank_filter else ("rango" not in query)
    if status == "ambiguous":
        assert result.hint is not None
        assert "regional" in result.hint
        assert "share" in result.hint
    if status == "ambiguous" and len(hits) > 1:
        assert len(result.matches) >= 2


async def test_ambiguous_hint_with_a_rank_does_not_ask_for_the_rank(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_searches(router, json_response, load_fixture("search_numero_oficial_ambiguous.json"))

    result = await resolve_reference(client, "Ley 3/2018")

    assert result.status == "ambiguous"
    assert result.hint is not None
    assert "Repeat the reference with its rank" not in result.hint
    assert "regional" in result.hint
    assert "title" in result.hint
    assert "identifier" in result.hint


async def test_title_fallback_lists_the_oldest_laws_first(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    route = mock_searches(router, json_response, load_fixture("search_page_limit10.json"))

    await resolve_reference(client, "régimen jurídico")

    document = json.loads(sent_params(route)["query"])
    assert document["sort"] == [{"fecha_publicacion": "asc"}]


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


def mock_note(
    router: respx.MockRouter, load_fixture: Loader, law_id: str, fixture: str | None = None
) -> respx.Route:
    """Answer the initial-note request: with a recorded note, or HTTP 404 (the law has none)."""
    status, name = (200, fixture) if fixture else (404, "error_404_bloque_no_in_force_law.xml")
    response = httpx.Response(
        status, content=load_fixture(name), headers={"Content-Type": "application/xml"}
    )
    return router.get(f"{SEARCH_URL}/id/{law_id}/texto/bloque/no").mock(return_value=response)


def mock_law(
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_fixture: Loader,
    law_id: str,
    metadata: str,
    analysis: str = "analisis_ley40_2015.json",
    note: str | None = None,
) -> None:
    """Mock metadata, analysis and initial note (HTTP 404 unless ``note`` names a fixture)."""
    router.get(f"{SEARCH_URL}/id/{law_id}/metadatos").mock(
        return_value=json_response(load_fixture(metadata))
    )
    router.get(f"{SEARCH_URL}/id/{law_id}/analisis").mock(
        return_value=json_response(load_fixture(analysis))
    )
    mock_note(router, load_fixture, law_id, note)


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
async def test_overview_status_combines_the_metadata_flags_and_the_repeal_references(
    client: BoeClient,
) -> None:
    # Not flagged repealed, but one later norm repeals part of it (relation "SE DEROGA").
    overview = await get_law_overview(client, LEY_40, today=date(2026, 11, 21), now=NOW)

    assert overview.status == "partially_repealed"
    assert "1 reference" in overview.status_note


def analysis_body(*relations: str) -> bytes:
    """An analysis whose later norms have the given relation texts."""
    later = [
        {
            "id_norma": f"BOE-A-2020-{number}",
            "relacion": {"codigo": "270", "texto": relation},
            "texto": "un precepto",
        }
        for number, relation in enumerate(relations, start=1)
    ]
    references: dict[str, Any] = {"posteriores": [{"posterior": later}]} if later else {}
    document = {"status": {"code": "200", "text": "ok"}, "data": [{"referencias": references}]}
    return json.dumps(document).encode()


def mock_ley_40_with(
    router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader, *relations: str
) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(
        return_value=json_response(analysis_body(*relations))
    )
    mock_note(router, load_fixture, LEY_40)


async def test_overview_without_repeal_references_is_in_force(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_ley_40_with(router, json_response, load_fixture, "SE MODIFICA", "SE DICTA EN RELACIÓN")

    overview = await get_law_overview(client, LEY_40, today=date(2026, 11, 21), now=NOW)

    assert overview.status == "in_force"
    assert "amended" in overview.status_note


async def test_overview_of_a_superseded_law_the_boe_does_not_flag_is_partially_repealed(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    law_id = "BOE-A-1999-23750"
    mock_law(
        router, json_response, load_fixture, law_id, "metadatos_superseded_partial_lopd1999.json"
    )
    router.get(f"{SEARCH_URL}/id/{law_id}/analisis").mock(
        return_value=json_response(
            analysis_body(
                "SE DEROGA",
                "SE DEROGA parcialmente",
                "SE DEROGA tácitamente ",
                "Se DEROGA de forma reiterada",
                "SE MODIFICA",
            )
        )
    )

    overview = await get_law_overview(client, law_id, now=NOW)

    assert overview.status == "partially_repealed"
    assert "4 references" in overview.status_note
    assert "only marks whole" in overview.status_note


async def test_overview_flagged_repealed_stays_repealed_whatever_the_references(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_law(router, json_response, load_fixture, LEY_30_1992, "metadatos_repealed_ley30_1992.json")
    router.get(f"{SEARCH_URL}/id/{LEY_30_1992}/analisis").mock(
        return_value=json_response(analysis_body("SE DEROGA"))
    )

    overview = await get_law_overview(client, LEY_30_1992, now=NOW)

    assert overview.status == "repealed"


@pytest.mark.parametrize(
    ("moment", "status"),
    [
        # Ley 40/2015 takes effect on 2016-10-02. In Madrid (CEST, UTC+2) it is already that day
        # at 22:30 UTC on the 1st, though still the 1st in UTC.
        (datetime(2016, 10, 1, 22, 30, tzinfo=UTC), "in_force"),
        (datetime(2016, 10, 1, 21, 30, tzinfo=UTC), "not_yet_in_force"),
        # Winter time (CET, UTC+1): the day changes at 23:00 UTC.
        (datetime(2017, 1, 1, 23, 30, tzinfo=UTC), "in_force"),
    ],
)
async def test_overview_judges_today_in_madrid_time(
    client: BoeClient,
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_fixture: Loader,
    moment: datetime,
    status: str,
) -> None:
    mock_ley_40_with(router, json_response, load_fixture, "SE MODIFICA")

    overview = await get_law_overview(client, LEY_40, now=moment)

    assert overview.status == status


async def test_overview_cancels_the_analysis_request_when_the_metadata_fails(
    client: BoeClient, router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    analysis_cancelled = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            analysis_cancelled.set()
            raise
        return httpx.Response(500)

    router.get(f"{SEARCH_URL}/id/BOE-A-1999-1/metadatos").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )
    router.get(f"{SEARCH_URL}/id/BOE-A-1999-1/analisis").mock(side_effect=hang)
    mock_note(router, load_fixture, "BOE-A-1999-1")

    with pytest.raises(BoeNotFoundError):  # the original error, not an ExceptionGroup
        await get_law_overview(client, "BOE-A-1999-1")

    assert analysis_cancelled.is_set()


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

    assert provenance.generated_at == NOW
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


@pytest.mark.usefixtures("ley_40")
async def test_overview_accepts_a_lowercase_identifier_like_resolve_reference(
    client: BoeClient,
) -> None:
    overview = await get_law_overview(client, f"  {LEY_40.lower()} ", now=NOW)

    assert overview.identifier == LEY_40


# --- get_law_overview: the initial note ------------------------------------------------------

LOPD = "BOE-A-1999-23750"


async def test_overview_of_a_law_repealed_only_by_its_note_is_repealed(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    # LO 15/1999: metadata flag N, yet the consolidated text opens with "Norma derogada...".
    mock_law(
        router,
        json_response,
        load_fixture,
        LOPD,
        "metadatos_superseded_partial_lopd1999.json",
        note="texto_bloque_no_lopd1999_repealed.xml",
    )

    overview = await get_law_overview(client, LOPD, now=NOW)

    assert overview.status == "repealed"
    assert overview.repealed_on == date(2018, 12, 7)
    assert "opens with a repeal note" in overview.status_note
    assert overview.initial_note is not None
    assert overview.initial_note.startswith("Norma derogada, con efectos de 7 de diciembre de 2018")


async def test_overview_carries_a_note_that_is_not_a_repeal_without_changing_the_status(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_law(
        router,
        json_response,
        load_fixture,
        LEY_40,
        "metadatos_in_force_ley40_2015.json",
        note="texto_bloque_no_rd424_2005_rename_not_repeal.xml",
    )

    overview = await get_law_overview(client, LEY_40, today=date(2026, 11, 21), now=NOW)

    assert overview.status == "partially_repealed"  # from the analysis, as without the note
    assert overview.initial_note is not None
    assert "pasa a denominarse" in overview.initial_note


async def test_overview_initial_note_is_capped_at_600_characters(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_law(
        router,
        json_response,
        load_fixture,
        LEY_30_1992,
        "metadatos_repealed_ley30_1992.json",
        note="texto_bloque_no_ley11_2007_repealed_with_tengase.xml",
    )

    overview = await get_law_overview(client, LEY_30_1992, now=NOW)

    assert overview.initial_note is not None
    assert len(overview.initial_note) == 600
    assert overview.initial_note.endswith("\u2026")


async def test_overview_of_a_flagged_law_without_a_note_is_repealed_with_no_note(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    # Ley 30/2007: flag S, and the BOE serves no initial note (HTTP 404).
    law_id = "BOE-A-2007-18874"
    mock_law(
        router,
        json_response,
        load_fixture,
        law_id,
        "metadatos_repealed_ley30_2007_no_initial_note.json",
    )

    overview = await get_law_overview(client, law_id, now=NOW)

    assert overview.status == "repealed"
    assert overview.initial_note is None
    assert "could not be checked" not in overview.status_note


async def test_overview_fetches_metadata_analysis_and_note_concurrently(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    note_requested = asyncio.Event()

    async def metadata_waits_for_the_note(request: httpx.Request) -> httpx.Response:
        # A sequential implementation would never request the note while this call waits.
        await asyncio.wait_for(note_requested.wait(), timeout=5)
        return json_response(load_fixture("metadatos_in_force_ley40_2015.json"))

    async def note_signals(request: httpx.Request) -> httpx.Response:
        note_requested.set()
        return httpx.Response(
            404,
            content=load_fixture("error_404_bloque_no_in_force_law.xml"),
            headers={"Content-Type": "application/xml"},
        )

    router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(side_effect=metadata_waits_for_the_note)
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/texto/bloque/no").mock(side_effect=note_signals)

    overview = await get_law_overview(client, LEY_40, now=NOW)

    assert overview.identifier == LEY_40


@pytest.mark.parametrize("status_code", [400, 503])
async def test_overview_survives_a_failing_note_request(
    router: respx.MockRouter,
    http: httpx.AsyncClient,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
    status_code: int,
) -> None:
    no_retries = BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(
        return_value=json_response(load_fixture("analisis_ley40_2015.json"))
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/texto/bloque/no").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), status_code)
    )

    overview = await get_law_overview(no_retries, LEY_40, today=date(2026, 11, 21), now=NOW)

    assert overview.status == "partially_repealed"
    assert overview.initial_note is None
    assert "initial note could not be checked" in overview.status_note
    assert overview.amendments.affected_by_count > 0


class _FailingClient(BoeClient):
    """A client whose three law calls fail as scripted, to test which error is reported."""

    def __init__(self, http: httpx.AsyncClient, *, metadata_late: bool) -> None:
        super().__init__(cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http)
        self._metadata_late = metadata_late

    async def get_law_metadata(self, law_id: str) -> Any:
        if self._metadata_late:
            # One event-loop turn later than the analysis, but before the cancellation arrives.
            await asyncio.sleep(0)
        raise BoeNotFoundError("metadata: no such law")

    async def get_law_analysis(self, law_id: str) -> Any:
        raise BoeUnavailableError("analysis: down")

    async def get_initial_note(self, law_id: str) -> Any:
        raise BoeUnavailableError("note: down")


@pytest.mark.parametrize("metadata_late", [True, False])
async def test_overview_reports_the_metadata_error_when_several_calls_fail(
    http: httpx.AsyncClient, metadata_late: bool
) -> None:
    failing = _FailingClient(http, metadata_late=metadata_late)

    with pytest.raises(BoeNotFoundError, match="metadata"):
        await get_law_overview(failing, LEY_40)


async def test_overview_reports_the_first_failure_when_metadata_succeeded(
    http: httpx.AsyncClient,
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_fixture: Loader,
) -> None:
    client = BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/metadatos").mock(
        return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(return_value=httpx.Response(503))
    mock_note(router, load_fixture, LEY_40)

    with pytest.raises(BoeUnavailableError):
        await get_law_overview(client, LEY_40)


# --- resolve_reference: exact-number matches beyond the first page ---------------------------


def numbered_hits(start: int, count: int) -> list[dict[str, Any]]:
    """Hits that match a loose number search but are not the number asked for."""
    return [raw_hit(f"BOE-A-2000-{start + n}", number=f"{start + n}/2000") for n in range(count)]


async def test_an_exact_number_beyond_the_first_page_is_found(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(
        router,
        json_response,
        search_body(*numbered_hits(1, 50)),
        search_body(*numbered_hits(51, 10), raw_hit("BOE-A-2018-16673", number="3/2018")),
    )

    result = await resolve_reference(client, "Ley 3/2018")

    assert result.status == "resolved"
    assert [m.identifier for m in result.matches] == ["BOE-A-2018-16673"]
    assert [sent_params(route, n)["offset"] for n in range(2)] == ["0", "50"]


async def test_paging_stops_at_the_first_short_page(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    route = mock_searches(
        router,
        json_response,
        search_body(*numbered_hits(1, 50)),
        search_body(*numbered_hits(51, 49)),  # short page: the state search ends here
        search_body(),  # the any-scope fallback
    )

    result = await resolve_reference(client, "Ley 3/2018")

    assert result.status == "not_found"
    assert route.call_count == 3


async def test_paging_stops_after_200_hits(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    pages = [search_body(*numbered_hits(1 + 50 * n, 50)) for n in range(8)]
    route = mock_searches(router, json_response, *pages)

    result = await resolve_reference(client, "Ley 3/2018")

    assert result.status == "not_found"
    assert route.call_count == 8  # 4 pages of 50 in each of the two scopes, never a 5th


# --- get_amendment_history --------------------------------------------------------------------

# Expected group of every relation text of the BOE "relaciones-posteriores" table (the fixture
# holds all 50), by relation code. A new code in the table makes the first test fail, so a relation
# can never silently land in "other".
RELATION_GROUPS = {
    **dict.fromkeys(("201", "202", "203", "204"), "corrections"),
    **dict.fromkeys(("210", "211", "212", "213", "214", "215", "216", "217"), "repeals"),
    **dict.fromkeys(("220", "221", "230", "235"), "repeals"),  # annulled, set aside, suppressed
    **dict.fromkeys(("245", "270", "271", "272", "404"), "modifies"),
    **dict.fromkeys(("406", "407", "408"), "adds"),
    **dict.fromkeys(
        (
            *("231", "247", "300", "301", "303", "330", "331", "400", "401", "402", "420"),
            *("421", "422", "426", "427", "430", "440", "470", "480", "490", "520", "530"),
            *("540", "552", "693", "694"),
        ),
        "other",
    ),
}


def test_every_relation_of_the_boe_table_has_an_expected_group(
    load_json: Callable[[str], Any],
) -> None:
    table = load_json("aux_relaciones_posteriores.json")["data"]

    assert set(table) == set(RELATION_GROUPS)


@pytest.mark.parametrize("code", sorted(RELATION_GROUPS))
def test_relation_texts_land_in_the_expected_group(
    load_json: Callable[[str], Any], code: str
) -> None:
    relation = load_json("aux_relaciones_posteriores.json")["data"][code]

    assert relation_group(relation) == RELATION_GROUPS[code], relation


def later_body(*references: tuple[str, str, str]) -> bytes:
    """An analysis whose later norms are ``(identifier, relation text, reference text)``."""
    later = [
        {"id_norma": law_id, "relacion": {"codigo": "270", "texto": relation}, "texto": text}
        for law_id, relation, text in references
    ]
    document = {
        "status": {"code": "200", "text": "ok"},
        "data": [{"referencias": {"posteriores": [{"posterior": later}]}}],
    }
    return json.dumps(document).encode()


HISTORY = (
    ("BOE-A-2018-1", "SE MODIFICA", "art. 1"),
    ("BOE-A-2020-2", "SE DEROGA", "art. 2"),
    ("BOE-A-2020-3", "SE MODIFICA", "art. 3"),
    ("BOE-A-2015-4", "SE AÑADE", "art. 4"),
    ("BOE-A-2020-5", "CORRECCIÓN de errores", "art. 5"),
    ("DOUE-L-2019-6", "SE DICTA", "art. 6"),
    ("BOE-X-NOYEAR", "SE MODIFICA", "art. 7"),
)


@pytest.fixture
def history_routes(router: respx.MockRouter, json_response: JsonResponse) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(
        return_value=json_response(later_body(*HISTORY))
    )


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_counts_every_group_over_all_references(client: BoeClient) -> None:
    result = await get_amendment_history(client, LEY_40, group="repeals", now=NOW)

    assert result.groups == {
        "repeals": 1,
        "modifies": 3,
        "adds": 1,
        "corrections": 1,
        "other": 1,
    }
    assert result.total == 1
    assert [item.law_id for item in result.items] == ["BOE-A-2020-2"]


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_lists_newest_year_first_keeping_the_api_order(
    client: BoeClient,
) -> None:
    result = await get_amendment_history(client, LEY_40, now=NOW)

    assert [item.law_id for item in result.items] == [
        "BOE-A-2020-2",
        "BOE-A-2020-3",
        "BOE-A-2020-5",
        "DOUE-L-2019-6",
        "BOE-A-2018-1",
        "BOE-A-2015-4",
        "BOE-X-NOYEAR",
    ]
    assert result.items[0].year == 2020
    assert result.items[-1].year is None
    assert (result.items[0].relation, result.items[0].group) == ("SE DEROGA", "repeals")
    assert result.items[0].text == "art. 2"
    assert result.total == 7


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_counts_per_year_newest_first_with_undated_last(
    client: BoeClient,
) -> None:
    result = await get_amendment_history(client, LEY_40, now=NOW)

    assert [(y.year, y.count) for y in result.by_year] == [
        (2020, 3),
        (2019, 1),
        (2018, 1),
        (2015, 1),
        (None, 1),
    ]


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_by_year_follows_the_selected_group(client: BoeClient) -> None:
    result = await get_amendment_history(client, LEY_40, group="modifies", now=NOW)

    assert [(y.year, y.count) for y in result.by_year] == [(2020, 1), (2018, 1), (None, 1)]
    assert result.total == 3


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_pages_with_has_more_and_next_offset(client: BoeClient) -> None:
    first = await get_amendment_history(client, LEY_40, limit=3, now=NOW)
    last = await get_amendment_history(client, LEY_40, limit=3, offset=6, now=NOW)

    assert (len(first.items), first.has_more, first.next_offset) == (3, True, 3)
    assert (len(last.items), last.has_more, last.next_offset) == (1, False, None)


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_pages_cover_every_item_once(client: BoeClient) -> None:
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = await get_amendment_history(client, LEY_40, limit=2, offset=offset, now=NOW)
        seen.extend(item.law_id for item in page.items)
        offset = page.next_offset

    assert sorted(seen) == sorted(law_id for law_id, _, _ in HISTORY)


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_offset_past_the_end_is_an_empty_page(client: BoeClient) -> None:
    result = await get_amendment_history(client, LEY_40, offset=50, now=NOW)

    assert (result.items, result.has_more, result.next_offset, result.total) == ([], False, None, 7)


async def test_amendment_history_of_a_law_nobody_amended_is_empty(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse
) -> None:
    router.get(f"{SEARCH_URL}/id/{LEY_40}/analisis").mock(
        return_value=json_response(analysis_body())
    )

    result = await get_amendment_history(client, LEY_40, now=NOW)

    assert (result.total, result.items, result.by_year) == (0, [], [])
    assert set(result.groups.values()) == {0}


@pytest.mark.usefixtures("history_routes")
async def test_amendment_history_uses_only_the_analysis_request_and_stamps_provenance(
    client: BoeClient, router: respx.MockRouter
) -> None:
    result = await get_amendment_history(client, f" {LEY_40.lower()} ", now=NOW)

    assert [call.request.url.path.rsplit("/", 1)[-1] for call in router.calls] == ["analisis"]
    assert result.law_id == LEY_40
    assert result.provenance.generated_at == NOW
    assert result.provenance.attribution.startswith("Basado en datos")


async def test_amendment_history_of_an_unknown_law_raises_not_found(
    client: BoeClient, router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(f"{SEARCH_URL}/id/BOE-A-1999-1/analisis").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    with pytest.raises(BoeNotFoundError):
        await get_amendment_history(client, "BOE-A-1999-1")


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"group": "everything"}],
)
async def test_amendment_history_rejects_invalid_arguments_without_a_request(
    client: BoeClient, router: respx.MockRouter, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await get_amendment_history(client, LEY_40, **arguments)

    assert router.calls.call_count == 0


# --- find_codes -------------------------------------------------------------------------------

AUX_URL = f"{BASE_URL}datos-auxiliares"


def mock_table(
    router: respx.MockRouter,
    json_response: JsonResponse,
    load_fixture: Loader,
    table: str,
    fixture: str,
) -> respx.Route:
    return router.get(f"{AUX_URL}/{table}").mock(return_value=json_response(load_fixture(fixture)))


async def test_find_codes_matches_departments_ignoring_case_and_accents(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_table(
        router, json_response, load_fixture, "departamentos", "aux_departamentos_trimmed.json"
    )

    result = await find_codes(client, "department", "PROTECCION de datos", now=NOW)

    assert [(m.code, m.text) for m in result.matches] == [
        ("1012", "Agencia de Protección de Datos"),
        ("1011", "Agencia Española de Protección de Datos"),
    ]
    assert (result.kind, result.query) == ("department", "PROTECCION de datos")
    assert (result.returned, result.total_matches) == (2, 2)
    assert result.provenance.generated_at == NOW
    assert result.provenance.attribution.startswith("Basado en datos")


async def test_find_codes_requires_every_word_to_match(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_table(router, json_response, load_fixture, "materias", "aux_materias_trimmed.json")

    both = await find_codes(client, "subject", "academia policia")
    neither = await find_codes(client, "subject", "academia fiscalia")

    assert [m.code for m in both.matches] == ["20", "23"]
    assert neither.matches == []
    assert neither.total_matches == 0


async def test_find_codes_limits_the_page_but_reports_every_match(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_table(router, json_response, load_fixture, "materias", "aux_materias_trimmed.json")

    result = await find_codes(client, "subject", "academia", limit=3)

    assert result.returned == 3
    assert len(result.matches) == 3
    assert result.total_matches == 9


async def test_find_codes_lists_ranks_with_their_codes(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_table(router, json_response, load_fixture, "rangos", "aux_rangos.json")

    result = await find_codes(client, "rank", "decreto")

    assert {(m.code, m.text) for m in result.matches} == {
        ("1510", "Decreto"),
        ("1470", "Decreto Legislativo"),
        ("1500", "Decreto-ley"),
        ("1480", "Decreto Foral Legislativo"),
        ("1325", "Decreto-ley Foral"),
        ("1340", "Real Decreto"),
        ("1310", "Real Decreto Legislativo"),
        ("1320", "Real Decreto-ley"),
    }
    assert result.matches[0].text == "Decreto"  # the shortest text first


async def test_find_codes_uses_the_table_of_the_requested_kind(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    route = mock_table(router, json_response, load_fixture, "rangos", "aux_rangos.json")

    await find_codes(client, "rank", "ley")

    assert route.call_count == 1
    assert router.calls.call_count == 1


@pytest.mark.parametrize(
    ("kind", "text", "limit", "message"),
    [
        ("rank", "   ", 20, "text"),
        ("rank", "ley", 0, "limit"),
        ("rank", "ley", 51, "limit"),
        ("thesaurus", "ley", 20, "kind"),
    ],
)
async def test_find_codes_rejects_invalid_arguments_without_a_request(
    client: BoeClient, router: respx.MockRouter, kind: Any, text: str, limit: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await find_codes(client, kind, text, limit=limit)

    assert router.calls.call_count == 0


async def test_overview_of_a_law_with_a_future_dated_repeal_note_is_not_repealed_yet(
    client: BoeClient, router: respx.MockRouter, json_response: JsonResponse, load_fixture: Loader
) -> None:
    mock_law(router, json_response, load_fixture, LEY_40, "metadatos_in_force_ley40_2015.json")
    future = load_fixture("texto_bloque_no_lopd1999_repealed.xml").replace(
        b"7 de diciembre de 2018", b"7 de diciembre de 2026"
    )
    router.get(f"{SEARCH_URL}/id/{LEY_40}/texto/bloque/no").mock(
        return_value=httpx.Response(
            200, content=future, headers={"Content-Type": "application/xml"}
        )
    )

    overview = await get_law_overview(client, LEY_40, today=date(2026, 11, 21), now=NOW)

    # The status is the one the rest of the evidence gives (Ley 40/2015's analysis lists repeals
    # of parts of it), never "repealed".
    assert overview.status == "partially_repealed"
    assert overview.repealed_on is None
    assert "2026-12-07" in overview.status_note
    assert "announced" in overview.status_note
