"""Output models of the MCP tools (pydantic v2).

Every tool result embeds a :class:`Provenance`, which carries the attribution and the
"informational only" notice the BOE reuse conditions require.

These are the return types of the tools in ``server``; ``legislation`` and ``articles`` build
them from the client's ``models`` types. FastMCP derives each tool's output schema from them, so
the class docstrings and ``Field`` descriptions here are sent to the language model and are
model-facing text (see CLAUDE.md, "Documentation standard"); notes for human readers go in ``#``
comments.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from boe_insights_mcp.chains import Direction, RelationKind
from boe_insights_mcp.citation import CitationStatus, MatchLevel
from boe_insights_mcp.structure import EntryKind
from boe_insights_mcp.versions import ArticleState

__all__ = [
    "GAZETTE_NOTICE",
    "MAX_QUOTE_CHARS",
    "AmendedLaw",
    "AmendmentChainResult",
    "AmendmentGroup",
    "AmendmentHistoryResult",
    "AmendmentItem",
    "AmendmentSummary",
    "ArticleCandidate",
    "ArticleChange",
    "ArticleHistoryEvent",
    "ArticleHistoryResult",
    "ArticleLocation",
    "ArticleResult",
    "ArticleState",
    "ChainEdgeInfo",
    "ChainNodeInfo",
    "CheckSearchesResult",
    "CitationCheck",
    "CitationRequest",
    "CodeMatch",
    "CompareArticleVersionsResult",
    "CompareTopicsResult",
    "Coverage",
    "DailySummaryResult",
    "DayMatches",
    "DeleteSearchResult",
    "DepartmentCount",
    "ExplainSpikeResult",
    "FindCodesResult",
    "FirstPublication",
    "GazetteCount",
    "GazetteEntry",
    "GazetteIssueInfo",
    "GroupSeries",
    "LawBrief",
    "LawOverview",
    "LawStatus",
    "LawStructureResult",
    "LegislationOverTimeResult",
    "MostAmendedLawsResult",
    "OtherVersionInfo",
    "PeriodCount",
    "PeriodSpike",
    "Provenance",
    "QuoteMatchInfo",
    "ReadArticleResult",
    "ReferenceBrief",
    "RelationCount",
    "ResolveReferenceResult",
    "SaveSearchResult",
    "SavedSearchInfo",
    "SavedSearchList",
    "ScanGazetteResult",
    "ScannedEntry",
    "SearchChange",
    "SearchCheck",
    "SearchLegislationResult",
    "SpikeContribution",
    "StructureItem",
    "TopDepartmentsResult",
    "TopicSpec",
    "TopicTrend",
    "VerifyCitationResult",
    "VersionInfo",
    "YearCount",
]

LawStatus = Literal[
    "in_force",
    "partially_repealed",
    "repealed",
    "annulled",
    "validity_exhausted",
    "not_yet_in_force",
]


_Day = date


class _Output(BaseModel):
    """Frozen base for tool outputs."""

    model_config = ConfigDict(frozen=True)


# The notice of results that describe the daily gazette itself rather than consolidated texts.
GAZETTE_NOTICE = (
    "Summary of the official gazette. Only the texts published in the BOE itself are authentic."
)


class Provenance(_Output):
    """Where the data comes from and under which conditions it may be reused."""

    source: str = "Agencia Estatal Boletín Oficial del Estado"
    attribution: str = "Basado en datos de la Agencia Estatal Boletín Oficial del Estado"
    source_url: str = "https://www.boe.es"
    generated_at: datetime = Field(
        description="When this result was assembled (UTC). The underlying data may come from a "
        "cache and be up to 7 days old; see data_updated_at for the BOE's own stamp."
    )
    data_updated_at: datetime | None = Field(
        default=None,
        description="The BOE's own last-update stamp for this data, when known (UTC).",
    )
    notice: str = (
        "Consolidated texts are provided for information only and have no official legal "
        "value. Only the texts published in the official gazette are authentic."
    )


class LawBrief(_Output):
    """A compact law record, used in lists."""

    identifier: str = Field(description="BOE identifier, e.g. BOE-A-2015-10566.")
    title: str
    rank: str | None = Field(default=None, description="Rank, e.g. 'Ley Orgánica'.")
    official_number: str | None = Field(default=None, description="Official number, e.g. 40/2015.")
    department: str | None = None
    scope: str | None = Field(default=None, description="'Estatal' (state) or 'Autonómico'.")
    published_on: date | None = Field(
        default=None,
        description="Publication date in the gazette of record (the BOE, or the regional "
        "gazette for laws identified by one).",
    )
    validity_exhausted: bool = Field(
        description="True when the law's effects are spent (e.g. a budget law of a past year)."
    )
    url: str | None = Field(default=None, description="Consolidated text page on boe.es.")


class SearchLegislationResult(_Output):
    """One page of search results."""

    items: list[LawBrief]
    returned: int = Field(description="Number of items in this page.")
    offset: int = Field(description="Offset this page started at.")
    has_more: bool = Field(
        description="True if further results may exist. The API gives no total count."
    )
    next_offset: int | None = Field(
        description="Offset to pass for the next page; None when has_more is false."
    )
    provenance: Provenance


class ResolveReferenceResult(_Output):
    """The outcome of resolving a reference to law identifiers."""

    reference: str = Field(description="The reference as given.")
    status: Literal["resolved", "ambiguous", "not_found"]
    interpreted_as: str | None = Field(
        description="How the reference was understood, e.g. 'Ley 40/2015 (state)'; "
        "None if it could not be parsed."
    )
    matches: list[LawBrief] = Field(
        description="One law when resolved; up to 10 candidates when ambiguous; empty otherwise."
    )
    hint: str | None = Field(description="What to do next when the reference is not resolved.")
    provenance: Provenance


class CodeMatch(_Output):
    """One entry of a BOE code table."""

    code: str = Field(description="The BOE code, to pass as a filter value.")
    text: str = Field(description="Its name, as the BOE publishes it (Spanish).")


class FindCodesResult(_Output):
    """Codes of a BOE table whose names match a text."""

    kind: Literal["department", "subject", "rank"]
    query: str = Field(description="The text searched for, as given.")
    matches: list[CodeMatch] = Field(description="Shortest names first; at most `limit` entries.")
    returned: int = Field(description="Number of entries in matches.")
    total_matches: int = Field(description="Number of entries of the table that match.")
    provenance: Provenance


class RelationCount(_Output):
    """How many amendment references have a given relation."""

    relation: str = Field(description="Relation text, e.g. 'SE MODIFICA'.")
    count: int


class ReferenceBrief(_Output):
    """One amendment cross-reference to another norm."""

    law_id: str = Field(description="Identifier of the other norm.")
    relation: str
    text: str = Field(description="The reference text as published (Spanish).")


class AmendmentSummary(_Output):
    """Counts and samples of the norms that amend this law or that it amends."""

    affected_by_count: int = Field(description="Number of later norms that affect this law.")
    affects_count: int = Field(description="Number of earlier norms this law affects.")
    affected_by_relations: list[RelationCount] = Field(
        description="Counts per relation, most frequent first."
    )
    affects_relations: list[RelationCount]
    affected_by_sample: list[ReferenceBrief] = Field(
        description="The first 10 later norms, as returned by the API."
    )
    affects_sample: list[ReferenceBrief]


class FirstPublication(_Output):
    """Where and when a regional law was first published."""

    gazette: str = Field(description="Gazette abbreviation, e.g. 'BOPA'.")
    gazette_name: str | None = Field(
        default=None, description="Full name of the gazette, when the BOE gives it."
    )
    number: str | None = Field(default=None, description="Issue number of the gazette.")
    # ``_Day`` and not ``date``: inside this class the name ``date`` is the field itself.
    date: _Day = Field(description="Publication date in that gazette.")


class LawOverview(_Output):
    """Identity, legal status and amendment summary of one law."""

    identifier: str
    title: str
    rank: str | None = None
    official_number: str | None = None
    department: str | None = None
    scope: str | None = None
    enacted_on: date | None = Field(default=None, description="Date of the disposition.")
    published_on: date | None = Field(
        default=None,
        description="Publication date in the gazette of record: the BOE for state laws and for "
        "regional laws reprinted there.",
    )
    first_published: FirstPublication | None = Field(
        default=None,
        description="For regional laws: the regional gazette, issue and date of first "
        "publication, which governs entry into force. Null for state laws or when unknown.",
    )
    effective_from: date | None = None
    status: LawStatus = Field(
        description="Derived from the BOE's repeal, annulment and validity flags and from the "
        "later norms that repeal it; an indication, not a legal determination."
    )
    status_note: str = Field(description="Plain-English explanation of the status, with caveats.")
    repealed_on: date | None = None
    initial_note: str | None = Field(
        default=None,
        description="The note the BOE shows above the law's text (a repeal, a rename, a "
        "correction notice), up to 600 characters; None when there is none.",
    )
    consolidation_state: str | None = None
    amendments: AmendmentSummary
    url: str | None = None
    eli_url: str | None = Field(default=None, description="European Legislation Identifier URL.")
    provenance: Provenance


AmendmentGroup = Literal["repeals", "modifies", "adds", "corrections", "other"]


class YearCount(_Output):
    """How many amendment references come from norms of one year."""

    year: int | None = Field(description="Year of the amending norm; None when its id has none.")
    count: int


class AmendmentItem(_Output):
    """One later norm that affects the law."""

    law_id: str = Field(description="Identifier of the amending norm.")
    year: int | None = Field(description="Year taken from the identifier; None if it has none.")
    relation: str = Field(description="The BOE's relation text, e.g. 'SE MODIFICA'.")
    group: AmendmentGroup
    text: str = Field(description="What it affects, as published (Spanish).")


class AmendmentHistoryResult(_Output):
    """The later norms that affect a whole law, newest first."""

    law_id: str
    group: Literal["all", "repeals", "modifies", "adds", "corrections", "other"] = Field(
        description="The group filter that was applied."
    )
    total: int = Field(description="Number of references in the selected group.")
    groups: dict[AmendmentGroup, int] = Field(
        description="References per group over all later norms, whatever the filter. "
        "'repeals' also covers annulments and suppressions."
    )
    by_year: list[YearCount] = Field(
        description="References of the selected group per year of the amending norm, newest first."
    )
    items: list[AmendmentItem] = Field(description="One page, newest year first.")
    has_more: bool = Field(description="True when more items follow; pass next_offset.")
    next_offset: int | None
    provenance: Provenance


class ArticleCandidate(_Output):
    """A block of the law's text that may be the one meant."""

    block_id: str = Field(description="Pass this as `block_id` to select the block exactly.")
    title: str = Field(description="The block's title in the law's index, e.g. 'Artículo 46 bis'.")


