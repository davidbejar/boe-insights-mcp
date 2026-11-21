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
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, get_args

from boe_insights_mcp.client import AuxiliaryTable, BoeClient
from boe_insights_mcp.clock import madrid_today, utc_now
from boe_insights_mcp.errors import BoeError, BoeNotFoundError
from boe_insights_mcp.identifiers import normalise_identifier
from boe_insights_mcp.models import (
    Block,
    BlockVersion,
    LawAnalysis,
    LawMetadata,
    LawReference,
    LawSummary,
)
from boe_insights_mcp.query import SortField, build_search_query
from boe_insights_mcp.references import ParsedReference, fold, parse_reference
from boe_insights_mcp.repeal import whole_law_repeal
from boe_insights_mcp.schemas import (
    AmendmentGroup,
    AmendmentHistoryResult,
    AmendmentItem,
    AmendmentSummary,
    CodeMatch,
    FindCodesResult,
    FirstPublication,
    LawBrief,
    LawOverview,
    LawStatus,
    Provenance,
    ReferenceBrief,
    RelationCount,
    ResolveReferenceResult,
    SearchLegislationResult,
    YearCount,
)
from boe_insights_mcp.structure import parse_publication_note
from boe_insights_mcp.versions import repeal_date, repeal_note, repeal_version_date

