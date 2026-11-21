"""Tests for the ``get_law_structure`` use case of ``boe_insights_mcp.articles``.

The BOE API is mocked with respx and answers from the recorded ``texto_full_*`` and metadata
fixtures. The pure rules of the outline itself are covered by ``test_structure``; here the subject
is what the use case adds: the two requests, paging, the ``detail`` filter, the counts, the notes
and the error handling.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from boe_insights_mcp.articles import (
    DEFAULT_STRUCTURE_LIMIT,
    MAX_STRUCTURE_LIMIT,
    _structure_notes,
    _uniform_state,
    get_law_structure,
)
from boe_insights_mcp.cache import MemoryCache
from boe_insights_mcp.client import BASE_URL, BoeClient
from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.ratelimit import RateLimiter
from boe_insights_mcp.repeal import LawRepeal
from boe_insights_mcp.schemas import LawStructureResult
from boe_insights_mcp.structure import StructureEntry
from boe_insights_mcp.versions import ArticleState

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 21)

LEY_40 = "BOE-A-2015-10566"
ASTURIAS = "BOE-A-2026-23272"
LOPD = "BOE-A-1999-23750"
CODIGO_CIVIL = "BOE-A-1889-4763"

Loader = Callable[[str], bytes]
JsonResponse = Callable[..., httpx.Response]
XmlResponse = Callable[..., httpx.Response]


@pytest.fixture
def client(router: respx.MockRouter, http: httpx.AsyncClient) -> BoeClient:
    return BoeClient(
        cache=MemoryCache(), limiter=RateLimiter(min_interval=0), http=http, max_retries=0
    )


class Law:
    """Mocks the two endpoints the use case reads for one law and records the requests."""

    def __init__(
        self,
        router: respx.MockRouter,
        json_response: JsonResponse,
        xml_response: XmlResponse,
        load_fixture: Loader,
        law_id: str,
        *,
        metadata: str,
        text: str | bytes,
    ) -> None:
        prefix = f"{BASE_URL}legislacion-consolidada/id/{law_id}"
        body = load_fixture(text) if isinstance(text, str) else text
        self.metadata = router.get(f"{prefix}/metadatos").mock(
            return_value=json_response(load_fixture(metadata))
        )
        self.text = router.get(f"{prefix}/texto").mock(return_value=xml_response(body, 200))
        self.router = router

    @property
    def paths(self) -> list[str]:
        """The last path segment of every request made, in order."""
        return sorted(call.request.url.path.rsplit("/", 1)[-1] for call in self.router.calls)


@pytest.fixture
def law(
    router: respx.MockRouter,
    json_response: JsonResponse,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> Callable[..., Law]:
    """Factory: ``law(law_id, metadata=..., text=...)`` with fixture names or raw text bytes."""

    def _make(law_id: str, *, metadata: str, text: str | bytes) -> Law:
        return Law(
            router, json_response, xml_response, load_fixture, law_id, metadata=metadata, text=text
        )

    return _make


async def structure(client: BoeClient, law_id: str, **kwargs: object) -> LawStructureResult:
    """Call the use case with a fixed clock."""
    return await get_law_structure(client, law_id, today=TODAY, now=NOW, **kwargs)  # type: ignore[arg-type]


LEY40 = {
    "metadata": "metadatos_in_force_ley40_2015.json",
    "text": "texto_full_ley40_2015_trimmed.xml",
}
ASTURIAS_FILES = {
    "metadata": "metadatos_asturias_ley8_2026.json",
    "text": "texto_full_asturias_ley8_2026_not_yet_in_force_trimmed.xml",
}
CIVIL = {
    "metadata": "metadatos_in_force_ley40_2015.json",
    "text": "texto_full_codigo_civil_trimmed.xml",
}
LOPD_FILES = {
    "metadata": "metadatos_superseded_partial_lopd1999.json",
    "text": "texto_full_lopd1999_trimmed.xml",
}


def synthetic_law(articles: int) -> bytes:
    """A whole-law XML with ``articles`` articles spread over titles of 100 articles each."""
    blocks: list[str] = []
    version = 'id_norma="BOE-A-2000-1" fecha_publicacion="20000101" fecha_vigencia="20000102"'
    for number in range(articles):
        if number % 100 == 0:
            blocks.append(
                f'<bloque id="t{number}" tipo="encabezado"><version {version}>'
                f'<p class="titulo_num">TÍTULO {number // 100 + 1}</p>'
                f'<p class="titulo_tit">Título número {number // 100 + 1}</p></version></bloque>'
            )
        blocks.append(
            f'<bloque id="a{number}" tipo="precepto" titulo="Artículo {number}"><version {version}>'
            f'<p class="articulo">Artículo {number}. Asunto {number}.</p></version></bloque>'
        )
    return (
        "<response><status><code>200</code></status><data><texto>"
        + "".join(blocks)
        + "</texto></data></response>"
    ).encode()


# --- the answer ----------------------------------------------------------------------------------


async def test_ley40_default_answer(client: BoeClient, law: Callable[..., Law]) -> None:
    mocks = law(LEY_40, **LEY40)

    result = await structure(client, LEY_40)

    assert (result.law_id, result.as_of) == (LEY_40, TODAY)
    assert result.title.startswith("Ley 40/2015, de 1 de octubre")
    assert [e.block_id for e in result.entries][:5] == ["tpreliminar", "ci", "a1", "a2", "cii"]
    first = result.entries[0]
    assert (first.kind, first.level, first.label) == ("title", 1, "TÍTULO PRELIMINAR")
    assert (result.returned, result.total_entries) == (len(result.entries), 22)
    assert (result.has_more, result.next_offset, result.note) == (False, None, None)
    # Two requests only: the initial note comes from the full text, no extra request is needed.
    assert mocks.paths == ["metadatos", "texto"]


async def test_counts_cover_the_whole_structure_by_kind(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    result = await structure(client, LEY_40)

    assert result.counts == {
        "title": 2,
        "chapter": 4,
        "section": 3,
        "subsection": 2,
        "article": 9,
        "provision": 2,
    }
    assert sum(result.counts.values()) == result.total_entries


async def test_provenance_is_the_usual_one_with_the_metadata_update_stamp(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    result = await structure(client, LEY_40)

    assert result.provenance.attribution.startswith("Basado en datos de la Agencia Estatal")
    assert result.provenance.source_url == "https://www.boe.es"
    assert "no official legal value" in result.provenance.notice
    assert result.provenance.generated_at == NOW
    assert result.provenance.data_updated_at == datetime(2026, 11, 20, 8, 50, tzinfo=UTC)


async def test_in_force_entries_carry_no_state_in_the_serialised_output(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    result = await structure(client, LEY_40)

    assert all("state" not in entry for entry in result.model_dump(mode="json")["entries"])


async def test_identifier_is_normalised(client: BoeClient, law: Callable[..., Law]) -> None:
    law(LEY_40, **LEY40)

    result = await structure(client, f" {LEY_40.lower()} ")

    assert result.law_id == LEY_40


# --- dates ---------------------------------------------------------------------------------------


async def test_as_of_in_the_past_reads_the_text_of_that_day(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(CODIGO_CIVIL, **CIVIL)

    old = await structure(client, CODIGO_CIVIL, as_of=date(1900, 1, 1))
    law_today = await structure(client, CODIGO_CIVIL)

    assert old.as_of == date(1900, 1, 1)
    by_id = {e.block_id: e for e in old.entries}
    assert by_id["tpreliminar"].heading.startswith("De las Leyes, de sus efectos")
    assert "ci" not in by_id  # only has a 1974 version
    assert "sprimera" in by_id  # not yet expired in 1900
    assert "sprimera" not in {e.block_id for e in law_today.entries}
    assert old.total_entries != law_today.total_entries


async def test_law_not_yet_in_force_says_so_once_instead_of_marking_every_entry(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(ASTURIAS, **ASTURIAS_FILES)

    result = await structure(client, ASTURIAS)

    assert result.total_entries == 25
    assert {e.state for e in result.entries} == {None}
    assert result.note is not None
    assert "not yet in force" in result.note
    assert "Every entry is not yet in force" in result.note
    assert "2026-11-23" in result.note
    dumped = result.model_dump(mode="json")["entries"]
    assert all("state" not in entry for entry in dumped)


def entry(state: ArticleState) -> StructureEntry:
    return StructureEntry("a1", "article", 1, "Artículo 1", "", state)


IN_FORCE_LAW = LawRepeal(repealed=False, on=None, announced=False, unchecked=())


def test_entries_that_all_share_a_state_are_described_once_in_the_note() -> None:
    notes = _structure_notes(
        [entry("repealed"), entry("repealed")],
        IN_FORCE_LAW,
        date(2020, 1, 1),
        as_of_given=True,
        effective_from=None,
    )

    assert notes == ["Every entry is repealed on 2020-01-01."]


def test_uniform_state_is_found_only_when_all_entries_agree_and_none_is_in_force() -> None:
    assert _uniform_state([entry("repealed"), entry("repealed")]) == "repealed"
    assert _uniform_state([entry("repealed"), entry("in_force")]) is None
    assert _uniform_state([entry("in_force"), entry("in_force")]) is None
    assert _uniform_state([]) is None


async def test_the_same_law_on_its_effective_date_has_no_note_and_no_states(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(ASTURIAS, **ASTURIAS_FILES)

    result = await structure(client, ASTURIAS, as_of=date(2026, 11, 23))

    assert {e.state for e in result.entries} == {None}
    assert result.note is None


async def test_before_the_law_was_published_the_structure_is_empty_with_a_note(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(ASTURIAS, **ASTURIAS_FILES)

    result = await structure(client, ASTURIAS, as_of=date(2020, 1, 1))

    assert (result.entries, result.total_entries, result.returned) == ([], 0, 0)
    assert (result.has_more, result.next_offset, result.counts) == (False, None, {})
    assert result.note is not None
    assert "2020-01-01" in result.note


# --- whole-law repeal ----------------------------------------------------------------------------


async def test_wholly_repealed_law_gets_the_repeal_note(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    # The LOPD 1999 text opens with "Norma derogada, con efectos de 7 de diciembre de 2018".
    law(LOPD, **LOPD_FILES)

    result = await structure(client, LOPD)

    assert result.note is not None
    assert "repealed" in result.note
    assert "2018-12-07" in result.note
    # The entries themselves still describe the last consolidated text: only a block with its
    # own repeal marker (article 40 of the trimmed text) shows a state.
    assert [e.block_id for e in result.entries if e.state] == ["a40"]


async def test_the_initial_note_is_found_by_its_kind_whatever_its_id(
    client: BoeClient, law: Callable[..., Law], load_fixture: Loader
) -> None:
    # Ley 40/2015 calls its initial note block ``co`` instead of ``no`` (api-notes, "Text
    # structure"), so the repeal note must not depend on the id.
    text = load_fixture(LOPD_FILES["text"]).replace(b'id="no"', b'id="co"')
    law(LOPD, metadata=LOPD_FILES["metadata"], text=text)

    result = await structure(client, LOPD)

    assert result.note is not None
    assert "2018-12-07" in result.note


async def test_repeal_that_came_after_the_requested_date_is_reported_as_later(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LOPD, **LOPD_FILES)

    result = await structure(client, LOPD, as_of=date(2010, 1, 1))

    assert result.note is not None
    assert "repealed later" in result.note
    assert "2018-12-07" in result.note


async def test_failed_metadata_degrades_to_a_note_and_the_id_as_title(
    client: BoeClient, router: respx.MockRouter, law: Callable[..., Law], xml_response: XmlResponse
) -> None:
    mocks = law(LEY_40, **LEY40)
    mocks.metadata.mock(return_value=xml_response(b"<response/>", 500))

    result = await structure(client, LEY_40)

    assert result.title == LEY_40
    assert result.note is not None
    assert "metadata could not be fetched" in result.note
    assert result.provenance.data_updated_at is None
    assert result.total_entries == 22  # the outline itself is intact


# --- paging and detail ---------------------------------------------------------------------------


async def test_pages_follow_each_other_and_add_up_to_the_whole_structure(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)
    whole = await structure(client, LEY_40)

    pages = [await structure(client, LEY_40, limit=8, offset=offset) for offset in (0, 8, 16)]

    assert [p.returned for p in pages] == [8, 8, 6]
    assert [(p.has_more, p.next_offset) for p in pages] == [(True, 8), (True, 16), (False, None)]
    assert all(p.total_entries == 22 for p in pages)
    assert [e for p in pages for e in p.entries] == whole.entries
    assert all(p.counts == whole.counts for p in pages)  # counts are about the whole law


async def test_an_offset_equal_to_the_total_is_an_empty_last_page(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    page = await structure(client, LEY_40, offset=22)

    assert (page.entries, page.returned, page.has_more, page.next_offset) == ([], 0, False, None)


async def test_an_offset_beyond_the_end_is_an_error(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    with pytest.raises(ValueError, match="offset"):
        await structure(client, LEY_40, offset=23)


async def test_headings_detail_drops_articles_and_provisions_but_not_the_counts(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    headings = await structure(client, LEY_40, detail="headings")
    everything = await structure(client, LEY_40)

    assert {e.kind for e in headings.entries} == {"title", "chapter", "section", "subsection"}
    assert headings.total_entries == 11
    assert headings.returned == 11
    assert headings.counts == everything.counts  # still says how many articles there are
    # Levels are those of the full outline, not recomputed without the articles.
    levels = {e.block_id: e.level for e in everything.entries}
    assert all(levels[e.block_id] == e.level for e in headings.entries)


async def test_headings_detail_keeps_the_preamble(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(ASTURIAS, **ASTURIAS_FILES)

    result = await structure(client, ASTURIAS, detail="headings")

    assert result.entries[0].kind == "preamble"
    assert "article" not in {e.kind for e in result.entries}
    assert "provision" not in {e.kind for e in result.entries}


async def test_headings_paging_counts_only_headings(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, **LEY40)

    page = await structure(client, LEY_40, detail="headings", limit=4)

    assert (page.returned, page.total_entries, page.has_more, page.next_offset) == (4, 11, True, 4)


async def test_a_large_code_is_served_one_bounded_page_at_a_time(
    client: BoeClient, law: Callable[..., Law]
) -> None:
    law(LEY_40, metadata=LEY40["metadata"], text=synthetic_law(2400))

    started = time.perf_counter()
    first = await structure(client, LEY_40, limit=MAX_STRUCTURE_LIMIT)
    last = await structure(client, LEY_40, limit=MAX_STRUCTURE_LIMIT, offset=2024)
    elapsed = time.perf_counter() - started

    assert first.total_entries == 2400 + 24
    assert (first.returned, first.has_more, first.next_offset) == (400, True, 400)
    assert last.returned == 400 and last.has_more is False
    assert first.counts == {"title": 24, "article": 2400}
    # Both pages together include parsing 2 400 blocks twice; a quadratic build would be slow.
    assert elapsed < 5.0


async def test_default_and_maximum_page_sizes() -> None:
    assert (DEFAULT_STRUCTURE_LIMIT, MAX_STRUCTURE_LIMIT) == (200, 400)


# --- errors --------------------------------------------------------------------------------------


async def test_unknown_law_is_not_found(
    client: BoeClient,
    router: respx.MockRouter,
    xml_response: XmlResponse,
    load_fixture: Loader,
) -> None:
    for endpoint in ("metadatos", "texto"):
        router.get(f"{BASE_URL}legislacion-consolidada/id/BOE-A-2000-1/{endpoint}").mock(
            return_value=xml_response(load_fixture("error_404_unknown_id.xml"), 404)
        )

    with pytest.raises(BoeNotFoundError):
        await structure(client, "BOE-A-2000-1")


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": MAX_STRUCTURE_LIMIT + 1},
        {"offset": -1},
        {"detail": "everything"},
    ],
)
async def test_bad_arguments_raise_value_error_before_any_request(
    client: BoeClient, router: respx.MockRouter, arguments: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        await structure(client, LEY_40, **arguments)
    assert router.calls.call_count == 0


async def test_malformed_law_id_raises_value_error_before_any_request(
    client: BoeClient, router: respx.MockRouter
) -> None:
    with pytest.raises(ValueError):
        await structure(client, "../etc/passwd")
    assert router.calls.call_count == 0