class ArticleLocation(_Output):
    """Which block of the law an article tool found for the wording given."""

    law_id: str
    outcome: Literal["found", "ambiguous", "not_found"] = Field(
        description="'ambiguous' and 'not_found' are normal answers: see candidates and hint."
    )
    block_id: str | None = Field(default=None, description="The block found; None otherwise.")
    block_title: str | None = None
    candidates: list[ArticleCandidate] = Field(
        default_factory=list,
        description="When ambiguous: every match (at most 10). When not found: the closest 8.",
    )
    hint: str | None = Field(default=None, description="What to do next when nothing was found.")
    provenance: Provenance


class ArticleResult(ArticleLocation):
    """An article result: the location plus what is known of a repeal of the whole law."""

    law_repealed: bool | None = Field(
        default=None,
        description="True when the whole law is repealed (an article then shows no marker); "
        "None when that could not be checked.",
    )
    law_repealed_on: date | None = Field(
        default=None,
        description="When the whole law is repealed, if known; a future date is an announced "
        "repeal.",
    )
    note: str | None = Field(
        default=None,
        description="Explains an empty, upcoming, truncated or special result, or a repeal of "
        "the whole law.",
    )


class VersionInfo(_Output):
    """One version of an article: the text as amended by one norm."""

    index: int = Field(description="1-based position among the article's versions, oldest first.")
    starts_on: date | None = Field(
        description="When this version starts to apply: effective_from, else published_on."
    )
    effective_from: date | None = Field(description="The BOE's effective date; may be missing.")
    published_on: date | None = Field(
        description="Date this version was published; "
        "for regional laws this is the regional gazette date.",
    )
    introduced_by: str = Field(description="Identifier of the norm that introduced this version.")


