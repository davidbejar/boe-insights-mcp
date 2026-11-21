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

from pydantic import BaseModel, ConfigDict, Field

from boe_insights_mcp.structure import EntryKind
from boe_insights_mcp.versions import ArticleState

__all__ = [
    "AmendedLaw",
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
    "CodeMatch",
    "CompareArticleVersionsResult",
    "CompareTopicsResult",
    "Coverage",
    "DepartmentCount",
    "ExplainSpikeResult",
    "FindCodesResult",
    "FirstPublication",
    "GroupSeries",
    "LawBrief",
    "LawOverview",
    "LawStatus",
    "LawStructureResult",
    "LegislationOverTimeResult",
    "MostAmendedLawsResult",
    "PeriodCount",
    "PeriodSpike",
    "Provenance",
    "ReadArticleResult",
    "ReferenceBrief",
    "RelationCount",
    "ResolveReferenceResult",
    "SearchLegislationResult",
    "SpikeContribution",
    "StructureItem",
    "TopDepartmentsResult",
    "TopicSpec",
    "TopicTrend",
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
        description="Words that must all be in the title, as exact word forms ('vivienda' does "
        "not match 'viviendas'); use text for laws that treat a topic without naming it.",
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
