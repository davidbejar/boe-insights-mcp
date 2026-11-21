"""Tests for the article use cases of ``boe_insights_mcp.articles`` against a mocked BOE API.

Real recorded responses are used wherever one exists; blocks that no fixture covers (a range
block of the Código Civil, a block with 65 versions) are built from a small XML template.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from boe_insights_mcp.articles import (
    compare_article_versions,
    get_article_history,
    read_article,
)
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.errors import BoeNotFoundError, BoeUnavailableError
from boe_insights_mcp.ratelimit import RateLimiter

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 21)

LEY_40 = "BOE-A-2015-10566"
RD_1624 = "BOE-A-1992-28925"
RD_1006 = "BOE-A-2026-24302"
LOPD = "BOE-A-1999-23750"
IVA = "BOE-A-1992-28740"
IRPF = "BOE-A-2006-20764"
CODIGO_CIVIL = "BOE-A-1889-4763"

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http)


class LawText:
    """Mocks one law's text endpoints and records how the use case used them."""

    def __init__(
        self,
        router: respx.MockRouter,
        json_response: JsonResponse,
        xml_response: XmlResponse,
        load_fixture: Loader,
        law_id: str,
        index: bytes,
    ) -> None:
        self._router = router
        self._xml_response = xml_response
        self._load = load_fixture
        self._prefix = f"{BASE_URL}legislacion-consolidada/id/{law_id}/texto"
        self.index = router.get(f"{self._prefix}/indice").mock(return_value=json_response(index))
        self.blocks: dict[str, respx.Route] = {}
        # Every found article also asks for the law's metadata (is the whole law repealed?), and
        # for the initial note when the index lists one. By default the law is in force and has
        # no note; tests of repealed laws call ``repealed_law``/``initial_note``.
        self._json_response = json_response
        self.metadata = router.get(f"{BASE_URL}legislacion-consolidada/id/{law_id}/metadatos").mock(
            return_value=json_response(load_fixture("metadatos_in_force_ley40_2015.json"))
        )
        self.note = router.get(f"{self._prefix}/bloque/no").mock(
            return_value=httpx.Response(
                404,
                content=load_fixture("error_404_bloque_no_in_force_law.xml"),
                headers={"Content-Type": "application/xml;charset=UTF-8"},
            )
        )

    def fixture_bytes(self, fixture: str) -> bytes:
        """The raw bytes of a recorded response."""
        return self._load(fixture)

    def metadata_fixture(self, fixture: str) -> None:
        """Serve a recorded ``/metadatos`` response for this law."""
        self.metadata.mock(return_value=self._json_response(self._load(fixture)))

    def initial_note(self, fixture: str) -> None:
        """Serve a recorded initial note (block ``no``) for this law."""
        self.note.mock(
            return_value=httpx.Response(
                200,
                content=self._load(fixture),
                headers={"Content-Type": "application/xml;charset=UTF-8"},
            )
        )

    def block(self, block_id: str, body: bytes) -> None:
        """Serve ``body`` as the XML of block ``block_id``."""
        self.blocks[block_id] = self._router.get(f"{self._prefix}/bloque/{block_id}").mock(
            return_value=httpx.Response(
                200, content=body, headers={"Content-Type": "application/xml;charset=UTF-8"}
            )
        )

    def fixture_block(self, block_id: str, fixture: str) -> None:
        """Serve a recorded block response."""
        self.block(block_id, self._load(fixture))

    @property
    def requested_all(self) -> list[str]:
        """The last path segment of every request, in order (``metadatos``, ``indice``, blocks)."""
        return [call.request.url.path.rsplit("/", 1)[-1] for call in self._router.calls]

    @property
    def requested(self) -> list[str]:
        """The index and block requests in order; the repeal checks are in ``requested_all``."""
        return [r for r in self.requested_all if r not in ("metadatos", "no")]