class ReadArticleResult(ArticleResult):
    """The text of one article as in force on a date."""

    as_of: date = Field(description="The date the text is valid for.")
    state: ArticleState | None = Field(
        default=None,
        description="On as_of: in_force, repealed, not_yet_in_force or did_not_exist.",
    )
    version: VersionInfo | None = Field(
        default=None,
        description="The version returned; for not_yet_in_force, the upcoming one.",
    )
    version_count: int = Field(default=0, description="Number of versions the article has.")
    is_latest: bool | None = Field(
        default=None, description="True when no later version exists, even a future one."
    )
    superseded_on: date | None = Field(
        default=None, description="When the next version starts; None if this is the latest."
    )
    text: str = Field(
        default="",
        description="Body text of the version (Spanish), paragraphs separated by blank lines; "
        "quoted paragraphs start with '> '. Joining the pages gives the full text.",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Footnotes of the version, up to 2000 characters in all; only on the first "
        "page.",
    )
    notes_truncated: bool = Field(default=False, description="True when notes were cut.")
    total_chars: int = Field(default=0, description="Length of the whole text.")
    truncated: bool = Field(default=False, description="True when text is only a part.")
    next_offset: int | None = Field(
        default=None, description="Pass as offset, with the same other arguments, for the rest."
    )


class ArticleChange(_Output):
    """What changed in one paragraph between two versions."""

    op: Literal["added", "removed", "changed"]
    old_text: str | None = Field(
        description="Paragraph before; None when added, or when 'inline' shows the change."
    )
    new_text: str | None = Field(
        description="Paragraph after; None when removed, or when 'inline' shows the change."
    )
    inline: str | None = Field(
        description="For a 'changed' paragraph: the new paragraph with removed words as [-...-] "
        "and added words as {+...+}. None for multi-line paragraphs (tables): see old_text and "
        "new_text. Text cut to fit the size budget ends in '…[truncated]'."
    )


class CompareArticleVersionsResult(ArticleResult):
    """The difference between an article's text on two dates."""

    from_date: date
    to_date: date
    from_version: VersionInfo | None = Field(
        default=None, description="Version in force on from_date; None if there was none."
    )
    to_version: VersionInfo | None = None
    identical: bool | None = Field(
        default=None,
        description="True when both dates select the same text; None if nothing to compare.",
    )
    added: int = Field(default=0, description="Paragraphs added.")
    removed: int = Field(default=0, description="Paragraphs removed.")
    changed: int = Field(default=0, description="Paragraphs reworded.")
    unchanged: int = Field(default=0, description="Paragraphs left as they were.")
    changes: list[ArticleChange] = Field(
        default_factory=list,
        description="The paragraphs that differ, in reading order, within max_changes and "
        "max_chars.",
    )
    changes_truncated: bool = Field(
        default=False,
        description="True when more changes exist than max_changes or max_chars allowed.",
    )
    intermediate_versions: int = Field(
        default=0, description="Versions strictly between the two compared."
    )
    amending_laws: list[str] = Field(
        default_factory=list,
        description="Norms that introduced the versions after from_version, up to to_version.",
    )
    unified_diff: str = Field(
        default="", description="Unified diff of the body text, one paragraph per line."
    )
    unified_truncated: bool = Field(
        default=False, description="True when unified_diff was cut at 6000 characters."
    )


