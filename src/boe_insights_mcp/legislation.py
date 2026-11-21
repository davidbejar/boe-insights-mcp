"""Use cases over the BOE consolidated legislation: search, reference resolution, overview.

These functions take a :class:`BoeClient` and plain arguments and return pydantic models; they
know nothing of MCP, so they can be tested against a mocked HTTP layer.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Literal

from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.models import LawAnalysis, LawMetadata, LawReference, LawSummary
from boe_insights_mcp.query import SortField, build_search_query
from boe_insights_mcp.references import ParsedReference, parse_reference
from boe_insights_mcp.schemas import (
    AmendmentSummary,
    LawBrief,
    LawOverview,
    LawStatus,
    Provenance,
    ReferenceBrief,
    RelationCount,
    ResolveReferenceResult,
    SearchLegislationResult,
)

__all__ = [
    "RankFilter",
    "ScopeFilter",
    "derive_status",
    "get_law_overview",
    "resolve_reference",
    "search_legislation",
]

RankFilter = Literal[
    "constitucion",
    "ley_organica",
    "ley",
    "real_decreto_ley",
    "real_decreto_legislativo",
    "real_decreto",
    "orden",
    "resolucion",
    "circular",
    "instruccion",
]
ScopeFilter = Literal["state", "regional", "any"]

_RANK_CODES: dict[RankFilter, str] = {
    "constitucion": "1070",
    "ley_organica": "1290",
    "ley": "1300",
    "real_decreto_ley": "1320",
    "real_decreto_legislativo": "1310",
    "real_decreto": "1340",
    "orden": "1350",
    "resolucion": "1370",
    "circular": "1390",
    "instruccion": "1410",
}
_SCOPE_CODES: dict[ScopeFilter, str | None] = {"state": "1", "regional": "2", "any": None}
_STATE_SCOPE_CODE = "1"

MAX_LIMIT = 50
_MAX_CANDIDATES = 10
_SAMPLE_SIZE = 10
_TITLE_SEARCH_LIMIT = 10

_FORMATS_HINT = (
    "Accepted formats: a BOE identifier (BOE-A-2015-10566), a rank and official number "
    "(Ley 40/2015, Real Decreto-ley 6/2019, LO 3/2018) or just a number (40/2015). "
    "Years need four digits."
)


def _utc(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _brief(law: LawSummary) -> LawBrief:
    return LawBrief(
        identifier=law.identifier,
        title=law.title,
        rank=law.rank.text if law.rank else None,
        official_number=law.official_number,
        department=law.department.text if law.department else None,
        scope=law.scope.text if law.scope else None,
        published_on=law.published_on,
        validity_exhausted=law.validity_exhausted,
        url=law.html_url,
    )


def _newest_update(laws: Iterable[LawSummary]) -> datetime | None:
    stamps = [law.updated_at for law in laws if law.updated_at is not None]
    return max(stamps, default=None)


def derive_status(metadata: LawMetadata, today: date) -> tuple[LawStatus, str]:
    """Return the legal status of a law and a plain-English note explaining it.

    Precedence: annulled, repealed, validity exhausted, not yet in force, in force.
    """
    if metadata.annulled:
        return "annulled", "The BOE flags this law as annulled (declared void)."
    if metadata.repealed:
        when = f" on {metadata.repealed_on.isoformat()}" if metadata.repealed_on else ""
        return "repealed", f"The BOE flags this law as repealed{when}."
    if metadata.validity_exhausted:
        return (
            "validity_exhausted",
            "The effects of this law are spent: it has fulfilled its purpose or expired "
            "(for example a budget law for a past year), though it was never repealed.",
        )
    if metadata.effective_from is not None and metadata.effective_from > today:
        return (
            "not_yet_in_force",
            f"This law takes effect on {metadata.effective_from.isoformat()}, which is in the "
            "future.",
        )
    return (
        "in_force",
        "The BOE does not flag this law as repealed. The flag covers only whole-law repeals, "
        "so individual provisions may have been amended or repealed: check the amendment "
        "summary.",
    )


async def search_legislation(
    client: BoeClient,
    *,
    title: str | None = None,
    text: str | None = None,
    rank: RankFilter | None = None,
    department_code: str | None = None,
    scope: ScopeFilter = "any",
    subject_code: str | None = None,
    published_from: date | None = None,
    published_to: date | None = None,
    sort_by: SortField = "published_on",
    descending: bool = True,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
) -> SearchLegislationResult:
    """Search consolidated legislation and return one page of compact records.

    One extra record is requested to detect a further page, because the API gives no total.
    The API's rank filter is approximate (a Ley Foral can match "Ley"), so hits whose rank
    code differs from the requested one are dropped. Raises ``ValueError`` for invalid
    arguments.
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    rank_code = _RANK_CODES[rank] if rank is not None else None
    query = build_search_query(
        title=title,
        text=text,
        rank_code=rank_code,
        department_code=department_code,
        scope_code=_SCOPE_CODES[scope],
        subject_code=subject_code,
        published_from=published_from,
        published_to=published_to,
        sort_by=sort_by,
        descending=descending,
    )
    fetched = await client.search_laws(query, offset=offset, limit=limit + 1)
    kept, has_more, consumed = _take_page(fetched, rank_code, limit, len(fetched) == limit + 1)
    return SearchLegislationResult(
        items=[_brief(law) for law in kept],
        returned=len(kept),
        offset=offset,
        has_more=has_more,
        next_offset=offset + consumed if has_more else None,
        provenance=Provenance(retrieved_at=_utc(now), data_updated_at=_newest_update(kept)),
    )