@pytest.fixture
def law_text(
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> Callable[[str, bytes], LawText]:
    """Factory: ``law_text(law_id, index_bytes)`` mocks that law's index."""

    def _make(law_id: str, index: bytes) -> LawText:
        return LawText(router, json_response, xml_response, load_fixture, law_id, index)

    return _make


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


@pytest.fixture
def ley_40(law_text: Callable[[str, bytes], LawText], load_fixture: Loader) -> LawText:
    text = law_text(LEY_40, load_fixture("texto_indice_ley40_2015_trimmed.json"))
    text.fixture_block("a48", "texto_bloque_a48_two_versions.xml")
    text.fixture_block("a4-2", "texto_bloque_a4-2_articulo46bis.xml")
    return text


# --- read_article: found ------------------------------------------------------------------------


async def test_read_article_finds_the_block_by_title_and_reads_it_in_two_requests(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "46 bis", today=TODAY, now=NOW)

    assert (result.outcome, result.block_id, result.block_title) == (
        "found",
        "a4-2",
        "Artículo 46 bis",
    )
    assert result.state == "in_force"
    assert result.candidates == []
    assert result.hint is None
    assert ley_40.requested == ["indice", "a4-2"]


async def test_read_article_describes_the_version_it_returns(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "artículo 46 bis", today=TODAY, now=NOW)

    assert result.version is not None
    assert result.version.index == 1
    assert result.version.introduced_by == "BOE-A-2019-15790"
    assert result.version.effective_from == date(2019, 11, 6)
    assert result.version.published_on == date(2019, 11, 5)
    assert result.version.starts_on == date(2019, 11, 6)
    assert (result.version_count, result.is_latest, result.superseded_on) == (1, True, None)
    assert result.as_of == TODAY
    assert result.text.startswith("Artículo 46 bis. Ubicación de los sistemas")
    assert (result.total_chars, result.truncated, result.next_offset) == (
        len(result.text),
        False,
        None,
    )
    assert result.note is None


async def test_read_article_stamps_provenance_with_the_index_update_date(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "46 bis", today=TODAY, now=NOW)

    assert result.provenance.generated_at == NOW
    assert result.provenance.data_updated_at == datetime(2019, 11, 5, tzinfo=UTC)
    assert result.provenance.attribution.startswith("Basado en datos")
    assert result.law_id == LEY_40


async def test_read_article_accepts_a_block_id_and_a_lowercase_law_id(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, f" {LEY_40.lower()} ", "a48", today=TODAY, now=NOW)

    assert (result.law_id, result.block_id, result.block_title) == (LEY_40, "a48", "Artículo 48")


async def test_read_article_defaults_to_the_madrid_date_of_now(
    client: BoeClient, ley_40: LawText
) -> None:
    # 2020-12-31 23:30 UTC is already 2021-01-01 in Madrid, the day version 2 of article 48 starts.
    moment = datetime(2020, 12, 31, 23, 30, tzinfo=UTC)

    result = await read_article(client, LEY_40, "48", now=moment)

    assert result.as_of == date(2021, 1, 1)
    assert result.version is not None
    assert result.version.index == 2


# --- read_article: versions over time -------------------------------------------------------------


async def test_read_article_as_of_a_date_returns_the_version_in_force_then(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "artículo 48", as_of=date(2017, 1, 1), now=NOW)

    assert result.as_of == date(2017, 1, 1)
    assert result.version is not None
    assert (result.version.index, result.version.introduced_by) == (1, "BOE-A-2015-10566")
    assert (result.version_count, result.is_latest) == (2, False)
    assert result.superseded_on == date(2021, 1, 1)
    assert result.state == "in_force"


@pytest.mark.parametrize(
    ("as_of", "index"),
    [(date(2016, 10, 2), 1), (date(2020, 12, 31), 1), (date(2021, 1, 1), 2), (date(2030, 1, 1), 2)],
)
async def test_read_article_switches_version_on_the_effective_date(
    client: BoeClient, ley_40: LawText, as_of: date, index: int
) -> None:
    result = await read_article(client, LEY_40, "48", as_of=as_of, now=NOW)

    assert result.version is not None
    assert result.version.index == index
    assert result.is_latest is (index == 2)


async def test_read_article_before_the_article_existed_gives_no_text(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "46 bis", as_of=date(2019, 1, 1), now=NOW)

    assert result.outcome == "found"
    assert result.state == "did_not_exist"
    assert result.version is None
    assert result.text == ""
    assert (result.total_chars, result.truncated, result.next_offset) == (0, False, None)
    assert result.version_count == 1
    assert result.note is not None
    assert "did not exist" in result.note


async def test_read_article_published_but_not_yet_effective_returns_the_upcoming_text(
    client: BoeClient, ley_40: LawText
) -> None:
    # Published on 2019-11-05, in force from 2019-11-06.
    result = await read_article(client, LEY_40, "46 bis", as_of=date(2019, 11, 5), now=NOW)

    assert result.state == "not_yet_in_force"
    assert result.version is not None
    assert result.version.index == 1
    assert result.text.startswith("Artículo 46 bis.")
    assert result.note is not None
    assert "2019-11-06" in result.note


async def test_read_article_with_a_future_dated_version_returns_it_and_says_so(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1006, load_fixture("texto_indice_rd1006_2026_full.json"))
    text.fixture_block("a1", "texto_bloque_future_fecha_vigencia.xml")

    result = await read_article(client, RD_1006, "artículo 1", today=TODAY, now=NOW)

    assert result.state == "not_yet_in_force"
    assert result.version is not None
    assert result.version.effective_from == date(2027, 7, 1)
    assert result.text
    assert result.note is not None
    assert "2027-07-01" in result.note
    assert (result.is_latest, result.superseded_on) == (True, None)


async def test_read_article_reports_a_repealed_article_with_its_marker_and_footnote(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LOPD, load_fixture("texto_indice_lopd1999_repealed_with_no.json"))
    text.fixture_block("a40", "texto_bloque_a40_repealed_article_DERIVED.xml")

    result = await read_article(client, LOPD, "artículo 40", today=TODAY, now=NOW)

    assert result.state == "repealed"
    assert result.text.endswith("(Derogado).")
    assert result.notes == [
        "Se deroga por la disposición derogatoria única del Real Decreto-ley 5/2018, "
        "de 27 de julio. Ref. BOE-A-2018-10751#dd"
    ]


async def test_read_article_before_the_repeal_returns_the_original_text(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LOPD, load_fixture("texto_indice_lopd1999_repealed_with_no.json"))
    text.fixture_block("a40", "texto_bloque_a40_repealed_article_DERIVED.xml")

    result = await read_article(client, LOPD, "40", as_of=date(2010, 1, 1), now=NOW)

    assert result.state == "in_force"
    assert "Derogado" not in result.text


# --- read_article: not found and ambiguous ----------------------------------------------------


async def test_read_article_ambiguous_title_lists_every_match_and_asks_for_a_block_id(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))

    result = await read_article(client, RD_1624, "Artículo 1", today=TODAY, now=NOW)

    assert result.outcome == "ambiguous"
    assert (result.block_id, result.block_title) == (None, None)
    assert [(c.block_id, c.title) for c in result.candidates] == [
        ("a1", "Artículo 1"),
        ("a1-2", "Artículo 1"),
    ]
    assert result.hint is not None
    assert "block_id" in result.hint
    assert (result.state, result.version, result.text) == (None, None, "")
    assert text.requested == ["indice"]  # the block is never fetched


async def test_read_article_ambiguous_candidate_block_id_then_selects_the_block(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))
    text.fixture_block("a1-2", "texto_bloque_blockquote_rd1624_a1-2.xml")

    result = await read_article(client, RD_1624, "a1-2", today=TODAY, now=NOW)

    assert (result.outcome, result.block_id) == ("found", "a1-2")
    assert result.version is not None
    assert result.version.index == 3


async def test_read_article_not_found_offers_close_titles(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "artículo 9999", today=TODAY, now=NOW)

    assert result.outcome == "not_found"
    assert result.block_id is None
    assert 1 <= len(result.candidates) <= 8
    assert result.hint is not None
    assert "candidates" in result.hint
    assert ley_40.requested == ["indice"]
    # No block was read: the newest date of the index stands for the data (the index of the
    # trimmed Ley 40/2015 fixture has its latest block revision on this day).
    assert result.provenance.data_updated_at == datetime(2024, 8, 2, tzinfo=UTC)


