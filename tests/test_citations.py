"""Tests of ``citations``: the use case behind ``verify_citation``.

The BOE API is mocked with respx. Laws and articles are small, built in the test, except where a
recorded response gives the real wording (the repealed Ley 30/1992). The time budget uses an
injected clock that advances only when a request is answered, so nothing here sleeps or reads the
real clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp import citations
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.schemas import CitationRequest, VerifyCitationResult

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 21)

LAW = "BOE-A-2015-10566"
OTHER_LAW = "BOE-A-2016-1"
LEY_30 = "BOE-A-1992-26318"

Loader = Callable[[str], bytes]

OLD = (
    "El convenio se perfecciona por la prestación del consentimiento de las partes en el plazo "
    "de 15 días."
)
NEW = (
    "El convenio se perfecciona por la prestación del consentimiento de las partes en el plazo "
    "de 5 días hábiles desde su formalización."
)


@pytest.fixture
def client(http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


def index_body(*entries: tuple[str, str, str]) -> bytes:
    """A text index with ``(block id, title, fecha_actualizacion)`` entries."""
    blocks = [{"id": i, "titulo": t, "fecha_actualizacion": d} for i, t, d in entries]
    document = {"status": {"code": "200", "text": "ok"}, "data": [{"bloque": blocks}]}
    return json.dumps(document).encode()


def block_xml(block_id: str, title: str, *versions: tuple[str, str, str, Sequence[str]]) -> bytes:
    """A block response with ``(id_norma, publicacion, vigencia, paragraph texts)`` versions."""
    parts = []
    for law_id, published, effective, paragraphs in versions:
        body = "".join(f'<p class="parrafo">{text}</p>' for text in paragraphs)
        parts.append(
            f'<version id_norma="{law_id}" fecha_publicacion="{published}" '
            f'fecha_vigencia="{effective}">{body}</version>'
        )
    return (
        "<response><status><code>200</code><text>ok</text></status><data>"
        f'<bloque id="{block_id}" tipo="precepto" titulo="{title}">{"".join(parts)}</bloque>'
        "</data></response>"
    ).encode()


def xml_ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "application/xml"})


def json_ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


class Mocks:
    """Mocks the endpoints of the laws of one test and counts what was requested."""

    def __init__(self, router: respx.MockRouter, load_fixture: Loader) -> None:
        self.router = router
        self.load = load_fixture

    def law(
        self,
        law_id: str,
        index: bytes,
        blocks: dict[str, bytes],
        *,
        metadata: str = "metadatos_in_force_ley40_2015.json",
        on_index: Callable[[], None] | None = None,
    ) -> None:
        """Serve a law's metadata, index and blocks; its initial note is absent.

        ``on_index`` runs each time the index is requested (tests move their clock with it).
        """
        base = f"{BASE_URL}legislacion-consolidada/id/{law_id}"
        self.router.get(f"{base}/metadatos").mock(return_value=json_ok(self.load(metadata)))

        def answer_index(request_: httpx.Request) -> httpx.Response:
            if on_index is not None:
                on_index()
            return json_ok(index)

        self.router.get(f"{base}/texto/indice").mock(side_effect=answer_index)
        self.router.get(f"{base}/texto/bloque/no").mock(
            return_value=httpx.Response(
                404,
                content=self.load("error_404_bloque_no_in_force_law.xml"),
                headers={"Content-Type": "application/xml"},
            )
        )
        for block_id, body in blocks.items():
            self.router.get(f"{base}/texto/bloque/{block_id}").mock(return_value=xml_ok(body))

    def paths(self) -> list[str]:
        """The path of every request so far, from the law id on."""
        return [call.request.url.path.split("/id/")[-1] for call in self.router.calls]


@pytest.fixture
def mocks(router: respx.MockRouter, load_fixture: Loader) -> Mocks:
    return Mocks(router, load_fixture)


def article48(mocks: Mocks, on_index: Callable[[], None] | None = None) -> None:
    """Ley 40/2015 with one article whose 2016 wording was replaced in 2021."""
    mocks.law(
        LAW,
        index_body(("a48", "Artículo 48", "20210101")),
        {
            "a48": block_xml(
                "a48",
                "Artículo 48",
                ("BOE-A-2015-10566", "20151002", "20161002", ["Artículo 48. Convenios.", OLD]),
                ("BOE-A-2020-17340", "20201231", "20210101", ["Artículo 48. Convenios.", NEW]),
            )
        },
        on_index=on_index,
    )


def request(
    article: str | None = "artículo 48",
    quote: str | None = None,
    law: str = LAW,
    as_of: date | None = None,
    block_id: str | None = None,
) -> CitationRequest:
    return CitationRequest(law=law, article=article, quote=quote, as_of=as_of, block_id=block_id)


async def verify(
    client: BoeClient, *requests: CitationRequest, **kwargs: Any
) -> VerifyCitationResult:
    options: dict[str, Any] = {"now": NOW, "today": TODAY}
    options.update(kwargs)
    return await citations.verify_citations(client, list(requests), **options)


# --- the statuses ------------------------------------------------------------------------------


async def test_an_exact_quote_of_the_current_text_matches(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(
        client, request(quote="en el plazo de 5 días hábiles desde su formalización")
    )

    check = result.results[0]
    assert check.status == "matches"
    assert (check.law_id, check.block_id, check.article_label) == (LAW, "a48", "Artículo 48")
    assert (check.state, check.law_repealed, check.as_of) == ("in_force", False, TODAY)
    assert check.quote_match is not None
    assert (check.quote_match.level, check.quote_match.similarity) == ("exact", 1.0)
    assert check.quote_match.matched_text.startswith("en el plazo de 5 días")
    assert check.other_version is None
    assert (result.checked, result.not_checked) == (1, 0)


async def test_a_citation_without_a_quote_only_checks_the_article(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(client, request())

    check = result.results[0]
    assert (check.status, check.state, check.quote_match, check.other_version) == (
        "in_force",  # A5: no quote, so nothing can "match"
        "in_force",
        None,
        None,
    )


async def test_quotes_ignore_case_punctuation_and_quotation_marks(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(client, request(quote="«EL CONVENIO se perfecciona, por la prestación»"))

    assert result.results[0].status == "matches"


async def test_the_wording_of_an_earlier_version_is_reported_as_such(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(client, request(quote="de las partes en el plazo de 15 días"))

    check = result.results[0]
    assert check.status == "matches_older_version"
    assert check.quote_match is not None and check.quote_match.level in ("partial", "close", "none")
    assert check.other_version is not None
    assert check.other_version.model_dump() == {
        "relation": "earlier",
        "starts_on": date(2016, 10, 2),
        "ends_on": date(2020, 12, 31),
        "level": "exact",
        "similarity": 1.0,
        "matched_text": "de las partes en el plazo de 15 días",
        "differences": "",
    }


async def test_the_wording_of_a_later_version_is_reported_with_a_past_date(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(
        client,
        request(
            quote="en el plazo de 5 días hábiles desde su formalización", as_of=date(2017, 1, 1)
        ),
    )

    check = result.results[0]
    assert check.status == "matches_later_version"
    assert check.as_of == date(2017, 1, 1)
    assert check.other_version is not None
    assert (check.other_version.relation, check.other_version.starts_on) == (
        "later",
        date(2021, 1, 1),
    )
    assert check.other_version.ends_on is None


async def test_a_changed_number_makes_the_text_differ(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(
        client,
        request(
            quote=(
                "consentimiento de las partes en el plazo de 15 días hábiles desde su formalización"
            )
        ),
    )

    check = result.results[0]
    assert check.status == "text_differs"
    assert check.quote_match is not None
    assert check.quote_match.level in ("close", "partial")
    assert 0.5 <= check.quote_match.similarity < 1.0
    assert "[-15-]" in check.quote_match.differences
    assert "{+5+}" in check.quote_match.differences


async def test_words_found_in_no_version_are_not_found(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(
        client, request(quote="los hornos crematorios serán de titularidad pública")
    )

    check = result.results[0]
    assert check.status == "quote_not_found"
    assert check.quote_match is not None and check.quote_match.level == "none"
    assert check.quote_match.matched_text == ""


async def test_omission_marks_let_a_quote_skip_text(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(
        client, request(quote="El convenio se perfecciona (...) en el plazo de 5 días hábiles")
    )

    assert result.results[0].status == "matches"


async def test_matched_text_is_capped_at_600_characters(client: BoeClient, mocks: Mocks) -> None:
    long_text = " ".join(f"palabra{n}" for n in range(300))
    mocks.law(
        LAW,
        index_body(("a1", "Artículo 1", "20200101")),
        {"a1": block_xml("a1", "Artículo 1", (LAW, "20151002", "20161002", [long_text]))},
    )

    result = await verify(client, request("artículo 1", quote="palabra0 (...) palabra299"))

    matched = result.results[0].quote_match
    assert matched is not None and matched.level == "exact"
    assert len(matched.matched_text) == 600 and matched.matched_text.endswith("…")


async def test_an_article_that_does_not_exist_is_not_found(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(client, request("artículo 999", quote="algo de interés"))

    check = result.results[0]
    assert check.status == "article_not_found"
    assert (check.law_id, check.block_id, check.quote_match) == (LAW, None, None)
    assert check.note is not None
    assert (result.checked, result.not_checked) == (1, 0)


async def test_two_blocks_with_the_same_designation_are_ambiguous(
    client: BoeClient, mocks: Mocks
) -> None:
    mocks.law(
        LAW,
        index_body(("a1", "Artículo 1", "20200101"), ("a1-2", "Artículo 1", "20200101")),
        {},
    )

    result = await verify(client, request("artículo 1"))

    check = result.results[0]
    assert check.status == "article_ambiguous"
    assert [c.model_dump() for c in check.candidates] == [
        {"block_id": "a1", "title": "Artículo 1"},
        {"block_id": "a1-2", "title": "Artículo 1"},
    ]


async def test_an_unknown_identifier_is_law_not_found(
    client: BoeClient, router: respx.MockRouter, load_fixture: Loader
) -> None:
    router.get(f"{BASE_URL}legislacion-consolidada/id/BOE-A-1999-1/metadatos").mock(
        return_value=httpx.Response(
            404,
            content=load_fixture("error_404_unknown_id.xml"),
            headers={"Content-Type": "application/xml"},
        )
    )

    result = await verify(client, request(law="BOE-A-1999-1"))

    check = result.results[0]
    assert (check.status, check.law_id, check.state) == ("law_not_found", None, None)
    assert check.note is not None
    assert (result.checked, result.not_checked) == (1, 0)


async def test_a_reference_with_several_laws_is_ambiguous_and_lists_them(
    client: BoeClient, router: respx.MockRouter, load_fixture: Loader
) -> None:
    router.get(f"{BASE_URL}legislacion-consolidada").mock(
        return_value=json_ok(load_fixture("search_numero_oficial_ambiguous.json"))
    )

    result = await verify(client, request(law="Ley 3/2018"))

    check = result.results[0]
    assert check.status == "law_ambiguous"
    assert len(check.candidates) == 5  # A10: candidates are capped at 5
    assert check.model_dump()["candidates"][0]["identifier"].startswith("BOE-")


async def test_a_name_resolves_to_the_law_before_the_article_is_looked_up(
    client: BoeClient, router: respx.MockRouter, load_fixture: Loader, mocks: Mocks
) -> None:
    router.get(f"{BASE_URL}legislacion-consolidada").mock(
        return_value=json_ok(load_fixture("search_numero_oficial_rango_single.json"))
    )
    mocks.law(
        "BOE-A-2018-16673",
        index_body(("a1", "Artículo 1", "20200101")),
        {
            "a1": block_xml(
                "a1", "Artículo 1", ("BOE-A-2018-16673", "20181206", "20181207", ["Objeto."])
            )
        },
    )

    result = await verify(client, request("artículo 1", law="LO 3/2018"))

    check = result.results[0]
    assert (check.status, check.law_id) == ("in_force", "BOE-A-2018-16673")
    assert check.law_title is not None and "Protección de Datos" in check.law_title
    assert check.law == "LO 3/2018"


# --- repealed laws -----------------------------------------------------------------------------


def ley_30(mocks: Mocks) -> None:
    """Ley 30/1992, repealed as a whole (recorded metadata and initial note)."""
    mocks.law(
        LEY_30,
        index_body(("no", "", "20161002"), ("a1", "Artículo 1", "20161002")),
        {
            "a1": block_xml(
                "a1",
                "Artículo 1",
                (LEY_30, "19921127", "19930227", ["Artículo 1. Objeto.", "Objeto de la ley."]),
            )
        },
        metadata="metadatos_repealed_ley30_1992.json",
    )
    mocks.router.get(f"{BASE_URL}legislacion-consolidada/id/{LEY_30}/texto/bloque/no").mock(
        return_value=xml_ok(
            mocks.load("texto_bloque_no_ley30_1992_two_versions_empty_vigencia.xml")
        )
    )


async def test_an_article_of_a_repealed_law_is_not_in_force(
    client: BoeClient, mocks: Mocks
) -> None:
    ley_30(mocks)

    result = await verify(client, request("artículo 1", law=LEY_30))

    check = result.results[0]
    assert check.status == "not_in_force"
    assert (check.state, check.law_repealed) == ("repealed", True)
    assert check.note is not None and "repealed" in check.note
    assert (result.checked, result.not_checked) == (1, 0)


async def test_an_exact_quote_of_a_repealed_article_is_not_in_force_either(
    client: BoeClient, mocks: Mocks
) -> None:
    ley_30(mocks)

    result = await verify(client, request("artículo 1", quote="Objeto de la ley", law=LEY_30))

    check = result.results[0]
    assert check.status == "not_in_force"
    assert check.quote_match is not None and check.quote_match.level == "exact"


async def test_a_date_before_the_repeal_finds_the_law_in_force(
    client: BoeClient, mocks: Mocks
) -> None:
    ley_30(mocks)

    result = await verify(client, request("artículo 1", law=LEY_30, as_of=date(2010, 1, 1)))

    check = result.results[0]
    assert (check.status, check.state) == ("in_force", "in_force")
    assert check.note is not None and "repealed later" in check.note


# --- the article wording -----------------------------------------------------------------------


async def test_the_part_of_the_wording_that_was_ignored_is_noted(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(client, request("artículo 48.3"))

    check = result.results[0]
    assert check.status == "in_force"
    assert check.note == "Checked against the whole article; '.3' was not used to narrow the text."


# --- several citations, one request per fact ---------------------------------------------------


async def test_citations_are_answered_in_order_and_a_shared_law_costs_no_extra_request(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(
        client,
        request(quote="en el plazo de 5 días hábiles desde su formalización"),
        request(quote="en el plazo de 15 días"),
        request("artículo 999"),
        request(),
    )

    assert [c.status for c in result.results] == [
        "matches",
        "matches_older_version",
        "article_not_found",
        "in_force",
    ]
    assert [c.article for c in result.results] == [
        "artículo 48",
        "artículo 48",
        "artículo 999",
        "artículo 48",
    ]
    assert (result.checked, result.not_checked) == (4, 0)
    paths = mocks.paths()
    # Metadata, index, block and the (absent) initial note are each read once for all four.
    assert sorted(paths) == sorted(
        [f"{LAW}/metadatos", f"{LAW}/texto/indice", f"{LAW}/texto/bloque/a48"]
    )


async def test_provenance_carries_the_newest_article_stamp(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(client, request())

    assert result.provenance.generated_at == NOW
    assert result.provenance.data_updated_at == datetime(2021, 1, 1, tzinfo=UTC)
    assert result.provenance.attribution.startswith("Basado en datos")
    assert "Consolidated texts" in result.provenance.notice


# --- the time budget and failures --------------------------------------------------------------


class Clock:
    """A clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