class ArticleHistoryEvent(_Output):
    """One version of an article in its amendment history."""

    index: int = Field(description="1-based position among the article's versions.")
    kind: Literal["original", "amended", "repealed"]
    effective_from: date | None
    published_on: date | None = Field(
        description="Date this version was published; "
        "for regional laws this is the regional gazette date.",
    )
    introduced_by: str = Field(description="Identifier of the norm that introduced the version.")
    added: int = Field(description="Paragraphs added versus the previous version.")
    removed: int
    changed: int


class ArticleHistoryResult(ArticleResult):
    """When and by which norm an article was amended."""

    version_count: int = Field(default=0, description="Number of versions the article has.")
    events: list[ArticleHistoryEvent] = Field(
        default_factory=list, description="Oldest first; at most the 60 most recent versions."
    )
    truncated: bool = Field(default=False, description="True when older versions were left out.")
    current_state: ArticleState | None = Field(
        default=None, description="State of the article today."
    )


class StructureItem(_Output):
    """One line of a law's table of contents."""

    block_id: str = Field(description="Pass this as block_id to the article tools.")
    kind: EntryKind
    level: int = Field(description="Nesting depth; 1 is the top level.")
    label: str = Field(description="Designation, e.g. 'TÍTULO I', 'Artículo 27'.")
    heading: str = Field(description="Subject line after the designation; empty when none.")
    # ``exclude_if`` leaves ``state`` out when it is ``None`` (in force, the usual case, or blanked
    # by the use case), to save space; a custom serializer would do the same but would hide the
    # field's description from the output schema.
    state: Literal["repealed", "not_yet_in_force"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Present only when the entry is not in force on as_of.",
    )


class LawStructureResult(_Output):
    """One page of a law's table of contents as of a date."""

    law_id: str
    title: str = Field(description="Title of the law.")
    as_of: date = Field(description="The date the structure is valid for.")
    entries: list[StructureItem] = Field(description="In reading order.")
    returned: int = Field(description="Number of entries in this page.")
    total_entries: int = Field(description="Entries in the whole structure for this detail level.")
    has_more: bool
    next_offset: int | None = Field(
        description="Offset to pass for the next page; None when has_more is false."
    )
    counts: dict[str, int] = Field(
        description="Entries per kind in the whole law, whatever detail and page."
    )
    note: str | None = Field(
        default=None,
        description="Explains a law not yet in force, repealed or empty on as_of.",
    )
    provenance: Provenance


# --- Trend tools --------------------------------------------------------------------------------


class Coverage(_Output):
    """What the counts were computed from."""

    catalogue_size: int = Field(description="Laws in the downloaded consolidated collection.")
    catalogue_fetched_at: datetime = Field(description="When that collection was downloaded (UTC).")
    matched: int = Field(description="Laws that passed the filters, before the date range.")


class PeriodCount(_Output):
    """The number of laws in one period."""

    period: str = Field(description="'2020', '2020-Q3' or '2020-03'.")
    count: int
    # ``exclude_if`` leaves the two optional flags out of the output when they say nothing (most
    # points), to save space, and unlike a custom serializer keeps them in the output schema.
    partial: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
        description="True when the period is unfinished or cut by the range; omitted when false.",
    )
    highest_number: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Highest official number seen that year for this rank: a lower bound of how "
        "many were enacted, to judge coverage.",
    )


class PeriodSpike(_Output):
    """A period with unusually many laws."""

    period: str
    count: int
    baseline: float = Field(description="Median count of the other complete periods in the range.")
    ratio: float | None = Field(description="count / baseline; null when the baseline is 0.")


class GroupSeries(_Output):
    """The series of one group (rank, department or scope)."""

    code: str | None = Field(description="BOE code; null for the row 'other'.")
    label: str
    total: int
    series: list[PeriodCount]


class LegislationOverTimeResult(_Output):
    """Laws counted per period."""

    from_date: date = Field(description="Start of the range used.")
    to_date: date = Field(description="End of the range used.")
    period: Literal["year", "quarter", "month"]
    series: list[PeriodCount]
    total: int = Field(description="Laws counted in the range.")
    undated: int = Field(description="Matching laws without a date for the chosen date_basis.")
    groups: list[GroupSeries] = Field(
        description="One series per top group, then 'other'; empty when group_by is 'none'."
    )
    spikes: list[PeriodSpike] = Field(description="Periods that stand out, strongest first.")
    coverage: Coverage
    caveats: list[str]
    provenance: Provenance