__all__ = [
    "AmendmentFilter",
    "CodeKind",
    "RankFilter",
    "ScopeFilter",
    "brief",
    "derive_status",
    "find_codes",
    "get_amendment_history",
    "get_law_overview",
    "rank_code",
    "relation_group",
    "resolve_reference",
    "scope_code",
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
AmendmentFilter = Literal["all", "repeals", "modifies", "adds", "corrections", "other"]

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
_REGIONAL_SCOPE_CODE = "2"

MAX_LIMIT = 50
_MAX_CANDIDATES = 10
_SAMPLE_SIZE = 10
_TITLE_SEARCH_LIMIT = 10
_MAX_NUMBER_HITS = 200
MAX_AMENDMENT_LIMIT = 100

# Relation texts of the BOE "relaciones-posteriores" table, folded (lower case, no accents), that
# put an amending norm in a group. The first group with a match wins, so the order is the
# precedence. Checked against all 50 relations of the table in the tests: the repeals group also
# takes annulments, suppressions and "sin efecto" (they remove text, like a repeal); the adds
# group takes "completa" and "amplia" (they put in new text); "actualiza" is an amendment.
_GROUP_KEYWORDS: tuple[tuple[AmendmentGroup, tuple[str, ...]], ...] = (
    ("repeals", ("se deroga", "anula", "deja sin efecto", "suprime")),
    ("corrections", ("correccion", "corrig", "errores")),
    ("modifies", ("modifica", "sustituye", "nueva redaccion", "actualiza")),
    ("adds", ("anade", "amplia", "completa")),
)
_YEAR_IN_ID = re.compile(r"-(\d{4})-")
_QUOTE_LIMIT = 300
_INITIAL_NOTE_LIMIT = 600

_FORMATS_HINT = (
    "Accepted formats: a BOE identifier (BOE-A-2015-10566), a rank and official number "
    "(Ley 40/2015, Real Decreto-ley 6/2019, LO 3/2018) or just a number (40/2015). "
    "Years need four digits."
)


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


# The compact record is public because the trend tools return the same one (``trends``).
brief = _brief


def rank_code(rank: RankFilter) -> str:
    """The BOE rank code of a ``RankFilter`` value, for filtering hits locally."""
    return _RANK_CODES[rank]


def scope_code(scope: ScopeFilter) -> str | None:
    """The BOE scope code of a ``ScopeFilter`` value; ``None`` for ``any``."""
    return _SCOPE_CODES[scope]


def _newest_update(laws: Iterable[LawSummary]) -> datetime | None:
    """Latest ``updated_at`` among ``laws``, or ``None``; becomes ``Provenance.data_updated_at``."""
    stamps = [law.updated_at for law in laws if law.updated_at is not None]
    return max(stamps, default=None)


def derive_status(
    metadata: LawMetadata,
    today: date,
    repeal_references: int,
    repeal_note: BlockVersion | None,
    *,
    repeal_on: date | None = None,
) -> tuple[LawStatus, str]:
    """Return the legal status of a law and a plain-English note explaining it.

    ``repeal_references`` is the number of later norms whose relation is a repeal
    (:func:`_repeal_reference_count`); ``repeal_note`` is the version of the law's initial note
    that announces a repeal (``versions.repeal_note``), or ``None``, and ``repeal_on`` the date
    that note gives (``versions.repeal_date``; read from ``repeal_note`` itself when omitted).
    Precedence: annulled, repealed (the flag or the note, in effect today), validity exhausted,
    not yet in force, partially repealed, in force. A repeal dated after ``today`` is only
    announced: the status is then the one the other rules give, and the note says so. The BOE's
    repeal flag marks only whole-law repeals, so a law that later norms repeal in part (or in
    full, without the flag) is reported as ``partially_repealed`` rather than ``in_force``.
    """
    # Order encodes how strong each signal is. The two BOE flags are authoritative, so they come
    # first and an annulled or repealed law is never reported as "not yet in force". The repeal
    # note is as strong as the repeal flag: some wholly repealed laws carry only the note
    # (LO 15/1999, flag N), others only the flag (Ley 30/2007, no note); neither signal is
    # complete alone (api-notes, "Initial note"). Validity exhausted (a spent law such as a past
    # budget) comes next. ``fecha_vigencia`` is when the law *enters* into force, never an end
    # date (api-notes 4), so it only ever yields "not yet". "partially_repealed" is a heuristic
    # read from the amendment references, so it is checked last: it must not mask a stronger
    # signal, and it downgrades only a law that would otherwise be shown as plainly in force.
    # A repeal that takes effect in the future (an announced one) is not a repeal yet, so it is
    # left out of this order and only mentioned at the end.
    if metadata.annulled:
        return "annulled", "The BOE flags this law as annulled (declared void)."
    if repeal_on is None and repeal_note is not None:
        repeal_on = repeal_version_date(repeal_note)
    repeal = whole_law_repeal(
        flagged=metadata.repealed,
        flag_on=metadata.repealed_on,
        note_version=repeal_note,
        note_on=repeal_on,
        today=today,
    )
    if repeal.in_effect and metadata.repealed:
        when = f" on {metadata.repealed_on.isoformat()}" if metadata.repealed_on else ""
        return "repealed", f"The BOE flags this law as repealed{when}."
    if repeal.in_effect and repeal_note is not None:
        return (
            "repealed",
            "The BOE's consolidated text opens with a repeal note "
            f'("{_first_sentence(repeal_note)}"), even though the BOE metadata flag is not set.',
        )
    status, note = _status_without_repeal(metadata, today, repeal_references)
    if repeal.announced and repeal.on is not None:
        note += (
            f" A repeal of the whole law is announced for {repeal.on.isoformat()}, which is in "
            "the future."
        )
    return status, note


def _status_without_repeal(
    metadata: LawMetadata, today: date, repeal_references: int
) -> tuple[LawStatus, str]:
    """The status from the rules that come after the whole-law repeal (see ``derive_status``)."""
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


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, ending with an ellipsis when it was cut."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _first_sentence(version: BlockVersion) -> str:
    """The first sentence of a note version, at most ``_QUOTE_LIMIT`` characters."""
    # The note's first paragraph is the announcement; it is followed by a "Ref. BOE-..." link
    # text after a full stop, which the cut at the first ". " leaves out.
    first = version.paragraphs[0].text if version.paragraphs else ""
    end = first.find(". ")
    return _truncate(first if end < 0 else first[: end + 1], _QUOTE_LIMIT)


def _initial_note_text(note: Block | None) -> str | None:
    """The text of the latest version of the initial note, capped, or ``None`` without a note."""
    # ``Block.versions`` is ascending by start date, so the last one is the current note.
    text = note.versions[-1].text if note is not None and note.versions else ""
    return _truncate(text, _INITIAL_NOTE_LIMIT) if text else None


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
        provenance=Provenance(generated_at=utc_now(now), data_updated_at=_newest_update(kept)),
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
    provenance_time = utc_now(now)
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
        provenance=Provenance(generated_at=utc_now(now)),
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
    matches: list[LawSummary] = []
    # The API matches the number as a loose phrase (api-notes 2), so a common number such as 1/2020
    # can have more hits than one page, and the exact match may sit further down. Read pages until
    # a short one (the end) or ``_MAX_NUMBER_HITS`` hits, which bounds the requests of one lookup.
    for offset in range(0, _MAX_NUMBER_HITS, MAX_LIMIT):
        page = await client.search_laws(query, offset=offset, limit=MAX_LIMIT)
        # Check the number again, and the rank, since the rank filter is approximate.
        matches.extend(
            law
            for law in page
            if law.official_number == parsed.official_number
            and (
                parsed.rank_code is None
                or (law.rank is not None and law.rank.code == parsed.rank_code)
            )
        )
        if len(page) < MAX_LIMIT:
            break
    return matches


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


@dataclass(frozen=True)
class _LawData:
    """What the overview needs from the API: the three concurrent fetches."""

    metadata: LawMetadata
    analysis: LawAnalysis
    note: Block | None
    note_checked: bool
    """False when the initial note could not be fetched (``note`` is then ``None``)."""


async def _fetch_note(client: BoeClient, law_id: str) -> tuple[Block | None, bool]:
    """Fetch the initial note; a BOE failure gives ``(None, False)`` instead of raising."""
    # The note is extra information. It must not fail the overview, and, inside the task group,
    # an exception would also cancel the metadata and analysis requests that did succeed.
    try:
        return await client.get_initial_note(law_id), True
    except BoeError:
        return None, False


async def _fetch_law(client: BoeClient, law_id: str) -> _LawData:
    """Fetch metadata, analysis and initial note together; metadata or analysis failure cancels.

    The original error (not an ``ExceptionGroup``) is raised, preferring the metadata's.
    """
    # The three requests are independent, so they run concurrently (the client's rate limiter
    # still spaces them). TaskGroup gives the cancellation: when metadata or analysis fails it
    # cancels the others, so no request is left running for a result nobody will use.
    metadata: asyncio.Task[LawMetadata] | None = None
    try:
        async with asyncio.TaskGroup() as group:
            metadata = group.create_task(client.get_law_metadata(law_id))
            analysis = group.create_task(client.get_law_analysis(law_id))
            note = group.create_task(_fetch_note(client, law_id))
    except ExceptionGroup as failures:
        # TaskGroup always wraps failures in an ExceptionGroup. Callers and ``server._guard``
        # catch ``BoeError`` and ``ValueError`` by type, so re-raise a member. When both calls
        # failed in the same turn the group lists them in completion order, which is arbitrary:
        # the metadata error is the one that says whether the law exists, so it wins.
        if metadata is not None and metadata.done() and not metadata.cancelled():
            error = metadata.exception()
            if error is not None:
                raise error from None
        raise failures.exceptions[0] from None
    return _LawData(metadata.result(), analysis.result(), *note.result())


def _first_publication(metadata: LawMetadata, analysis: LawAnalysis) -> FirstPublication | None:
    """Where a regional law was first published, or ``None`` (state laws and unknown cases)."""
    # Two cases, both from api-notes, third probe, "Regional laws and publication dates". A law
    # with a regional-gazette identifier (``DOGC-f-``, ``BOJA-b-``...) *is* the regional
    # publication: its metadata already names that gazette and carries its date, and there is no
    # BOE reprint. A regional law reprinted in the BOE (``BOE-A-`` with scope code 2) has the BOE
    # reprint date in the metadata, which can be decades after the regional one, and only a note
    # in the analysis says where it first appeared. That date matters because it, not the
    # reprint, governs entry into force. State laws have no earlier publication.
    if not metadata.identifier.startswith("BOE-"):
        if metadata.gazette is None or metadata.published_on is None:
            return None
        return FirstPublication(
            gazette=metadata.gazette,
            number=metadata.gazette_number,
            date=metadata.published_on,
        )
    if metadata.identifier.startswith("BOE-A-") and (
        metadata.scope is not None and metadata.scope.code == _REGIONAL_SCOPE_CODE
    ):
        note = parse_publication_note(analysis.notes)
        if note is not None:
            return FirstPublication(gazette=note.gazette, number=note.number, date=note.date)
    return None


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
    data = await _fetch_law(client, normalise_identifier(law_id) or law_id)
    metadata, analysis = data.metadata, data.analysis
    moment = utc_now(now)
    repeal_version = repeal_note(data.note)
    now_day = madrid_today(moment, today)
    status, note = derive_status(
        metadata,
        now_day,
        _repeal_reference_count(analysis),
        repeal_version,
        repeal_on=repeal_date(data.note),
    )
    repeal = whole_law_repeal(
        flagged=metadata.repealed,
        flag_on=metadata.repealed_on,
        note_version=repeal_version,
        note_on=repeal_date(data.note),
        today=now_day,
    )
    if not data.note_checked:
        note += (
            " The BOE's initial note could not be checked, so a repeal announced only there "
            "would not show here."
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
        first_published=_first_publication(metadata, analysis),
        effective_from=metadata.effective_from,
        status=status,
        status_note=note,
        # An announced repeal has not happened yet: its date is in the status note only.
        repealed_on=None if repeal.announced else repeal.on,
        initial_note=_initial_note_text(data.note),
        consolidation_state=(
            metadata.consolidation_state.text if metadata.consolidation_state else None
        ),
        amendments=_amendment_summary(analysis),
        url=metadata.html_url,
        eli_url=metadata.eli_url,
        provenance=Provenance(generated_at=moment, data_updated_at=metadata.updated_at),
    )


def relation_group(relation: str) -> AmendmentGroup:
    """The group of an amendment relation text such as ``SE MODIFICA``; ``other`` if unknown."""
    folded = fold(relation)
    for group, keywords in _GROUP_KEYWORDS:
        if any(keyword in folded for keyword in keywords):
            return group
    return "other"


def _year_of(law_id: str) -> int | None:
    """The 4-digit year in an identifier such as ``BOE-A-2020-123``, or ``None``."""
    found = _YEAR_IN_ID.search(law_id)
    return int(found.group(1)) if found else None


def _amendment_items(analysis: LawAnalysis) -> list[AmendmentItem]:
    """Every later reference as an item, newest year first; the API order breaks ties."""
    items = [
        AmendmentItem(
            law_id=reference.law_id,
            year=_year_of(reference.law_id),
            relation=reference.relation.text.strip(),
            group=relation_group(reference.relation.text),
            text=reference.text,
        )
        for reference in analysis.later
    ]
    # ``sorted`` is stable. Items without a year sort after every dated one.
    return sorted(items, key=lambda item: -item.year if item.year is not None else 1)


def _year_counts(items: Iterable[AmendmentItem]) -> list[YearCount]:
    """Count items per year, newest first, with the undated ones last."""
    counts = Counter(item.year for item in items)
    return [
        YearCount(year=year, count=counts[year])
        for year in sorted(counts, key=lambda year: -year if year is not None else 1)
    ]


async def get_amendment_history(
    client: BoeClient,
    law_id: str,
    group: AmendmentFilter = "all",
    limit: int = 50,
    offset: int = 0,
    *,
    now: datetime | None = None,
) -> AmendmentHistoryResult:
    """List the later norms that affect a whole law, newest first, one page at a time.

    Built from the analysis only (no text requests). ``groups`` counts all later references,
    while ``total``, ``by_year`` and ``items`` follow the ``group`` filter. Raises ``ValueError``
    for invalid arguments or a malformed identifier and ``BoeNotFoundError`` for an unknown law.
    """
    if group not in get_args(AmendmentFilter):
        raise ValueError(f"group must be one of {', '.join(get_args(AmendmentFilter))}")
    if not 1 <= limit <= MAX_AMENDMENT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_AMENDMENT_LIMIT}")
    if offset < 0:
        raise ValueError("offset must not be negative")
    law_id = normalise_identifier(law_id) or law_id
    analysis = await client.get_law_analysis(law_id)
    everything = _amendment_items(analysis)
    selected = [item for item in everything if group in ("all", item.group)]
    counts = Counter(item.group for item in everything)
    page = selected[offset : offset + limit]
    has_more = offset + limit < len(selected)
    return AmendmentHistoryResult(
        law_id=law_id,
        group=group,
        total=len(selected),
        groups={name: counts[name] for name in get_args(AmendmentGroup)},
        by_year=_year_counts(selected),
        items=page,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
        provenance=Provenance(generated_at=utc_now(now)),
    )