async def test_the_remaining_citations_come_back_unchecked_with_a_note(
    client: BoeClient, mocks: Mocks
) -> None:
    clock = Clock()

    def spend() -> None:
        clock.now += 36.0  # the first citation uses up 36 of the 40 seconds

    article48(mocks, on_index=spend)

    result = await verify(
        client, request(), request("artículo 999"), request(law=OTHER_LAW), clock=clock
    )

    first, second, third = result.results
    assert first.status == "in_force"
    for skipped in (second, third):
        assert skipped.status is None
        assert (
            skipped.note
            == "Not checked: time limit reached. Call again with the remaining citations."
        )
        assert skipped.model_dump()["status"] is None
    assert (result.checked, result.not_checked) == (1, 2)
    assert (second.law, second.article) == (LAW, "artículo 999")
    # Nothing was requested for the skipped ones.
    assert not any(OTHER_LAW in path for path in mocks.paths())


async def test_a_failing_citation_is_reported_unchecked_and_the_others_go_on(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)
    mocks.router.get(f"{BASE_URL}legislacion-consolidada/id/{OTHER_LAW}/metadatos").mock(
        return_value=httpx.Response(503)
    )

    result = await verify(client, request(law=OTHER_LAW), request())

    broken, fine = result.results
    assert broken.status is None
    assert broken.note is not None
    # The note is the curated message of the tool errors, never the API's own text.
    assert broken.note == (
        "Not checked: BOE API unavailable (unavailable, retryable): Wait a few seconds and try "
        "again. Call again with this citation."
    )
    assert fine.status == "in_force"
    assert (result.checked, result.not_checked) == (1, 1)