class DepartmentCount(_Output):
    """One department and the laws it issued."""

    code: str
    label: str
    count: int
    share: float = Field(description="count / laws with a department, 0 to 1.")


class TopDepartmentsResult(_Output):
    """Departments ranked by number of laws."""

    from_date: date
    to_date: date
    departments: list[DepartmentCount]
    other_count: int = Field(description="Laws of the departments not listed.")
    total: int = Field(description="Laws in the range.")
    distinct_departments: int
    coverage: Coverage
    caveats: list[str]
    provenance: Provenance


class TopicSpec(BaseModel):
    """One topic to compare: a name and at least one criterion."""

    name: str = Field(min_length=1, description="Label for the topic, e.g. 'teletrabajo'.")
    title: str | None = Field(
        default=None,
        description="Words that must all be in the title (whole words; singular and plural forms "
        "both match); use text for laws that treat a topic without naming it.",
    )
    text: str | None = Field(default=None, description="Words that must all be in the body text.")
    subject_code: str | None = Field(default=None, description="BOE subject (materia) code.")


class TopicTrend(_Output):
    """One topic's counts over time."""

    name: str
    total: int = Field(description="Laws of the topic in the range.")
    share_of_all: float | None = Field(
        description="total / all laws in the range under the same rank, scope and department."
    )
    peak: str | None = Field(description="Complete period with the most laws; null if none.")
    first: str | None = Field(description="First period in the range with a law of the topic.")
    series: list[PeriodCount]


class CompareTopicsResult(_Output):
    """Several topics counted over the same periods."""

    from_date: date
    to_date: date
    period: Literal["year", "quarter", "month"]
    topics: list[TopicTrend] = Field(description="In the order given.")
    coverage: Coverage = Field(
        description="matched is the reference population: all laws under the rank, scope and "
        "department filters."
    )
    caveats: list[str]
    provenance: Provenance


class SpikeContribution(_Output):
    """A group that grew in the explained period."""

    dimension: Literal["rank", "department", "scope"]
    code: str
    label: str
    count: int = Field(description="Laws of the group in the period.")
    expected: float = Field(description="The group's mean per baseline period.")
    excess: float = Field(description="count - expected.")


class ExplainSpikeResult(_Output):
    """Why a period has many laws."""

    found: bool = Field(description="False when no period stands out (see note).")
    period: str | None
    count: int | None = Field(description="Laws in the period.")
    baseline: float | None = Field(
        description="Mean laws per period over the baseline_periods preceding periods."
    )
    baseline_periods: int | None = Field(description="Number of preceding periods averaged.")
    contributions: list[SpikeContribution] = Field(description="Largest excess first.")
    laws: list[LawBrief] = Field(
        description="An evenly spaced sample of the period's laws in date order, first and last "
        "included, up to max_laws."
    )
    laws_total: int = Field(description="All laws of the period.")
    other_spikes: list[PeriodSpike] = Field(description="Other unusual periods in the range.")
    note: str | None = None
    coverage: Coverage
    caveats: list[str]
    provenance: Provenance


class AmendedLaw(_Output):
    """A law and how many later laws amended it."""

    law: LawBrief
    amendments: int = Field(description="References that modify, add to or repeal it.")
    by_kind: dict[str, int] = Field(description="Amendments per kind: modifies, adds, repeals.")


class MostAmendedLawsResult(_Output):
    """Laws ranked by amendments, among those examined so far."""

    from_date: date | None = Field(description="Start of the range; null when there is none.")
    to_date: date
    laws: list[AmendedLaw] = Field(description="Most amended first.")
    candidates: int = Field(description="Matching laws in the range.")
    examined: int = Field(description="Laws whose amendments were counted.")
    failed: int = Field(description="Laws skipped after a BOE error.")
    complete: bool = Field(description="True when every candidate was examined successfully.")
    note: str | None = Field(default=None, description="What to do when not complete.")
    coverage: Coverage
    caveats: list[str]
    provenance: Provenance


class GazetteIssueInfo(_Output):
    """One issue of the gazette."""

    number: str = Field(description="Issue number, e.g. 67.")
    identifier: str = Field(description="Summary identifier, e.g. BOE-S-2020-67.")


class GazetteCount(_Output):
    """A section or department and how many items it has."""

    code: str | None = Field(description="Section or department code; null when the BOE has none.")
    name: str | None
    items: int