def _take_page(
    fetched: list[LawSummary], rank_code: str | None, limit: int, upstream_full: bool
) -> tuple[list[LawSummary], bool, int]:
    """Keep up to ``limit`` hits of the requested rank; return them, ``has_more`` and the
    number of upstream records consumed (the step to the next page's offset).

    If the page ends without an extra kept hit but the upstream page was full, the dropped
    hits hide how many remain, so ``has_more`` is reported true: the next page may be empty.
    """
    kept: list[LawSummary] = []
    for consumed, law in enumerate(fetched):
        if rank_code is not None and (law.rank is None or law.rank.code != rank_code):
            continue
        if len(kept) == limit:
            return kept, True, consumed
        kept.append(law)
    return kept, upstream_full, len(fetched)


async def resolve_reference(
    client: BoeClient, reference: str, *, now: datetime | None = None
) -> ResolveReferenceResult:
    """Resolve a human reference (``Ley 40/2015``, an identifier, or a title) to laws.

    Raises ``ValueError`` if ``reference`` is blank.
    """
    text = reference.strip()
    if not text:
        raise ValueError("reference must not be empty")
    provenance_time = _utc(now)
    parsed = parse_reference(text)
    if parsed is None:
        return await _resolve_by_title(client, reference, text, provenance_time)
    if parsed.identifier is not None:
        return await _resolve_identifier(client, reference, parsed.identifier, provenance_time)
    return await _resolve_numbered(client, reference, parsed, provenance_time)


def _resolution(
    reference: str,
    interpreted_as: str | None,
    laws: Sequence[LawSummary],
    *,
    hint: str | None,
    now: datetime,
) -> ResolveReferenceResult:
    """Build a result: one law is resolved, several ambiguous, none not found."""
    if len(laws) == 1:
        status: Literal["resolved", "ambiguous", "not_found"] = "resolved"
    else:
        status = "ambiguous" if laws else "not_found"
    return ResolveReferenceResult(
        reference=reference,
        status=status,
        interpreted_as=interpreted_as,
        matches=[_brief(law) for law in laws[:_MAX_CANDIDATES]],
        hint=None if status == "resolved" else hint,
        provenance=Provenance(retrieved_at=now, data_updated_at=_newest_update(laws)),
    )


async def _resolve_identifier(
    client: BoeClient, reference: str, identifier: str, now: datetime
) -> ResolveReferenceResult:
    try:
        laws = [await client.get_law_metadata(identifier)]
    except BoeNotFoundError:
        laws = []
    return _resolution(
        reference,
        f"BOE identifier {identifier}",
        laws,
        hint="No law has this identifier in the consolidated legislation. Check it, or "
        "search by rank and number (Ley 40/2015) or by title with search_legislation.",
        now=now,
    )


