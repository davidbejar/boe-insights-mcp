"""Output models of the MCP tools (pydantic v2).

Every tool result embeds a :class:`Provenance`, which carries the attribution and the
"informational only" notice the BOE reuse conditions require.

These are the return types of the four tools in ``server``; ``legislation`` builds them from the
client's ``models`` types. FastMCP derives each tool's output schema from them, so the class
docstrings and ``Field`` descriptions here are sent to the language model and are model-facing
text (see CLAUDE.md, "Documentation standard"); notes for human readers go in ``#`` comments.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AmendmentSummary",
    "CodeMatch",
    "FindCodesResult",
    "LawBrief",
    "LawOverview",
    "LawStatus",
    "Provenance",
    "ReferenceBrief",
    "RelationCount",
    "ResolveReferenceResult",
    "SearchLegislationResult",
]

LawStatus = Literal[
    "in_force",
    "partially_repealed",
    "repealed",
    "annulled",
    "validity_exhausted",
    "not_yet_in_force",
]


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
    published_on: date | None = Field(default=None, description="Publication date in the BOE.")
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


class LawOverview(_Output):
    """Identity, legal status and amendment summary of one law."""

    identifier: str
    title: str
    rank: str | None = None
    official_number: str | None = None
    department: str | None = None
    scope: str | None = None
    enacted_on: date | None = Field(default=None, description="Date of the disposition.")
    published_on: date | None = None
    effective_from: date | None = None
    status: LawStatus = Field(
        description="Derived from the BOE's repeal, annulment and validity flags and from the "
        "later norms that repeal it; an indication, not a legal determination."
    )
    status_note: str = Field(description="Plain-English explanation of the status, with caveats.")
    repealed_on: date | None = None
    consolidation_state: str | None = None
    amendments: AmendmentSummary
    url: str | None = None
    eli_url: str | None = Field(default=None, description="European Legislation Identifier URL.")
    provenance: Provenance
