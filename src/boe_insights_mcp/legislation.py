"""Use cases over the BOE consolidated legislation: search, reference resolution, overview.

These functions take a :class:`BoeClient` and plain arguments and return pydantic models; they
know nothing of MCP, so they can be tested against a mocked HTTP layer.

Public use cases: ``search_legislation``, ``resolve_reference``, ``get_law_overview`` and
``find_codes``, plus ``derive_status``. ``server`` calls them (one per tool); they call
``client`` for data, ``query`` and ``references`` for pure logic, and build the ``schemas``
results, each stamped with a ``Provenance``. The ``now`` and ``today`` arguments exist so tests
can fix the clock.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from boe_insights_mcp.client import AuxiliaryTable, BoeClient
from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.models import LawAnalysis, LawMetadata, LawReference, LawSummary
from boe_insights_mcp.query import SortField, build_search_query
from boe_insights_mcp.references import ParsedReference, fold, normalise_identifier, parse_reference
from boe_insights_mcp.schemas import (
    AmendmentSummary,
    CodeMatch,
    FindCodesResult,
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
    "CodeKind",
    "RankFilter",
    "ScopeFilter",
    "derive_status",
    "find_codes",
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
CodeKind = Literal["department", "subject", "rank"]

_CODE_TABLES: dict[CodeKind, AuxiliaryTable] = {
    "department": "departamentos",
    "subject": "materias",
    "rank": "rangos",
}

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


# Calendar days of Spanish legislation are Madrid days (see get_law_overview).
_MADRID = ZoneInfo("Europe/Madrid")


def _utc(now: datetime | None) -> datetime:
    """Return ``now``, or the current UTC time when it is ``None`` (the injectable clock)."""
    return now if now is not None else datetime.now(UTC)


def _brief(law: LawSummary) -> LawBrief:
    """Reduce a summary or metadata record to the compact tool-facing ``LawBrief``."""
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
    """Latest ``updated_at`` among ``laws``, or ``None``; becomes ``Provenance.data_updated_at``."""
    stamps = [law.updated_at for law in laws if law.updated_at is not None]
    return max(stamps, default=None)


def derive_status(
    metadata: LawMetadata, today: date, repeal_references: int
) -> tuple[LawStatus, str]:
    """Return the legal status of a law and a plain-English note explaining it.

    ``repeal_references`` is the number of later norms whose relation is a repeal
    (:func:`_repeal_reference_count`). Precedence: annulled, repealed, validity exhausted, not
    yet in force, partially repealed, in force. The BOE's repeal flag marks only whole-law
    repeals, so a law that later norms repeal in part (or in full, without the flag) is
    reported as ``partially_repealed`` rather than ``in_force``.
    """
    # Order encodes how strong each signal is. The two BOE flags are authoritative, so they come
    # first and an annulled or repealed law is never reported as "not yet in force". Validity
    # exhausted (a spent law such as a past budget) comes next. ``fecha_vigencia`` is when the
    # law *enters* into force, never an end date (api-notes 4), so it only ever yields "not yet".
    # "partially_repealed" is a heuristic read from the amendment references, so it is checked
    # last: it must not mask a stronger flag, and it downgrades only a law that would otherwise
    # be shown as plainly in force (the repeal flag covers whole-law repeals only, api-notes 4).
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
    if repeal_references > 0:
        plural = "reference" if repeal_references == 1 else "references"
        return (
            "partially_repealed",
            f"The BOE analysis has {repeal_references} {plural} in which later norms repeal this "
            "law or parts of it. The BOE flag only marks whole repeals and is not set here, so "
            "the law may be repealed in full or in part: check the amendment summary.",
        )
    return (
        "in_force",
        "The BOE does not flag this law as repealed and no later norm is recorded as "
        "repealing it. Individual provisions may still have been amended: check the "
        "amendment summary.",
    )


def _repeal_reference_count(analysis: LawAnalysis) -> int:
    """Count the later references whose relation is a repeal (BOE codes 210-217, "SE DEROGA...")."""
    # The relation is identified by its text, not its code: ``posteriores`` use the passive
    # wording ("SE DEROGA", "SE DEROGA tácitamente"...), api-notes 5. ``casefold`` keeps this
    # independent of letter case.
    return sum(
        1
        for reference in analysis.later
        if reference.relation.text.casefold().startswith("se deroga")
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
    # The ``rank`` literal is translated to a BOE code here and sent to the API as a filter, then
    # checked again on the hits in ``_take_page``, because the API's filter is approximate
    # (api-notes 2, anomaly with Ley Foral 1450).
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
    # Ask for one record more than the page: if it comes back, a next page exists. The API has no
    # total count (api-notes 1), so this is the cheapest "is there more" test.
    fetched = await client.search_laws(query, offset=offset, limit=limit + 1)
    kept, has_more, consumed = _take_page(fetched, rank_code, limit, len(fetched) == limit + 1)
    return SearchLegislationResult(
        items=[_brief(law) for law in kept],
        returned=len(kept),
        offset=offset,
        has_more=has_more,
        # ``consumed`` counts upstream records, including any dropped by the rank filter, so
        # the next page resumes exactly after the last record this page looked at.
        next_offset=offset + consumed if has_more else None,
        provenance=Provenance(generated_at=_utc(now), data_updated_at=_newest_update(kept)),
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
        # Drop hits of another rank (the API filter lets a few through, api-notes 2). They still
        # count as consumed upstream records, so the offset arithmetic stays correct.
        if rank_code is not None and (law.rank is None or law.rank.code != rank_code):
            continue
        if len(kept) == limit:
            # A genuine extra hit exists. ``consumed`` is its index, so the next page starts at
            # it and no record is skipped or repeated.
            return kept, True, consumed
        kept.append(law)
    # Fell off the end without finding an extra kept hit. If the upstream page was full there
    # may be more records beyond it (filtered ones leave us unable to tell), so say "maybe".
    return kept, upstream_full, len(fetched)


async def resolve_reference(
    client: BoeClient, reference: str, *, now: datetime | None = None
) -> ResolveReferenceResult:
    """Resolve a human reference (``Ley 40/2015``, an identifier, or a title) to laws.

    Raises ``ValueError`` if ``reference`` is blank.
    """
    # Three routes: an identifier is looked up directly, a rank/number is matched exactly, and
    # anything else falls back to a title search (``parse_reference`` returns ``None`` then).
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
    resolvable: bool = True,
) -> ResolveReferenceResult:
    """Build a result: resolved (one sure law), ambiguous (several or an unsure one), not found.

    ``resolvable`` is false when a lone match is not certain enough to call resolved. The hint is
    only attached to results that are not resolved, and at most ``_MAX_CANDIDATES`` matches show.
    """
    if len(laws) == 1 and resolvable:
        status: Literal["resolved", "ambiguous", "not_found"] = "resolved"
    else:
        status = "ambiguous" if laws else "not_found"
    return ResolveReferenceResult(
        reference=reference,
        status=status,
        interpreted_as=interpreted_as,
        matches=[_brief(law) for law in laws[:_MAX_CANDIDATES]],
        hint=None if status == "resolved" else hint,
        provenance=Provenance(generated_at=now, data_updated_at=_newest_update(laws)),
    )


async def find_codes(
    client: BoeClient,
    kind: CodeKind,
    text: str,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> FindCodesResult:
    """Find the codes of departments, subjects or ranks whose name contains all words of ``text``.

    Matching ignores case and accents. The shortest names come first, since they are the
    closest matches. Raises ``ValueError`` for an unknown ``kind``, a blank ``text`` or a
    ``limit`` outside ``1..MAX_LIMIT``.
    """
    if kind not in _CODE_TABLES:
        raise ValueError(f"kind must be one of {', '.join(_CODE_TABLES)}")
    words = fold(text).split()
    if not words:
        raise ValueError("text must not be empty")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    table = await client.get_auxiliary_table(_CODE_TABLES[kind])
    # Sort key: name length first (a shorter name is usually a closer match); folded name and code
    # then make the order deterministic.
    found: list[tuple[int, str, str, str]] = []
    for code, name in table.items():
        folded = fold(name)
        if all(word in folded for word in words):
            found.append((len(name), folded, code, name))
    found.sort()
    shown = [CodeMatch(code=code, text=name) for _, _, code, name in found[:limit]]
    return FindCodesResult(
        kind=kind,
        query=text,
        matches=shown,
        returned=len(shown),
        total_matches=len(found),
        provenance=Provenance(generated_at=_utc(now)),
    )


async def _resolve_identifier(
    client: BoeClient, reference: str, identifier: str, now: datetime
) -> ResolveReferenceResult:
    """Look an identifier up directly; an unknown id is a ``not_found`` result, not an error."""
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


def _scope_plan(parsed: ParsedReference) -> tuple[tuple[str | None, str], ...]:
    """Decide which scopes to search, in order, as ``(ambito code, label)`` pairs.

    Regional laws are numbered independently of state laws, so a state-scope hit for a number
    is the wrong answer when the text may refer to a regional law (a regional rank, a rank word
    we do not know, or a region name). Then only the any-scope search makes sense. Otherwise
    state laws come first, with any scope as the fallback.
    """
    # State first because ``ambito@codigo:1`` is what removes the regional laws that reuse
    # numbers (api-notes 3). The fallback covers state laws filed under another scope code (a
    # regional law republished in the BOE has a ``BOE-A-`` id but ``ambito`` 2, api-notes 0).
    if parsed.may_be_regional:
        return ((None, "any scope"),)
    return ((_STATE_SCOPE_CODE, "state"), (None, "any scope"))


def _may_resolve(parsed: ParsedReference, laws: Sequence[LawSummary]) -> bool:
    """Whether a lone match can be reported as resolved rather than as a candidate.

    An unknown rank word means the rank could not be checked, and a region name contradicts a
    state-scope match, so in neither case is a single hit certain.
    """
    # If several laws matched there is nothing to resolve to. A region name only contradicts a
    # hit that is state scope; a lone regional hit agrees with it.
    if len(laws) != 1 or parsed.unrecognised_rank_word is not None:
        return False
    scope = laws[0].scope
    return parsed.region is None or scope is None or scope.code != _STATE_SCOPE_CODE


def _ambiguous_hint(parsed: ParsedReference) -> str:
    """Pick the advice for an unresolved numbered reference, from what the text revealed."""
    if parsed.may_be_regional:
        unknown = (
            f" The rank word {parsed.unrecognised_rank_word!r} is not recognised."
            if parsed.unrecognised_rank_word
            else ""
        )
        return (
            "This reference may be a regional law, and regional laws share numbers with state "
            f"laws and with each other.{unknown} Choose among the matches by title, department "
            "or scope, or give the BOE identifier."
        )
    if parsed.rank_name:
        return (
            "Several laws share this rank and number: regional laws are numbered independently "
            "of state laws and share numbers. Choose among the matches by title or department, "
            "or give the BOE identifier."
        )
    return (
        "Several laws share this number. Repeat the reference with its rank "
        "(Ley, Real Decreto, ...) or pick one of the matches."
    )


async def _resolve_numbered(
    client: BoeClient, reference: str, parsed: ParsedReference, now: datetime
) -> ResolveReferenceResult:
    """Look for an exact official number, in the scopes chosen by :func:`_scope_plan`."""
    description = (
        f"{parsed.rank_name} {parsed.official_number}"
        if parsed.rank_name
        else f"official number {parsed.official_number} (rank not given)"
    )
    plan = _scope_plan(parsed)
    for scope_code, label in plan:
        laws = await _exact_number_matches(client, parsed, scope_code)
        # The first scope that finds anything decides; later scopes are only fallbacks.
        if laws:
            return _resolution(
                reference,
                f"{description} ({label})",
                laws,
                hint=_ambiguous_hint(parsed),
                now=now,
                resolvable=_may_resolve(parsed, laws),
            )
    return _resolution(
        reference,
        f"{description} ({', then '.join(label for _, label in plan)})",
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
    # The API matches the number as a loose phrase (api-notes 2), so check the number again, and
    # the rank, since the rank filter is approximate. Only the first MAX_LIMIT hits are looked at.
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
    # Oldest first: the original law comes before the later laws that amend it and cite its name.
    page = await search_legislation(
        client, title=text, descending=False, limit=_TITLE_SEARCH_LIMIT, now=now
    )
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
    """Count references per relation text, most frequent first, ties in alphabetical order."""
    counts = Counter(reference.relation.text for reference in references)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [RelationCount(relation=relation, count=count) for relation, count in ordered]


def _samples(references: list[LawReference]) -> list[ReferenceBrief]:
    """The first ``_SAMPLE_SIZE`` references in the API's order, as compact records."""
    return [
        ReferenceBrief(law_id=ref.law_id, relation=ref.relation.text, text=ref.text)
        for ref in references[:_SAMPLE_SIZE]
    ]