async def test_read_article_of_an_unknown_law_raises_not_found(
    client: BoeClient, router: respx.MockRouter, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    router.get(f"{BASE_URL}legislacion-consolidada/id/BOE-A-1999-1/texto/indice").mock(
        return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
    )

    with pytest.raises(BoeNotFoundError):
        await read_article(client, "BOE-A-1999-1", "1", today=TODAY, now=NOW)


async def test_read_article_finds_an_article_inside_a_range_block(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(CODIGO_CIVIL, load_fixture("texto_indice_codigo_civil_trimmed.json"))
    text.block(
        "as127a130",
        block_xml(
            "as127a130",
            "Arts 127 a 130",
            ("BOE-A-2000-1", "20000108", "20000109", ["Artículos 127 a 130.", "Texto."]),
        ),
    )

    result = await read_article(client, CODIGO_CIVIL, "artículo 128", today=TODAY, now=NOW)

    assert (result.outcome, result.block_id, result.block_title) == (
        "found",
        "as127a130",
        "Arts 127 a 130",
    )
    assert result.text.startswith("Artículos 127 a 130.")


@pytest.mark.parametrize(
    ("law_id", "article"),
    [("not-an-id", "1"), (LEY_40, ""), (LEY_40, "   ")],
)
async def test_read_article_rejects_bad_arguments_without_a_request(
    client: BoeClient, router: respx.MockRouter, law_id: str, article: str
) -> None:
    with pytest.raises(ValueError):
        await read_article(client, law_id, article)

    assert router.calls.call_count == 0


@pytest.mark.parametrize("arguments", [{"max_chars": 499}, {"max_chars": 20001}, {"offset": -1}])
async def test_read_article_rejects_out_of_range_paging_without_a_request(
    client: BoeClient, router: respx.MockRouter, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await read_article(client, LEY_40, "48", **arguments)

    assert router.calls.call_count == 0


# --- read_article: paging ----------------------------------------------------------------------


@pytest.fixture
def long_blocks(
    law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> dict[str, tuple[str, str, str]]:
    """Two long real blocks, mocked: (law id, article wording, expected block id) by name."""
    irpf = law_text(IRPF, index_body(("a68", "Artículo 68", "20261007")))
    irpf.fixture_block("a68", "texto_bloque_a68_irpf_many_versions_trimmed.xml")
    iva = law_text(IVA, load_fixture("texto_indice_iva_1992_trimmed.json"))
    iva.fixture_block("an", "texto_bloque_anexo_iva_1992_thead_noDesde_trimmed.xml")
    return {"irpf": (IRPF, "artículo 68", "a68"), "iva_annex": (IVA, "anexo", "an")}


@pytest.mark.parametrize("name", ["irpf", "iva_annex"])
@pytest.mark.parametrize("max_chars", [500, 777, 3000, 6000, 20000])
async def test_read_article_pages_reproduce_the_full_text_exactly(
    client: BoeClient,
    long_blocks: dict[str, tuple[str, str, str]],
    name: str,
    max_chars: int,
) -> None:
    law_id, article, _ = long_blocks[name]
    first = await read_article(client, law_id, article, today=TODAY, max_chars=20000)
    pages: list[str] = []
    offset: int | None = 0

    while offset is not None:
        page = await read_article(
            client, law_id, article, today=TODAY, max_chars=max_chars, offset=offset
        )
        assert len(page.text) <= max_chars
        assert page.total_chars == first.total_chars
        assert page.truncated is (page.next_offset is not None)
        if page.next_offset is not None:
            assert page.next_offset == offset + len(page.text)  # no gap, no overlap
            assert page.note is not None
            assert str(page.next_offset) in page.note
        pages.append(page.text)
        offset = page.next_offset

    assert "".join(pages) == first.text
    assert len(first.text) > 6000
    assert (len(pages) > 1) is (len(first.text) > max_chars)


async def test_read_article_cuts_at_a_paragraph_boundary_when_one_is_inside_the_window(
    client: BoeClient, long_blocks: dict[str, tuple[str, str, str]]
) -> None:
    law_id, article, _ = long_blocks["irpf"]

    page = await read_article(client, law_id, article, today=TODAY, max_chars=3000)

    assert page.truncated
    assert page.text.endswith("\n\n")
    assert not page.text.endswith("\n\n\n")


async def test_read_article_cuts_at_the_limit_when_no_paragraph_boundary_is_inside(
    client: BoeClient, law_text: Callable[[str, bytes], LawText]
) -> None:
    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20200101")))
    long_paragraph = "palabra " * 400
    text.block("a1", block_xml("a1", "Artículo 1", ("X", "20200101", "20200101", [long_paragraph])))

    page = await read_article(client, LEY_40, "1", today=TODAY, max_chars=500)

    assert len(page.text) == 500
    assert page.next_offset == 500
    assert page.truncated


async def test_read_article_notes_come_only_with_the_first_page(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LOPD, load_fixture("texto_indice_lopd1999_repealed_with_no.json"))
    text.fixture_block("a40", "texto_bloque_a40_repealed_article_DERIVED.xml")

    first = await read_article(client, LOPD, "40", today=TODAY, max_chars=500)
    second = await read_article(client, LOPD, "40", today=TODAY, max_chars=500, offset=10)

    assert first.notes
    assert second.notes == []


async def test_read_article_offset_at_the_end_is_empty_and_beyond_it_is_an_error(
    client: BoeClient, ley_40: LawText
) -> None:
    whole = await read_article(client, LEY_40, "46 bis", today=TODAY)

    at_end = await read_article(client, LEY_40, "46 bis", today=TODAY, offset=whole.total_chars)

    assert (at_end.text, at_end.truncated, at_end.next_offset) == ("", False, None)
    with pytest.raises(ValueError, match="offset"):
        await read_article(client, LEY_40, "46 bis", today=TODAY, offset=whole.total_chars + 1)


# --- compare_article_versions ---------------------------------------------------------------------


async def test_compare_two_versions_reports_the_changes_and_the_amending_law(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "48", date(2017, 1, 1), date(2022, 1, 1), today=TODAY, now=NOW
    )

    assert result.outcome == "found"
    assert (result.from_date, result.to_date) == (date(2017, 1, 1), date(2022, 1, 1))
    assert result.from_version is not None and result.to_version is not None
    assert (result.from_version.index, result.to_version.index) == (1, 2)
    assert result.identical is False
    assert (result.added, result.removed, result.changed, result.unchanged) == (0, 0, 1, 11)
    assert [change.op for change in result.changes] == ["changed"]
    assert result.changes[0].inline is not None
    assert "{+" in result.changes[0].inline
    assert result.intermediate_versions == 0
    assert result.amending_laws == ["BOE-A-2020-17340"]
    assert result.unified_diff.startswith("--- old")
    assert (result.changes_truncated, result.unified_truncated) == (False, False)
    assert ley_40.requested == ["indice", "a48"]


async def test_compare_without_to_date_compares_against_today(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "48", date(2017, 1, 1), today=TODAY, now=NOW
    )

    assert result.to_date == TODAY
    assert result.to_version is not None
    assert result.to_version.index == 2


async def test_compare_two_dates_in_the_same_version_is_identical_and_says_so(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "48", date(2021, 2, 1), date(2025, 1, 1), today=TODAY, now=NOW
    )

    assert result.identical is True
    assert result.note is not None
    assert "same version" in result.note
    assert (result.added, result.removed, result.changed) == (0, 0, 0)
    assert result.unchanged == 12
    assert (result.changes, result.unified_diff, result.amending_laws) == ([], "", [])
    assert result.intermediate_versions == 0


async def test_compare_an_article_added_later_shows_everything_as_added(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "46 bis", date(2018, 1, 1), today=TODAY, now=NOW
    )

    assert result.from_version is None
    assert result.to_version is not None
    assert result.identical is False
    assert (result.added, result.removed, result.changed, result.unchanged) == (3, 0, 0, 0)
    assert [change.op for change in result.changes] == ["added"] * 3
    assert result.amending_laws == ["BOE-A-2019-15790"]
    assert result.note is not None
    assert "did not exist" in result.note


async def test_compare_across_several_versions_counts_the_ones_in_between(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))
    text.fixture_block("dt", "texto_bloque_disposicion_multiversion_rd1624_dt.xml")

    result = await compare_article_versions(
        client,
        RD_1624,
        "disposición transitoria cuarta",
        date(2018, 1, 1),
        date(2026, 2, 10),
        today=TODAY,
        now=NOW,
    )

    assert result.from_version is not None and result.to_version is not None
    assert (result.from_version.index, result.to_version.index) == (1, 5)
    assert result.intermediate_versions == 3
    assert result.amending_laws == [
        "BOE-A-2019-13563",
        "BOE-A-2025-26458",
        "BOE-A-2026-2024",
        "BOE-A-2026-2547",
    ]


async def test_compare_uses_the_start_date_of_a_version_with_no_effective_date(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))
    text.fixture_block("dt", "texto_bloque_disposicion_multiversion_rd1624_dt.xml")

    result = await compare_article_versions(
        client,
        RD_1624,
        None,
        date(2026, 2, 10),
        date(2026, 3, 1),
        block_id="dt",
        today=TODAY,
        now=NOW,
    )

    # The last version has no fecha_vigencia: it starts on its publication date, 2026-02-28.
    assert result.to_version is not None
    assert result.to_version.index == 6
    assert result.to_version.effective_from is None
    assert result.to_version.published_on == date(2026, 2, 28)
    assert result.to_version.starts_on == date(2026, 2, 28)


@pytest.fixture
def irpf(law_text: Callable[[str, bytes], LawText], load_fixture: Loader) -> LawText:
    text = law_text(IRPF, index_body(("a68", "Artículo 68", "20261007")))
    text.block("a68", load_fixture("texto_bloque_a68_irpf_many_versions_trimmed.xml"))
    return text


async def test_compare_caps_the_changes_and_says_how_many_exist(
    client: BoeClient, irpf: LawText
) -> None:
    # The size budget is lifted to its maximum so that only the count limit acts (the default
    # budget cuts this article's changes earlier: see the budget tests below).
    result = await compare_article_versions(
        client,
        IRPF,
        "68",
        date(2010, 1, 1),
        date(2026, 11, 1),
        max_chars=30000,
        today=TODAY,
        now=NOW,
    )

    assert (result.added, result.removed, result.changed) == (19, 24, 26)
    assert len(result.changes) == 40
    assert result.changes_truncated is True
    assert all(change.op != "unchanged" for change in result.changes)


async def test_compare_max_changes_is_honoured_and_a_large_value_returns_all(
    client: BoeClient, irpf: LawText
) -> None:
    few = await compare_article_versions(
        client, IRPF, "68", date(2010, 1, 1), date(2026, 11, 1), max_changes=5, now=NOW
    )
    every = await compare_article_versions(
        client,
        IRPF,
        "68",
        date(2010, 1, 1),
        date(2026, 11, 1),
        max_changes=200,
        max_chars=30000,
        now=NOW,
    )

    assert (len(few.changes), few.changes_truncated) == (5, True)
    assert (len(every.changes), every.changes_truncated) == (69, False)


async def test_compare_cuts_the_unified_diff_at_6000_characters(
    client: BoeClient, irpf: LawText
) -> None:
    result = await compare_article_versions(
        client, IRPF, "68", date(2010, 1, 1), date(2026, 11, 1), now=NOW
    )

    assert len(result.unified_diff) == 6000
    assert result.unified_truncated is True


async def test_compare_with_no_version_on_either_date_has_nothing_to_compare(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "46 bis", date(2010, 1, 1), date(2015, 1, 1), now=NOW
    )

    assert (result.from_version, result.to_version) == (None, None)
    assert result.identical is None
    assert result.changes == []
    assert result.note is not None
    assert "did not exist" in result.note


async def test_compare_with_an_ambiguous_article_returns_candidates(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))

    result = await compare_article_versions(
        client, RD_1624, "Artículo 1", date(2017, 1, 1), now=NOW
    )

    assert result.outcome == "ambiguous"
    assert [c.block_id for c in result.candidates] == ["a1", "a1-2"]
    assert (result.identical, result.changes, result.from_version) == (None, [], None)


@pytest.mark.parametrize(
    "arguments",
    [
        {"from_date": date(2022, 1, 2), "to_date": date(2022, 1, 1)},
        {"from_date": date(2017, 1, 1), "max_changes": 0},
        {"from_date": date(2017, 1, 1), "max_changes": 201},
        {"from_date": date(2027, 1, 1)},  # after today, the default end date
    ],
)
async def test_compare_rejects_invalid_arguments_without_a_request(
    client: BoeClient, router: respx.MockRouter, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await compare_article_versions(client, LEY_40, "48", today=TODAY, now=NOW, **arguments)

    assert router.calls.call_count == 0


# --- get_article_history -------------------------------------------------------------------------


async def test_history_lists_the_versions_with_their_kind_and_source(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))
    text.fixture_block("dt", "texto_bloque_disposicion_multiversion_rd1624_dt.xml")

    result = await get_article_history(client, RD_1624, block_id="dt", today=TODAY, now=NOW)

    assert result.outcome == "found"
    assert result.version_count == 6
    assert [e.kind for e in result.events] == [
        "original",
        "repealed",
        "amended",
        "repealed",
        "amended",
        "repealed",
    ]
    assert [e.index for e in result.events] == [1, 2, 3, 4, 5, 6]
    assert result.events[0].introduced_by == "BOE-A-2017-5857"
    assert result.events[0].effective_from == date(2017, 5, 28)
    assert result.events[0].published_on == date(2017, 5, 27)
    assert (result.events[0].added, result.events[0].removed, result.events[0].changed) == (0, 0, 0)
    assert result.events[-1].effective_from is None
    assert result.current_state == "repealed"
    assert result.truncated is False
    assert text.requested == ["indice", "dt"]


async def test_history_of_a_repealed_article_ends_with_a_repeal_event(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LOPD, load_fixture("texto_indice_lopd1999_repealed_with_no.json"))
    text.fixture_block("a40", "texto_bloque_a40_repealed_article_DERIVED.xml")

    result = await get_article_history(client, LOPD, "40", today=TODAY, now=NOW)

    assert [e.kind for e in result.events] == ["original", "repealed"]
    assert result.current_state == "repealed"


async def test_history_current_state_follows_today(client: BoeClient, ley_40: LawText) -> None:
    before = await get_article_history(client, LEY_40, "46 bis", today=date(2019, 1, 1), now=NOW)
    after = await get_article_history(client, LEY_40, "46 bis", today=TODAY, now=NOW)

    assert (before.current_state, after.current_state) == ("did_not_exist", "in_force")


async def test_history_keeps_the_60_most_recent_versions_and_says_so(
    client: BoeClient, law_text: Callable[[str, bytes], LawText]
) -> None:
    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20260101")))
    versions = [
        (f"BOE-A-2000-{n}", f"{2000 + n}0101", f"{2000 + n}0101", [f"Texto {n}."])
        for n in range(1, 66)
    ]
    text.block("a1", block_xml("a1", "Artículo 1", *versions))

    result = await get_article_history(client, LEY_40, "1", today=TODAY, now=NOW)

    assert result.version_count == 65
    assert len(result.events) == 60
    assert result.truncated is True
    assert result.events[0].index == 6
    assert result.events[-1].index == 65


async def test_history_with_nothing_found_returns_candidates(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await get_article_history(client, LEY_40, "artículo 9999", today=TODAY, now=NOW)

    assert result.outcome == "not_found"
    assert result.events == []
    assert result.current_state is None
    assert result.candidates


# === Review findings ==============================================================

LEY_30 = "BOE-A-1992-26318"
LEY_30_2007 = "BOE-A-2007-18874"


@pytest.fixture
def ley_30(law_text: Callable[[str, bytes], LawText], load_fixture: Loader) -> LawText:
    """Ley 30/1992, repealed as a whole: article 1 carries no repeal marker."""
    text = law_text(LEY_30, load_fixture("texto_indice_ley30_1992_repealed_trimmed.json"))
    text.fixture_block("a1", "texto_bloque_a1_repealed_law_no_marker.xml")
    text.metadata_fixture("metadatos_repealed_ley30_1992.json")
    text.initial_note("texto_bloque_no_ley30_1992_two_versions_empty_vigencia.xml")
    return text


@pytest.fixture
def lopd(law_text: Callable[[str, bytes], LawText], load_fixture: Loader) -> LawText:
    """LO 15/1999: the metadata flag is N; only the initial note says it is repealed."""
    text = law_text(LOPD, load_fixture("texto_indice_lopd1999_repealed_with_no.json"))
    text.fixture_block("a40", "texto_bloque_a40_repealed_article_DERIVED.xml")
    text.fixture_block("a1", "texto_bloque_a1_repealed_law_no_marker.xml")
    text.metadata_fixture("metadatos_superseded_partial_lopd1999.json")
    text.initial_note("texto_bloque_no_lopd1999_repealed.xml")
    return text


# --- whole-law repeal: read_article ---------------------------------------------------------------


async def test_read_article_of_a_wholly_repealed_law_is_repealed_though_the_block_has_no_marker(
    client: BoeClient, ley_30: LawText
) -> None:
    result = await read_article(client, LEY_30, "1", today=TODAY, now=NOW)

    assert result.state == "repealed"
    assert result.law_repealed is True
    # The note says 2016-10-02 and the metadata 2021-04-02 (end of the transitional regime):
    # the earlier one is when the law was repealed.
    assert result.law_repealed_on == date(2016, 10, 2)
    assert result.text.startswith("Artículo 1. Objeto de la Ley.")
    assert result.note is not None
    assert "whole law was repealed on 2016-10-02" in result.note


async def test_read_article_before_the_repeal_date_is_decided_by_the_block_and_mentions_it(
    client: BoeClient, ley_30: LawText
) -> None:
    result = await read_article(client, LEY_30, "1", as_of=date(2015, 1, 1), now=NOW)

    assert result.state == "in_force"
    assert result.law_repealed is True
    assert result.note is not None
    assert "repealed later" in result.note
    assert "2016-10-02" in result.note


@pytest.mark.parametrize(
    ("as_of", "state"),
    [
        (date(2016, 10, 1), "in_force"),
        (date(2016, 10, 2), "repealed"),
        (date(2030, 1, 1), "repealed"),
    ],
)
async def test_read_article_switches_to_repealed_on_the_repeal_date(
    client: BoeClient, ley_30: LawText, as_of: date, state: str
) -> None:
    result = await read_article(client, LEY_30, "1", as_of=as_of, now=NOW)

    assert result.state == state


async def test_read_article_of_a_law_repealed_only_by_its_note_is_repealed(
    client: BoeClient, lopd: LawText
) -> None:
    result = await read_article(client, LOPD, block_id="a1", today=TODAY, now=NOW)

    assert (result.state, result.law_repealed, result.law_repealed_on) == (
        "repealed",
        True,
        date(2018, 12, 7),
    )


async def test_read_article_of_a_repealed_law_without_a_known_date_follows_as_of(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20200101")))
    text.fixture_block("a1", "texto_bloque_a1_repealed_law_no_marker.xml")
    flagged = load_fixture("metadatos_repealed_ley30_2007_no_initial_note.json")
    text.metadata.mock(
        return_value=httpx.Response(200, content=flagged.replace(b"20111216", b"")),
    )

    today = await read_article(client, LEY_40, "1", today=TODAY, now=NOW)
    earlier = await read_article(client, LEY_40, "1", as_of=date(2000, 1, 1), now=NOW)

    assert (today.state, today.law_repealed, today.law_repealed_on) == ("repealed", True, None)
    assert earlier.state == "in_force"
    assert earlier.law_repealed is True
    assert earlier.note is not None
    assert "repealed" in earlier.note


async def test_read_article_of_a_law_flagged_repealed_without_a_note_is_repealed(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LEY_30_2007, index_body(("a1", "Artículo 1", "20200101")))
    text.fixture_block("a1", "texto_bloque_a1_repealed_law_no_marker.xml")
    text.metadata_fixture("metadatos_repealed_ley30_2007_no_initial_note.json")

    result = await read_article(client, LEY_30_2007, "1", today=TODAY, now=NOW)

    assert (result.state, result.law_repealed_on) == ("repealed", date(2011, 12, 16))
    assert "no" not in text.requested_all  # the index lists no initial note: no extra request


async def test_read_article_of_a_law_in_force_has_law_repealed_false(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "48", today=TODAY, now=NOW)

    assert (result.state, result.law_repealed, result.law_repealed_on) == ("in_force", False, None)
    assert result.note is None


async def test_read_article_of_a_future_dated_repeal_is_not_repealed_yet(
    client: BoeClient, ley_40: LawText, load_fixture: Loader
) -> None:
    flagged = load_fixture("metadatos_repealed_ley30_1992.json").replace(b"20210402", b"20271231")
    ley_40.metadata.mock(return_value=httpx.Response(200, content=flagged))

    result = await read_article(client, LEY_40, "48", today=TODAY, now=NOW)

    assert result.state == "in_force"
    assert (result.law_repealed, result.law_repealed_on) == (False, date(2027, 12, 31))
    assert result.note is not None
    assert "announced" in result.note
    assert "2027-12-31" in result.note


async def test_a_law_without_a_listed_initial_note_costs_one_extra_request(
    client: BoeClient, ley_40: LawText
) -> None:
    await read_article(client, LEY_40, "48", today=TODAY, now=NOW)

    assert sorted(ley_40.requested_all) == ["a48", "indice", "metadatos"]


async def test_a_law_whose_index_lists_the_initial_note_also_fetches_it(
    client: BoeClient, ley_30: LawText
) -> None:
    await read_article(client, LEY_30, "1", today=TODAY, now=NOW)

    assert sorted(ley_30.requested_all) == ["a1", "indice", "metadatos", "no"]


async def test_the_repeal_checks_run_concurrently_with_the_block_fetch(
    client: BoeClient, ley_30: LawText
) -> None:
    metadata_asked = asyncio.Event()
    block_response = httpx.Response(
        200,
        content=ley_30.fixture_bytes("texto_bloque_a1_repealed_law_no_marker.xml"),
        headers={"Content-Type": "application/xml"},
    )
    metadata_response = httpx.Response(
        200, content=ley_30.fixture_bytes("metadatos_repealed_ley30_1992.json")
    )

    def metadata(request: httpx.Request) -> httpx.Response:
        metadata_asked.set()
        return metadata_response

    async def block(request: httpx.Request) -> httpx.Response:
        # Answers only once the metadata was asked for: a sequential implementation would wait
        # here for a request it has not sent yet and time out.
        await asyncio.wait_for(metadata_asked.wait(), timeout=2)
        return block_response

    ley_30.metadata.side_effect = metadata
    ley_30.blocks["a1"].side_effect = block

    result = await read_article(client, LEY_30, "1", today=TODAY, now=NOW)

    assert result.state == "repealed"


# --- whole-law repeal: degradation ---------------------------------------------------------------


@pytest.fixture
def strict_client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    """A client that does not retry, so a failing endpoint fails at once."""
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


async def test_a_failing_metadata_request_degrades_the_result_and_says_so(
    strict_client: BoeClient, ley_40: LawText
) -> None:
    ley_40.metadata.mock(return_value=httpx.Response(503))

    result = await read_article(strict_client, LEY_40, "48", today=TODAY, now=NOW)

    assert result.outcome == "found"
    assert result.state == "in_force"
    assert result.law_repealed is None
    assert result.text
    assert result.note is not None
    assert "metadata could not be fetched" in result.note


async def test_a_failing_note_request_degrades_when_nothing_else_says_repealed(
    strict_client: BoeClient, lopd: LawText
) -> None:
    lopd.note.mock(return_value=httpx.Response(503))

    result = await read_article(strict_client, LOPD, block_id="a1", today=TODAY, now=NOW)

    assert (result.state, result.law_repealed) == ("in_force", None)
    assert result.note is not None
    assert "initial note could not be fetched" in result.note


async def test_a_failing_note_request_does_not_hide_a_repeal_the_metadata_flags(
    strict_client: BoeClient, ley_30: LawText
) -> None:
    ley_30.note.mock(return_value=httpx.Response(503))

    result = await read_article(strict_client, LEY_30, "1", today=TODAY, now=NOW)

    assert (result.state, result.law_repealed, result.law_repealed_on) == (
        "repealed",
        True,
        date(2021, 4, 2),
    )


async def test_an_unknown_law_in_the_metadata_still_fails_the_tool(
    strict_client: BoeClient, ley_40: LawText, xml_response: XmlResponse, load_fixture: Loader
) -> None:
    ley_40.metadata.mock(return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404))

    with pytest.raises(BoeNotFoundError):
        await read_article(strict_client, LEY_40, "48", today=TODAY, now=NOW)


async def test_a_failing_block_request_fails_the_tool_with_its_own_error(
    strict_client: BoeClient, ley_40: LawText
) -> None:
    ley_40.blocks["a48"].mock(return_value=httpx.Response(503))

    with pytest.raises(BoeUnavailableError):
        await read_article(strict_client, LEY_40, "48", today=TODAY, now=NOW)


# --- whole-law repeal: history and comparison ---------------------------------------------------


async def test_history_current_state_is_repealed_for_a_wholly_repealed_law(
    client: BoeClient, ley_30: LawText
) -> None:
    result = await get_article_history(client, LEY_30, "1", today=TODAY, now=NOW)

    assert result.current_state == "repealed"
    assert (result.law_repealed, result.law_repealed_on) == (True, date(2016, 10, 2))
    assert result.events[0].kind == "original"  # the block itself shows no repeal
    assert result.note is not None
    assert "whole law was repealed" in result.note


async def test_history_current_state_before_the_repeal_date_is_the_blocks(
    client: BoeClient, ley_30: LawText
) -> None:
    result = await get_article_history(client, LEY_30, "1", today=date(2015, 1, 1), now=NOW)

    assert result.current_state == "in_force"


async def test_compare_mentions_a_repeal_on_or_before_to_date(
    client: BoeClient, ley_30: LawText
) -> None:
    after = await compare_article_versions(
        client, LEY_30, "1", date(2000, 1, 1), date(2020, 1, 1), now=NOW
    )
    exactly = await compare_article_versions(
        client, LEY_30, "1", date(2000, 1, 1), date(2016, 10, 2), now=NOW
    )
    before = await compare_article_versions(
        client, LEY_30, "1", date(2000, 1, 1), date(2016, 10, 1), now=NOW
    )

    assert after.note is not None and "2016-10-02" in after.note
    assert exactly.note is not None and "2016-10-02" in exactly.note
    assert before.note is not None and "repealed" not in before.note
    assert (after.law_repealed, after.law_repealed_on) == (True, date(2016, 10, 2))


# --- block_id and the selector ------------------------------------------------------------------


async def test_block_id_selects_the_block_exactly(client: BoeClient, ley_40: LawText) -> None:
    result = await read_article(client, LEY_40, block_id="a4-2", today=TODAY, now=NOW)

    assert (result.outcome, result.block_id) == ("found", "a4-2")


async def test_block_id_works_for_a_designation_the_locator_would_not_resolve(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))
    text.fixture_block("dt", "texto_bloque_disposicion_multiversion_rd1624_dt.xml")

    by_wording = await read_article(client, RD_1624, "dt", today=TODAY, now=NOW)
    by_id = await read_article(client, RD_1624, block_id="dt", today=TODAY, now=NOW)

    assert by_wording.outcome == "not_found"
    assert by_wording.hint is not None and "block_id" in by_wording.hint
    assert (by_id.outcome, by_id.block_id) == ("found", "dt")


async def test_an_unknown_block_id_is_not_found_with_candidates(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, block_id="zzz", today=TODAY, now=NOW)

    assert result.outcome == "not_found"
    assert result.candidates
    assert result.hint is not None and "block_id" in result.hint
    assert ley_40.requested == ["indice"]


async def test_the_ambiguous_hint_tells_the_model_to_use_block_id(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    law_text(RD_1624, load_fixture("texto_indice_rd1624_1992_dup_articulo1.json"))

    result = await read_article(client, RD_1624, "Artículo 1", today=TODAY, now=NOW)

    assert result.hint is not None
    assert "block_id" in result.hint


@pytest.mark.parametrize(
    ("article", "block_id"),
    [(None, None), ("48", "a48"), ("", None), (None, ""), (None, "  ")],
)
async def test_exactly_one_of_article_and_block_id_is_required(
    client: BoeClient, router: respx.MockRouter, article: str | None, block_id: str | None
) -> None:
    with pytest.raises(ValueError):
        await read_article(client, LEY_40, article, block_id=block_id)
    with pytest.raises(ValueError):
        await get_article_history(client, LEY_40, article, block_id=block_id)
    with pytest.raises(ValueError):
        await compare_article_versions(client, LEY_40, article, date(2017, 1, 1), block_id=block_id)

    assert router.calls.call_count == 0


async def test_an_article_of_more_than_200_characters_is_rejected_without_a_request(
    client: BoeClient, router: respx.MockRouter
) -> None:
    with pytest.raises(ValueError, match="200"):
        await read_article(client, LEY_40, "artículo 1 " + "x" * 200)

    assert router.calls.call_count == 0


async def test_an_article_of_exactly_200_characters_is_accepted(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "48" + " " * 198, today=TODAY, now=NOW)

    assert result.block_id == "a48"


async def test_trailing_paragraph_numbers_are_ignored_and_the_note_says_so(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await read_article(client, LEY_40, "artículo 48.2", today=TODAY, now=NOW)

    assert (result.outcome, result.block_id) == ("found", "a48")
    assert result.note is not None
    assert "Located Artículo 48" in result.note
    assert "('.2')" in result.note
    assert "ignored" in result.note
    assert "whole articles" in result.note


@pytest.mark.parametrize("tool", ["history", "compare"])
async def test_the_ignored_remainder_is_reported_by_every_article_tool(
    client: BoeClient, ley_40: LawText, tool: str
) -> None:
    if tool == "history":
        result = await get_article_history(client, LEY_40, "48.2", today=TODAY, now=NOW)
    else:
        result = await compare_article_versions(
            client, LEY_40, "48.2", date(2017, 1, 1), date(2022, 1, 1), now=NOW
        )

    assert result.note is not None and "('.2')" in result.note


async def test_a_plain_article_has_no_ignored_note(client: BoeClient, ley_40: LawText) -> None:
    result = await read_article(client, LEY_40, "artículo 48", today=TODAY, now=NOW)

    assert result.note is None


async def test_the_data_date_of_an_ambiguous_result_is_the_newest_index_date(
    client: BoeClient, law_text: Callable[[str, bytes], LawText]
) -> None:
    law_text(
        LEY_40,
        index_body(("a1", "Artículo 1", "20200101"), ("a1-2", "Artículo 1", "20240301")),
    )

    result = await read_article(client, LEY_40, "1", today=TODAY, now=NOW)

    assert result.outcome == "ambiguous"
    assert result.provenance.data_updated_at == datetime(2024, 3, 1, tzinfo=UTC)


# --- notes --------------------------------------------------------------------------------------


async def test_footnotes_are_capped_at_2000_characters_in_all(
    client: BoeClient, law_text: Callable[[str, bytes], LawText]
) -> None:
    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20200101")))
    footnotes = "".join(f'<p class="nota_pie">{"n" * 700}{i}</p>' for i in range(5))
    xml = block_xml("a1", "Artículo 1", ("X", "20200101", "20200101", ["Texto."])).replace(
        b"</version>", footnotes.encode() + b"</version>"
    )
    text.block("a1", xml)

    result = await read_article(client, LEY_40, "1", today=TODAY, now=NOW)

    assert sum(len(n) for n in result.notes) <= 2000
    assert result.notes_truncated is True
    assert result.notes[-1].endswith("…[truncated]")


async def test_short_footnotes_are_not_flagged_as_truncated(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], load_fixture: Loader
) -> None:
    text = law_text(LOPD, load_fixture("texto_indice_lopd1999_repealed_with_no.json"))
    text.fixture_block("a40", "texto_bloque_a40_repealed_article_DERIVED.xml")

    result = await read_article(client, LOPD, "40", today=TODAY, now=NOW)

    assert result.notes
    assert result.notes_truncated is False


# --- compare: wording of the absence, budget ------------------------------------------------------


async def test_compare_says_not_yet_in_force_between_publication_and_the_effective_date(
    client: BoeClient, ley_40: LawText
) -> None:
    # Article 46 bis was published on 2019-11-05 and took effect on 2019-11-06.
    result = await compare_article_versions(
        client, LEY_40, "46 bis", date(2019, 11, 5), date(2020, 1, 1), now=NOW
    )

    assert result.from_version is None
    assert result.note is not None
    assert "not yet in force" in result.note
    assert "2019-11-06" in result.note
    assert "did not exist" not in result.note


async def test_compare_says_the_article_did_not_exist_before_its_publication(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "46 bis", date(2019, 11, 4), date(2020, 1, 1), now=NOW
    )

    assert result.note is not None
    assert "did not exist" in result.note


async def test_compare_with_both_dates_before_the_effective_date_says_not_yet_in_force(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "46 bis", date(2019, 11, 5), date(2019, 11, 5), now=NOW
    )

    assert result.note is not None
    assert "not yet in force" in result.note
    assert "nothing to compare" in result.note


@pytest.fixture
def iva_annex(law_text: Callable[[str, bytes], LawText], load_fixture: Loader) -> LawText:
    text = law_text(IVA, load_fixture("texto_indice_iva_1992_trimmed.json"))
    text.fixture_block("an", "texto_bloque_anexo_iva_1992_thead_noDesde_trimmed.xml")
    return text


# What the default budgets allow: 8,000 characters of changes, 6,000 of unified diff, and the
# JSON syntax and field names around them (17 KB measured, UTF-8). The trimmed IVA annex used to
# give 26 KB, and the real one would give several hundred.
_DEFAULT_COMPARE_BOUND = 20_000


async def test_compare_of_the_iva_annex_stays_within_a_stated_size(
    client: BoeClient, iva_annex: LawText
) -> None:
    result = await compare_article_versions(
        client, IVA, "anexo", date(1993, 1, 1), date(2026, 11, 1), now=NOW
    )

    size = len(result.model_dump_json().encode())
    assert size < _DEFAULT_COMPARE_BOUND, size
    assert result.changes_truncated is True
    text_chars = sum(
        len(t) for c in result.changes for t in (c.old_text, c.new_text, c.inline) if t
    )
    assert text_chars <= 8000


@pytest.mark.parametrize("max_chars", [1000, 4000, 30000])
async def test_compare_changes_never_exceed_max_chars(
    client: BoeClient, iva_annex: LawText, max_chars: int
) -> None:
    result = await compare_article_versions(
        client, IVA, "anexo", date(1993, 1, 1), date(2026, 11, 1), max_chars=max_chars, now=NOW
    )

    text_chars = sum(
        len(t) for c in result.changes for t in (c.old_text, c.new_text, c.inline) if t
    )
    assert text_chars <= max_chars
    assert result.changes


async def test_a_reworded_paragraph_is_returned_as_inline_only(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(
        client, LEY_40, "48", date(2017, 1, 1), date(2022, 1, 1), now=NOW
    )

    (change,) = result.changes
    assert change.op == "changed"
    assert change.inline is not None
    assert (change.old_text, change.new_text) == (None, None)


async def test_added_and_removed_paragraphs_keep_their_text(
    client: BoeClient, ley_40: LawText
) -> None:
    result = await compare_article_versions(client, LEY_40, "46 bis", date(2018, 1, 1), now=NOW)

    assert all(c.op == "added" and c.new_text and c.inline is None for c in result.changes)


async def test_a_changed_table_paragraph_returns_both_texts_with_their_line_breaks(
    client: BoeClient, law_text: Callable[[str, bytes], LawText]
) -> None:
    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20200101")))
    table = (
        "<table><tbody><tr><td><p>Uno</p></td><td><p>Dos</p></td></tr>"
        "<tr><td><p>Tres</p></td><td><p>{}</p></td></tr></tbody></table>"
    )
    xml = (
        '<response><data><bloque id="a1" tipo="precepto" titulo="Artículo 1">'
        f'<version id_norma="A" fecha_publicacion="20100101" fecha_vigencia="20100101">'
        f'<p class="parrafo">Cabecera.</p>{table.format("Cuatro")}</version>'
        f'<version id_norma="B" fecha_publicacion="20200101" fecha_vigencia="20200101">'
        f'<p class="parrafo">Cabecera.</p>{table.format("Cinco")}</version>'
        "</bloque></data></response>"
    ).encode()
    text.block("a1", xml)

    result = await compare_article_versions(
        client, LEY_40, "1", date(2015, 1, 1), date(2021, 1, 1), now=NOW
    )

    (change,) = result.changes
    assert change.inline is None
    assert change.old_text == "Uno | Dos\nTres | Cuatro"
    assert change.new_text == "Uno | Dos\nTres | Cinco"


async def test_an_oversized_single_change_is_cut_with_a_marker_not_dropped(
    client: BoeClient, law_text: Callable[[str, bytes], LawText]
) -> None:
    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20200101")))
    text.block(
        "a1",
        block_xml(
            "a1",
            "Artículo 1",
            ("A", "20100101", "20100101", ["Cabecera."]),
            ("B", "20200101", "20200101", ["Cabecera.", "palabra " * 5000]),
        ),
    )

    result = await compare_article_versions(
        client, LEY_40, "1", date(2015, 1, 1), date(2021, 1, 1), max_chars=1000, now=NOW
    )

    (change,) = result.changes
    assert change.op == "added"
    assert change.new_text is not None
    assert change.new_text.endswith("…[truncated]")
    assert len(change.new_text) <= 1000
    assert result.changes_truncated is True


async def test_compare_rejects_a_max_chars_out_of_range_without_a_request(
    client: BoeClient, router: respx.MockRouter
) -> None:
    for value in (999, 30001):
        with pytest.raises(ValueError, match="max_chars"):
            await compare_article_versions(client, LEY_40, "48", date(2017, 1, 1), max_chars=value)

    assert router.calls.call_count == 0


# --- performance and the event loop ------------------------------------------------


async def test_a_big_diff_runs_off_the_event_loop(
    client: BoeClient, law_text: Callable[[str, bytes], LawText], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    text = law_text(LEY_40, index_body(("a1", "Artículo 1", "20200101")))
    text.block(
        "a1",
        block_xml(
            "a1",
            "Artículo 1",
            ("A", "20100101", "20100101", ["Uno."]),
            ("B", "20200101", "20200101", ["Uno.", "Dos."]),
        ),
    )
    threads: list[int] = []
    from boe_insights_mcp import articles as articles_module

    real_diff, real_history = articles_module.diff_versions, articles_module.history

    def spy_diff(*args: Any) -> Any:
        threads.append(threading.get_ident())
        return real_diff(*args)

    def spy_history(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return real_history(*args, **kwargs)

    monkeypatch.setattr(articles_module, "diff_versions", spy_diff)
    monkeypatch.setattr(articles_module, "history", spy_history)

    await compare_article_versions(client, LEY_40, "1", date(2015, 1, 1), date(2021, 1, 1), now=NOW)
    await get_article_history(client, LEY_40, "1", now=NOW)

    assert len(threads) == 2
    assert threading.get_ident() not in threads