class GazetteEntry(_Output):
    """One item the BOE published."""

    identifier: str = Field(description="BOE-A-... (dispositions) or BOE-B-... (announcements).")
    title: str = Field(description="Title in Spanish, cut at about 200 characters.")
    section: str = Field(description="Section code, e.g. 1, 2A, 3, 5B.")
    department: str | None
    heading: str | None = Field(default=None, description="Heading the item sits under, if any.")
    issue: str | None = Field(
        default=None,
        description="Issue number, given when the day has several issues (e.g. an extraordinary "
        "one).",
    )
    url: str | None = Field(description="Link to the text on boe.es.")

    # Why a serializer: ``heading`` and ``issue`` are left out of the output when there is none,
    # which saves a null on most items of a page that the model reads in full.
    @model_serializer(mode="wrap")
    def _omit_empty_optionals(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Serialise as usual, then drop ``heading`` and ``issue`` when they are ``None``."""
        data: dict[str, object] = handler(self)
        for key in ("heading", "issue"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class DailySummaryResult(_Output):
    """What the BOE published on one day."""

    date: _Day = Field(description="The day shown.")
    published: bool = Field(description="False when there was no issue that day.")
    issues: list[GazetteIssueInfo]
    total_items: int = Field(description="Items of the whole day.")
    sections: list[GazetteCount] = Field(description="Whole day, in order of appearance.")
    departments: list[GazetteCount] = Field(
        description="The 15 with most items among the matching items."
    )
    matched: int = Field(description="Items that match the filters.")
    items: list[GazetteEntry] = Field(description="This page of the matching items.")
    returned: int = Field(description="Number of items in this page.")
    has_more: bool
    next_offset: int | None = Field(description="Offset for the next page; null when none.")
    note: str | None = None
    provenance: Provenance


class ScannedEntry(GazetteEntry):
    """A matching item with its day."""

    date: _Day


class DayMatches(_Output):
    """How many items matched on one day."""

    date: _Day
    matches: int


class ScanGazetteResult(_Output):
    """Items found over a range of days, among the days scanned so far."""

    from_date: _Day
    to_date: _Day
    days_scanned: int = Field(description="Days read so far.")
    days_with_issue: int = Field(description="Scanned days on which the BOE had an issue.")
    days_pending: int = Field(description="Days not yet read; the oldest ones.")
    days_failed: int = Field(description="Days skipped after an error that retrying cannot fix.")
    complete: bool = Field(description="True when no day is pending.")
    matched: int = Field(description="Matches in the scanned days.")
    items: list[ScannedEntry] = Field(description="This page of the matches, newest day first.")
    returned: int = Field(description="Number of items in this page.")
    has_more: bool
    next_offset: int | None = Field(description="Offset for the next page; null when none.")
    by_day: list[DayMatches] = Field(description="Days with matches, newest first.")
    note: str | None = Field(default=None, description="What to do when not complete.")
    provenance: Provenance


# --- saved searches ----------------------------------------------------------------------------

SearchStatus = Literal["checked", "partial", "failed"]


class SavedSearchInfo(_Output):
    """A saved search: its filters and when it was last checked."""

    name: str
    kind: Literal["gazette", "law"] = Field(
        description="'gazette': items by title words, section or department. 'law': amendments "
        "of one law."
    )
    # ``exclude_if`` leaves out the fields a kind does not have, so a search shows only its own
    # filters; ``last_checked_at`` stays because null there means "never checked".
    words: str | None = Field(default=None, exclude_if=lambda value: value is None)
    section: str | None = Field(default=None, exclude_if=lambda value: value is None)
    department_code: str | None = Field(default=None, exclude_if=lambda value: value is None)
    law_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    law_title: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Title of the law, cut at 200 characters.",
    )
    created_at: datetime
    last_checked_at: datetime | None = Field(description="Null when never checked.")
    checked_through: _Day | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: the last day read; the next check starts after it.",
    )


class SavedSearchList(_Output):
    """The saved searches of the user."""

    searches: list[SavedSearchInfo] = Field(description="Oldest first.")
    total: int
    limit: int = Field(description="Most searches allowed.")
    note: str | None = None
    provenance: Provenance


class SaveSearchResult(_Output):
    """A saved search after it was created or edited."""

    search: SavedSearchInfo
    name_was_proposed: bool = Field(description="True when the server chose the name.")
    total: int = Field(description="Saved searches now.")
    note: str | None = None
    provenance: Provenance


class DeleteSearchResult(_Output):
    """A saved search that was deleted."""

    deleted: str = Field(description="Name of the deleted search.")
    remaining: int = Field(description="Saved searches left.")
    provenance: Provenance


class SearchChange(_Output):
    """A norm that acted on a followed law and was not reported before."""

    law_id: str = Field(description="Identifier of the norm that acts.")
    kind: RelationKind
    relation: str = Field(description="The BOE's relation text (Spanish).")
    text: str = Field(description="Reference text from the BOE (Spanish), cut at 200 characters.")


class SearchCheck(_Output):
    """What is new for one saved search."""

    name: str
    kind: Literal["gazette", "law"]
    status: SearchStatus = Field(
        description="checked: nothing more to fetch now (see note for days still to retry). "
        "partial: more is ready, call again. failed: could not be read; see note."
    )
    # The fields below belong to one kind each; ``exclude_if`` leaves out those that do not
    # apply (or, for the dates, that nothing was read for).
    from_date: _Day | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: first day read this time.",
    )
    to_date: _Day | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: last day read this time.",
    )
    days_pending: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: days of the range not read yet.",
    )
    days_failed: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: days given up after a week of failed reads.",
    )
    days_retrying: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: days that could not be read and will be tried again at the "
        "next check.",
    )
    matched: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: matching items in the days read.",
    )
    items: list[ScannedEntry] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: new matches, oldest day first.",
    )
    has_more: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Gazette: new matches remain that were not shown.",
    )
    law_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    law_title: str | None = Field(default=None, exclude_if=lambda value: value is None)
    changes: list[SearchChange] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Law: new amendments, repeals, additions, suspensions and corrections.",
    )
    changes_total: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Law: all new changes, which may exceed len(changes).",
    )
    other_new: dict[str, int] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Law: new references that are not changes, counted only: court_rulings, "
        "citations, other.",
    )
    note: str | None = Field(default=None, exclude_if=lambda value: value is None)


class CheckSearchesResult(_Output):
    """What is new for the saved searches, one entry per search."""

    checked: int = Field(description="Searches fully checked.")
    total: int = Field(description="Searches considered.")
    complete: bool = Field(description="True when no search needs another call.")
    results: list[SearchCheck]
    note: str | None = Field(default=None, description="What to do when not complete.")
    provenance: Provenance


# --- verify_citation ---------------------------------------------------------------------------

# The longest quote accepted. Matching cost grows with the quote, and a quote is a few sentences.
MAX_QUOTE_CHARS = 5000


class CitationRequest(BaseModel):
    """One citation to check: a law, an article of it, and optionally words attributed to it."""

    law: str = Field(
        min_length=1,
        description="Name, number or BOE identifier of the law, e.g. 'Ley 40/2015'.",
    )
    article: str | None = Field(
        default=None,
        min_length=1,
        description="The article or provision, e.g. 'artículo 48' or 'disposición adicional "
        "tercera'. Omit when block_id is given.",
    )
    block_id: str | None = Field(
        default=None,
        min_length=1,
        description="Exact block of the law, from the candidates of an ambiguous article; used "
        "instead of article.",
    )
    quote: str | None = Field(
        default=None,
        max_length=MAX_QUOTE_CHARS,
        description="Words attributed to it, to compare with the text. Use (...) for omissions.",
    )
    as_of: date | None = Field(
        default=None, description="Date the citation refers to; default today."
    )


class QuoteMatchInfo(_Output):
    """How closely the quote matches the text of one version."""

    level: MatchLevel = Field(
        description="'exact': the quoted words appear in the text. 'close': a few words differ; "
        "one word can change the meaning, see differences. 'partial': shares words with the text "
        "but much differs; the quote may come from elsewhere. 'none': not found. Case, "
        "punctuation and quotation marks are ignored; accents are not."
    )
    similarity: float = Field(description="0.0 to 1.0, share of the words that match.")
    matched_text: str = Field(
        description="The passage of the text the quote was matched to, as written (at most 600 "
        "characters); empty for 'none'."
    )
    differences: str = Field(
        description="Quote against passage: [-words only in the quote-] {+words only in the "
        "text+}; empty when exact or none."
    )
    flags: list[str] = Field(
        default_factory=list,
        description="'negation' or 'number' when a differing word is a negation (no, sin, "
        "salvo...) or a number; 'omitted_negation' or 'omitted_number' when so is an omitted "
        "passage.",
    )
    omitted: list[str] = Field(
        default_factory=list,
        description="Text of the article skipped at each omission mark; read it before relying "
        "on the quote.",
    )

    # Why a serializer: both lists are empty for most matches, and the model reads every one.
    @model_serializer(mode="wrap")
    def _omit_empty_lists(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Serialise as usual, then drop ``flags`` and ``omitted`` when they are empty."""
        data: dict[str, object] = handler(self)
        for key in ("flags", "omitted"):
            if not data.get(key):
                data.pop(key, None)
        return data


class OtherVersionInfo(_Output):
    """Another version of the article that matches the quote better than the one in effect."""

    relation: Literal["earlier", "later"] = Field(
        description="Whether that version starts before or after the date."
    )
    starts_on: date | None
    ends_on: date | None = Field(
        description="Last day of that version; None when it is the latest."
    )
    level: MatchLevel
    similarity: float
    matched_text: str = Field(description="The passage of that version, as written (at most 600).")
    differences: str = Field(description="Quote against that version; empty when exact.")


def _without_none(data: dict[str, object], keep: tuple[str, ...] = ()) -> dict[str, object]:
    """Drop the ``None`` values of a serialised model except for the keys in ``keep``."""
    return {key: value for key, value in data.items() if value is not None or key in keep}


class CitationCheck(_Output):
    """The facts found for one citation."""

    law: str = Field(description="The law as given.")
    article: str | None = Field(default=None, description="The article as given.")
    status: CitationStatus | None = Field(
        description="'matches': found, in force, and the quote is exact. 'in_force': found and "
        "in force, no quote given. 'matches_older_version' / 'matches_later_version': the quote "
        "is exact in another version, not in the one in effect on as_of (see other_version). "
        "'text_differs': the quote is close or partial. 'quote_not_found'. 'not_in_force': the "
        "article, or the whole law, is not in force on as_of. 'article_not_found', "
        "'article_ambiguous', 'law_not_found', 'law_ambiguous' (see candidates). Null when not "
        "checked (see note)."
    )
    law_id: str | None = Field(default=None, description="BOE identifier of the law found.")
    law_title: str | None = None
    block_id: str | None = Field(default=None, description="Block of the article found.")
    article_label: str | None = Field(
        default=None, description="The article's title in the law, e.g. 'Artículo 48'."
    )
    state: ArticleState | None = Field(
        default=None,
        description="On as_of: in_force, repealed, not_yet_in_force or did_not_exist.",
    )
    law_repealed: bool | None = Field(
        default=None,
        description="True when the whole law is repealed; None when that could not be checked.",
    )
    as_of: date | None = Field(default=None, description="The date the check refers to.")
    quote_match: QuoteMatchInfo | None = Field(
        default=None,
        description="The quote against the version in effect on as_of; only when a quote was "
        "given and the article found.",
    )
    other_version: OtherVersionInfo | None = Field(
        default=None,
        description="The version that matches the quote better than the one in effect, if any.",
    )
    candidates: list[LawBrief | ArticleCandidate] = Field(
        default_factory=list,
        description="At most 5 laws (law_ambiguous) or articles (article_ambiguous, "
        "article_not_found) that may be meant; pass an article's block_id.",
    )
    note: str | None = Field(
        default=None,
        description="Ignored parts of the wording, a repeal of the whole law, or why the "
        "citation was not checked.",
    )

    # Why a serializer: most of these fields are empty for most citations, and the model reads
    # every one. ``status`` stays because a null there means "not checked".
    @model_serializer(mode="wrap")
    def _omit_empty(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Serialise as usual, then drop the fields that are ``None`` or an empty list."""
        data = _without_none(handler(self), keep=("status",))
        if not data.get("candidates"):
            data.pop("candidates", None)
        return data


class VerifyCitationResult(_Output):
    """One check per citation, in the order given."""

    results: list[CitationCheck]
    checked: int = Field(description="Citations that got a status.")
    not_checked: int = Field(
        description="Citations left unchecked (time limit or BOE error); call again with them."
    )
    provenance: Provenance


# --- explore_amendment_chain -------------------------------------------------------------------


class ChainNodeInfo(_Output):
    """One law in the chain."""

    law_id: str
    depth: int = Field(description="Distance from the starting law (0).")
    title: str | None = Field(
        default=None,
        description="From the collection, when it has the law; cut at 100 characters (80 from "
        "level 2 on).",
    )
    rank: str | None = Field(default=None, description="Starting law and first level only.")
    published_on: date | None = Field(
        default=None, description="Starting law and first level only."
    )
    in_collection: bool = Field(
        default=True,
        description="Omitted when true. False for norms outside the consolidated collection "
        "(corrections, court proceedings, regional decrees...); their own amendments cannot be "
        "followed.",
    )
    expanded: bool = Field(
        default=True, description="Omitted when true. False when its amendments were not followed."
    )
    reason: Literal["depth_limit", "not_fetched", "unavailable"] | None = Field(
        default=None,
        description="Why it was not expanded: depth_limit, not_fetched (call again) or "
        "unavailable.",
    )

    # Why a serializer: nearly every node is in the collection and expanded, and the model reads
    # all of them, so the two flags are given only when they differ from that common case.
    @model_serializer(mode="wrap")
    def _omit_empty(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Serialise as usual, then drop the optional fields that are ``None`` or true."""
        data = _without_none(handler(self))
        for key in ("in_collection", "expanded"):
            if data.get(key) is True:
                data.pop(key)
        return data


class ChainEdgeInfo(_Output):
    """One amendment between two laws."""

    amending: str = Field(description="Identifier of the norm that acts.")
    amended: str = Field(description="Identifier of the norm acted on.")
    kind: RelationKind
    detail: str | None = Field(
        default=None,
        description="Reference text from the BOE (Spanish), cut at 120 characters; it carries "
        "the BOE's wording of the relation. Given as the details argument says.",
    )

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Serialise as usual, then drop ``detail`` when it was not asked for."""
        return _without_none(handler(self))


class AmendmentChainResult(_Output):
    """Laws linked to a starting law by amendments, over several levels."""

    law_id: str
    direction: Direction
    depth: int
    nodes: list[ChainNodeInfo] = Field(description="By level, starting law first; at most 100.")
    edges: list[ChainEdgeInfo] = Field(description="At most 250, in order of discovery.")
    nodes_total: int = Field(description="All laws found; more than len(nodes) when cut.")
    edges_total: int = Field(description="All edges found; more than len(edges) when cut.")
    levels: list[int] = Field(description="Number of laws at each level, starting law first.")
    examined: int = Field(description="Laws whose amendments were read, cached or not.")
    pending: int = Field(description="Laws still to read.")
    failed: int = Field(description="Laws skipped after a temporary BOE error; they are pending.")
    complete: bool = Field(description="True when nothing is pending.")
    truncated: bool = Field(description="True when the 150-law limit cut the chain.")
    court_rulings_excluded: int = Field(
        default=0,
        description="Court rulings on the starting law left out because include is "
        "'amendments'; see note. Use include='all' to see them.",
    )
    note: str | None = Field(default=None, description="What to do when not complete.")
    provenance: Provenance