def _amendment_summary(analysis: LawAnalysis) -> AmendmentSummary:
    """Summarise an analysis: "affected by" is ``later`` references, "affects" is ``earlier``."""
    return AmendmentSummary(
        affected_by_count=len(analysis.later),
        affects_count=len(analysis.earlier),
        affected_by_relations=_relation_counts(analysis.later),
        affects_relations=_relation_counts(analysis.earlier),
        affected_by_sample=_samples(analysis.later),
        affects_sample=_samples(analysis.earlier),
    )


async def _fetch_both(client: BoeClient, law_id: str) -> tuple[LawMetadata, LawAnalysis]:
    """Fetch metadata and analysis together; if one fails the other is cancelled.

    The original error (not an ``ExceptionGroup``) is raised, preferring the metadata's.
    """
    # The two requests are independent, so they run concurrently (the client's rate limiter
    # still spaces them). TaskGroup gives the cancellation: when one fails it cancels the other,
    # so no request is left running for a result nobody will use.
    try:
        async with asyncio.TaskGroup() as group:
            metadata = group.create_task(client.get_law_metadata(law_id))
            analysis = group.create_task(client.get_law_analysis(law_id))
    except ExceptionGroup as failures:
        # TaskGroup always wraps failures in an ExceptionGroup. Callers and ``server._guard``
        # catch ``BoeError`` and ``ValueError`` by type, so re-raise the first member. A cancelled
        # sibling is not part of the group, so normally there is exactly one.
        raise failures.exceptions[0] from None
    return metadata.result(), analysis.result()


async def get_law_overview(
    client: BoeClient,
    law_id: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> LawOverview:
    """Return a law's identity, derived legal status and amendment summary.

    The identifier is normalised like ``resolve_reference`` does (case, surrounding spaces).
    Raises ``ValueError`` for a malformed identifier and ``BoeNotFoundError`` for an unknown one.
    """
    # A malformed identifier is passed through unchanged so that the client's own check raises
    # the ``ValueError`` for it.
    metadata, analysis = await _fetch_both(client, normalise_identifier(law_id) or law_id)
    moment = _utc(now)
    # The BOE works in Madrid time, so "today" is the Madrid date, not the UTC one. Madrid is
    # one or two hours ahead, so for a short window around midnight UTC the dates differ, and a
    # law taking effect "tomorrow" in UTC terms may already be in force. ``today`` overrides this.
    status, note = derive_status(
        metadata,
        today if today is not None else moment.astimezone(_MADRID).date(),
        _repeal_reference_count(analysis),
    )
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
        provenance=Provenance(generated_at=moment, data_updated_at=metadata.updated_at),
    )