async def _resolve_numbered(
    client: BoeClient, reference: str, parsed: ParsedReference, now: datetime
) -> ResolveReferenceResult:
    """Look for an exact official number, first among state laws, then in any scope."""
    description = (
        f"{parsed.rank_name} {parsed.official_number}"
        if parsed.rank_name
        else f"official number {parsed.official_number} (rank not given)"
    )
    for scope_code, label in ((_STATE_SCOPE_CODE, "state"), (None, "any scope")):
        laws = await _exact_number_matches(client, parsed, scope_code)
        if laws:
            return _resolution(
                reference,
                f"{description} ({label})",
                laws,
                hint="Several laws share this number. Repeat the reference with its rank "
                "(Ley, Real Decreto, ...) or pick one of the matches.",
                now=now,
            )
    return _resolution(
        reference,
        f"{description} (state, then any scope)",
        [],
        hint="No consolidated law has this number. Check the rank and year, or search by "
        "title with search_legislation.",
        now=now,
    )


async def _exact_number_matches(
    client: BoeClient, parsed: ParsedReference, scope_code: str | None
) -> list[LawSummary]:
    """Search by official number and keep only hits with that exact number (and rank)."""
    query = build_search_query(
        official_number=parsed.official_number,
        rank_code=parsed.rank_code,
        scope_code=scope_code,
        sort_by="published_on",
    )
    hits = await client.search_laws(query, limit=MAX_LIMIT)
    return [
        law
        for law in hits
        if law.official_number == parsed.official_number
        and (
            parsed.rank_code is None or (law.rank is not None and law.rank.code == parsed.rank_code)
        )
    ]


async def _resolve_by_title(
    client: BoeClient, reference: str, text: str, now: datetime
) -> ResolveReferenceResult:
    """Fall back to a title search for text with no identifier or official number."""
    page = await search_legislation(client, title=text, limit=_TITLE_SEARCH_LIMIT, now=now)
    if not page.items:
        return ResolveReferenceResult(
            reference=reference,
            status="not_found",
            interpreted_as=None,
            matches=[],
            hint=f"No law title matches this text. {_FORMATS_HINT}",
            provenance=page.provenance,
        )
    return ResolveReferenceResult(
        reference=reference,
        status="ambiguous",
        interpreted_as=None,
        matches=page.items,
        hint="The text has no identifier or official number, so these are title matches. "
        f"Pick one, or use an exact format. {_FORMATS_HINT}",
        provenance=page.provenance,
    )


def _relation_counts(references: list[LawReference]) -> list[RelationCount]:
    counts = Counter(reference.relation.text for reference in references)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [RelationCount(relation=relation, count=count) for relation, count in ordered]


def _samples(references: list[LawReference]) -> list[ReferenceBrief]:
    return [
        ReferenceBrief(law_id=ref.law_id, relation=ref.relation.text, text=ref.text)
        for ref in references[:_SAMPLE_SIZE]
    ]


def _amendment_summary(analysis: LawAnalysis) -> AmendmentSummary:
    return AmendmentSummary(
        affected_by_count=len(analysis.later),
        affects_count=len(analysis.earlier),
        affected_by_relations=_relation_counts(analysis.later),
        affects_relations=_relation_counts(analysis.earlier),
        affected_by_sample=_samples(analysis.later),
        affects_sample=_samples(analysis.earlier),
    )


async def get_law_overview(
    client: BoeClient,
    law_id: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> LawOverview:
    """Return a law's identity, derived legal status and amendment summary.

    Raises ``ValueError`` for a malformed identifier and ``BoeNotFoundError`` for an unknown one.
    """
    metadata, analysis = await asyncio.gather(
        client.get_law_metadata(law_id), client.get_law_analysis(law_id)
    )
    moment = _utc(now)
    status, note = derive_status(metadata, today if today is not None else moment.date())
    brief = _brief(metadata)
    return LawOverview(
        identifier=metadata.identifier,
        title=metadata.title,
        rank=brief.rank,
        official_number=metadata.official_number,
        department=brief.department,
        scope=brief.scope,
        enacted_on=metadata.enacted_on,
        published_on=metadata.published_on,
        effective_from=metadata.effective_from,
        status=status,
        status_note=note,
        repealed_on=metadata.repealed_on,
        consolidation_state=(
            metadata.consolidation_state.text if metadata.consolidation_state else None
        ),
        amendments=_amendment_summary(analysis),
        url=metadata.html_url,
        eli_url=metadata.eli_url,
        provenance=Provenance(retrieved_at=moment, data_updated_at=metadata.updated_at),
    )