# --- bad arguments -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requests", "message"),
    [
        ([], "between 1 and 10"),
        ([request()] * 11, "between 1 and 10"),
        ([request(law="  ")], "law must not be empty"),
        ([request(article="  ")], "give article or block_id"),
        ([request(article="x" * 201)], "at most 200"),
        ([request(quote="«»…")], "quote must contain words"),
    ],
)
async def test_bad_arguments_are_refused_before_any_request(
    client: BoeClient, mocks: Mocks, requests: list[CitationRequest], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await verify(client, *requests)
    assert mocks.router.calls.call_count == 0


# --- statuses that need a closer look at the facts ---------------------------------------------

EDITORIAL_BLOCK = (
    "<response><status><code>200</code><text>ok</text></status><data>"
    '<bloque id="a48" tipo="precepto" titulo="Artículo 48">'
    '<version id_norma="BOE-A-2015-10566" fecha_publicacion="20151002" fecha_vigencia="20161002">'
    '<p class="articulo">Artículo 48. Convenios.</p>'
    '<p class="parrafo">Los convenios no podrán superar los gastos previstos, salvo pacto en '
    "contrario de las partes firmantes.</p>"
    '<p class="parrafo">Los firmantes publicarán el convenio en el plazo de 5 días hábiles.</p>'
    '<p class="nota_pie">Se modifica el apartado 8 por la disposición final segunda del Real '
    "Decreto-ley 36/2020, de 30 de diciembre.</p>"
    "</version></bloque></data></response>"
).encode()


def editorial_article(mocks: Mocks) -> None:
    """A one-version article with a footnote of the BOE under its text."""
    mocks.law(
        LAW,
        index_body(("a48", "Artículo 48", "20210101")),
        {"a48": EDITORIAL_BLOCK},
    )


async def test_the_words_of_an_editorial_note_are_not_the_articles_text(
    client: BoeClient, mocks: Mocks
) -> None:
    editorial_article(mocks)

    result = await verify(
        client,
        request(quote="Se modifica el apartado 8 por la disposición final segunda del Real"),
    )

    check = result.results[0]
    assert check.status == "quote_not_found"
    assert check.note == (
        "The words match an editorial note of the BOE about this article, not the article's text."
    )


async def test_a_dropped_negation_is_flagged_and_an_omission_is_shown(
    client: BoeClient, mocks: Mocks
) -> None:
    editorial_article(mocks)

    result = await verify(
        client,
        request(
            quote="Los convenios podrán superar los gastos previstos, salvo pacto en contrario"
        ),
        request(quote="Los convenios (...) superar los gastos previstos"),
    )

    changed, omitted = result.results
    assert changed.status == "text_differs"
    assert changed.quote_match is not None
    assert changed.quote_match.flags == ["negation"]
    assert omitted.status == "matches"
    assert omitted.quote_match is not None
    assert omitted.quote_match.omitted == ["no podrán"]
    assert omitted.quote_match.flags == ["omitted_negation"]


async def test_a_date_before_the_article_took_effect_is_not_in_force(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(
        client,
        request(
            quote="en el plazo de 5 días hábiles desde su formalización", as_of=date(2010, 1, 1)
        ),
        request(as_of=date(2010, 1, 1)),
    )

    with_quote, without = result.results
    assert (with_quote.status, with_quote.state) == ("not_in_force", "did_not_exist")
    # The later wording that does match is still reported.
    assert with_quote.other_version is not None
    assert (with_quote.other_version.relation, with_quote.other_version.level) == ("later", "exact")
    assert without.status == "not_in_force"


async def test_a_close_wording_of_another_version_is_only_text_differs(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(
        client,
        # One word off the 2016 wording, which is 0.06 closer to it than to the current text.
        request(quote=OLD.replace("perfecciona", "formaliza")),
    )

    check = result.results[0]
    assert check.status == "text_differs"
    assert check.other_version is not None
    assert check.other_version.level == "close"
    assert check.other_version.differences != ""
    assert check.other_version.matched_text != ""


async def test_a_block_id_replaces_the_article_wording(client: BoeClient, mocks: Mocks) -> None:
    article48(mocks)

    result = await verify(client, request(article=None, block_id="a48"))

    check = result.results[0]
    assert (check.status, check.block_id) == ("in_force", "a48")


async def test_an_article_that_is_not_found_gives_candidates_and_a_hint(
    client: BoeClient, mocks: Mocks
) -> None:
    article48(mocks)

    result = await verify(client, request("artículo 999"))

    check = result.results[0]
    assert check.status == "article_not_found"
    assert [c.model_dump()["block_id"] for c in check.candidates] == ["a48"]
    assert check.note is not None and "block_id" in check.note


async def test_matching_is_not_started_when_the_time_budget_is_spent(
    client: BoeClient, mocks: Mocks
) -> None:
    clock = Clock()

    def spend() -> None:
        clock.now += 41.0  # the lookup alone uses the whole budget

    article48(mocks, on_index=spend)

    result = await verify(client, request(quote="en el plazo de 5 días"), clock=clock)

    check = result.results[0]
    assert check.status is None
    assert check.note == "Not checked: time limit reached. Call again with the remaining citations."
    assert (result.checked, result.not_checked) == (0, 1)
