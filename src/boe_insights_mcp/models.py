"""Typed, immutable models of the BOE API data, with English names.

The API uses Spanish field names and a few serialisation quirks (``yyyymmdd`` dates, ``S``/``N``
flags, single-element lists collapsed into objects, empty lists as ``""``). The models absorb
all of that: they validate from the raw API objects and dump with English names.

Public names: ``LawSummary`` (a search hit), ``LawMetadata`` (a summary plus repeal status),
``LawAnalysis`` with ``LawReference`` (subjects and amendment cross-references), ``CodedValue``
and the ``as_list`` helper. The text models are ``IndexEntry`` (one row of a law's block index),
``Paragraph``, ``BlockVersion`` and ``Block`` (one block of text with all its versions); the
latter three are built by ``textparse`` from the XML of ``/texto/bloque`` and of the whole-law
``/texto`` (api-notes 6 and "Text structure" of the third probe). ``DailySummary``,
``GazetteIssue`` and ``GazetteItem`` describe one day's gazette and are built by ``summaries``
from the XML of ``/boe/sumario`` (api-notes 8 and "Verified after the fourth probe").
``client`` builds these from response bodies; ``legislation`` consumes them and converts them to
the tool-facing ``schemas``. They are not sent to the model directly. Quirks are described in
``docs/api-notes.md`` section 0 and the sections cited below.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import (
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

__all__ = [
    "Block",
    "BlockVersion",
    "CodedValue",
    "DailySummary",
    "GazetteIssue",
    "GazetteItem",
    "IndexEntry",
    "LawAnalysis",
    "LawMetadata",
    "LawReference",
    "LawSummary",
    "Paragraph",
    "StrippedText",
    "as_list",
    "collapse_whitespace",
    "ellipsize",
    "strip_or_none",
]


def as_list(value: Any) -> list[Any]:
    """Normalise the API's collapsed collections: ``None``, ``""``, one object or a list."""
    # The API converts XML to JSON, so a collection with exactly one element arrives as a bare
    # object and an empty one as ``""`` or null (api-notes 0). Callers must always get a list.
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def collapse_whitespace(value: str) -> str:
    """Collapse every run of whitespace, non-breaking spaces included, into one space."""
    # ``str.split()`` with no argument treats U+00A0 as whitespace, and the API uses NBSP in
    # titles such as ``Artículo\xa046`` (api-notes 6), so one call handles both.
    return " ".join(value.split())


def strip_or_none(value: str | None) -> str | None:
    """Strip a text; an empty or missing one becomes ``None``."""
    stripped = (value or "").strip()
    return stripped or None


def ellipsize(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, the last one being "…" when cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _normalise_title(value: Any) -> Any:
    """Normalise a block title; a missing title (blocks without one) becomes ``""``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return collapse_whitespace(value)
    return value


def _blank_to_none(value: Any) -> Any:
    """Turn the API's empty elements (``""``) into ``None``."""
    # An empty XML element is serialised as ``""`` whatever its type, which would fail
    # validation as a date or as a nested object.
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _parse_date(value: Any) -> Any:
    """Parse a ``yyyymmdd`` string; blank means no date. Other values are left to pydantic.

    A malformed date raises ``ValueError``, which pydantic reports as a validation error.
    """
    value = _blank_to_none(value)
    if isinstance(value, str):
        return datetime.strptime(value, "%Y%m%d").date()
    return value


def _parse_timestamp(value: Any) -> Any:
    """Parse a ``yyyymmddTHHMMSSZ`` UTC stamp into an aware datetime."""
    value = _blank_to_none(value)
    if isinstance(value, str):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return value


def _parse_flag(value: Any) -> Any:
    """Parse the API's ``"S"``/``"N"`` (yes/no) flags.

    Any other string is rejected rather than guessed at, so an unexpected API value surfaces
    as a parse error instead of a silently wrong legal status.
    """
    if isinstance(value, str):
        if value not in ("S", "N"):
            raise ValueError(f"expected 'S' or 'N', got {value!r}")
        return value == "S"
    return value


def _unwrapper(key: str) -> Callable[[Any], list[Any]]:
    """Build a validator flattening ``[{key: item-or-items}, ...]`` wrappers into a list.

    Items that are not wrapped (no ``key``) are kept as they are.
    """
    # Shape being flattened (api-notes 5): ``referencias.anteriores`` is
    # ``[{"anterior": [ref, ref, ...]}, ...]`` and ``materias`` is ``[{"materia": {...}}]``,
    # each level collapsing to a bare object when it has one element. Every combination of
    # list/object at both levels must come out as one flat list.

    def unwrap(value: Any) -> list[Any]:
        items: list[Any] = []
        for entry in as_list(value):
            if isinstance(entry, Mapping) and key in entry:
                items.extend(as_list(entry[key]))
            else:
                items.append(entry)
        return items

    return unwrap


StrippedText = Annotated[str, StringConstraints(strip_whitespace=True)]
"""A string without surrounding whitespace."""

_OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
_OptionalDate = Annotated[date | None, BeforeValidator(_parse_date)]
_Flag = Annotated[bool, BeforeValidator(_parse_flag)]
_Title = Annotated[str, BeforeValidator(_normalise_title)]


class _ApiModel(BaseModel):
    """Frozen base: reads Spanish API names or English names, ignores unknown fields."""

    # ``extra="ignore"``: the API returns many fields we do not use, and new ones must not break
    # parsing. ``validate_by_name`` lets code and tests build models with the English names.

    model_config = ConfigDict(frozen=True, validate_by_name=True, extra="ignore")


class CodedValue(_ApiModel):
    """A ``{"codigo": ..., "texto": ...}`` pair, such as a rank or a department."""

    code: str = Field(validation_alias="codigo")
    text: StrippedText = Field(validation_alias="texto")


_OptionalCoded = Annotated[CodedValue | None, BeforeValidator(_blank_to_none)]


class LawSummary(_ApiModel):
    """One search hit of the consolidated legislation."""

    identifier: str = Field(validation_alias="identificador")
    title: str = Field(validation_alias="titulo")
    rank: _OptionalCoded = Field(default=None, validation_alias="rango")
    department: _OptionalCoded = Field(default=None, validation_alias="departamento")
    scope: _OptionalCoded = Field(default=None, validation_alias="ambito")
    official_number: _OptionalText = Field(default=None, validation_alias="numero_oficial")
    enacted_on: _OptionalDate = Field(default=None, validation_alias="fecha_disposicion")
    published_on: _OptionalDate = Field(default=None, validation_alias="fecha_publicacion")
    effective_from: _OptionalDate = Field(default=None, validation_alias="fecha_vigencia")
    updated_at: Annotated[datetime | None, BeforeValidator(_parse_timestamp)] = Field(
        default=None, validation_alias="fecha_actualizacion"
    )
    validity_exhausted: _Flag = Field(default=False, validation_alias="vigencia_agotada")
    consolidation_state: _OptionalCoded = Field(
        default=None, validation_alias="estado_consolidacion"
    )
    gazette: _OptionalText = Field(default=None, validation_alias="diario")
    gazette_number: _OptionalText = Field(default=None, validation_alias="diario_numero")
    eli_url: _OptionalText = Field(default=None, validation_alias="url_eli")
    html_url: _OptionalText = Field(default=None, validation_alias="url_html_consolidada")


class LawMetadata(LawSummary):
    """The metadata of one law, which adds the repeal and annulment status to a summary."""

    # Only ``/metadatos`` carries these; search hits do not (api-notes 1, 4). The repeal flag
    # means "wholly repealed", not "superseded" (api-notes 4).

    repealed: _Flag = Field(default=False, validation_alias="estatus_derogacion")
    repealed_on: _OptionalDate = Field(default=None, validation_alias="fecha_derogacion")
    annulled: _Flag = Field(default=False, validation_alias="estatus_anulacion")


class LawReference(_ApiModel):
    """One amendment cross-reference between two norms."""

    law_id: str = Field(validation_alias="id_norma")
    relation: CodedValue = Field(validation_alias="relacion")
    text: str = Field(validation_alias="texto")


class LawAnalysis(_ApiModel):
    """The subjects, notes and amendment references of one law."""

    subjects: Annotated[list[CodedValue], BeforeValidator(_unwrapper("materia"))] = Field(
        default_factory=list, validation_alias="materias"
    )
    notes: Annotated[list[str], BeforeValidator(_unwrapper("nota"))] = Field(
        default_factory=list, validation_alias="notas"
    )
    earlier: Annotated[list[LawReference], BeforeValidator(_unwrapper("anterior"))] = Field(
        default_factory=list, validation_alias=AliasPath("referencias", "anteriores")
    )
    later: Annotated[list[LawReference], BeforeValidator(_unwrapper("posterior"))] = Field(
        default_factory=list, validation_alias=AliasPath("referencias", "posteriores")
    )


class IndexEntry(_ApiModel):
    """One row of a law's block index (``/texto/indice``)."""

    block_id: str = Field(validation_alias="id")
    title: _Title = Field(default="", validation_alias="titulo")
    updated_on: _OptionalDate = Field(default=None, validation_alias="fecha_actualizacion")


class Paragraph(_ApiModel):
    """One paragraph of a block version, as plain text."""

    kind: str
    """The element's ``class`` (``articulo``, ``parrafo``, ``nota_pie``...), else its tag name."""
    text: str
    quoted: bool = False
    """True when the paragraph sat inside a ``<blockquote>`` (footnotes and citations)."""


def _is_footnote(paragraph: Paragraph) -> bool:
    """Whether a paragraph is a footnote (``nota_pie``, ``nota_pie_2``...)."""
    # ``nota_inicial`` and ``siempreSeVe`` are body text (the note that opens a repealed law),
    # so the test is the ``nota_pie`` prefix and not "looks like a note" (api-notes 6.1).
    return paragraph.kind.startswith("nota_pie")


class BlockVersion(_ApiModel):
    """The complete text of a block as amended by one norm, from ``effective_from`` on."""

    source_law_id: str
    """``id_norma``: the norm that introduced this version."""
    published_on: date | None
    effective_from: date | None
    paragraphs: tuple[Paragraph, ...]

    @property
    def starts_on(self) -> date | None:
        """When this version starts to apply: ``effective_from``, else ``published_on``."""
        # Why the fallback: ``fecha_vigencia`` is sometimes empty on a first or last version while
        # ``fecha_publicacion`` is present, and versions are not reliably in order in the XML, so
        # ordering and selection need one date that is almost always there (api-notes 6.1).
        return self.effective_from or self.published_on

    @property
    def start_key(self) -> tuple[bool, date]:
        """Sort key placing versions without any start date before dated ones."""
        return (self.starts_on is not None, self.starts_on or date.min)

    @property
    def body(self) -> tuple[Paragraph, ...]:
        """The paragraphs that are not footnotes, quoted ones included."""
        return tuple(p for p in self.paragraphs if not _is_footnote(p))

    @property
    def own_paragraphs(self) -> tuple[Paragraph, ...]:
        """The paragraphs that are the law's own words: not footnotes, not quoted notes."""
        # Quoted paragraphs ("Téngase en cuenta...", "Redacción anterior:" citations, and the
        # footnotes inside a blockquote) and ``nota_pie`` footnotes are the BOE's notes about the
        # text, not the text (api-notes 6.1, "Blockquote").
        return tuple(p for p in self.body if not p.quoted)

    @property
    def editorial_paragraphs(self) -> tuple[Paragraph, ...]:
        """The paragraphs the BOE adds about the text: footnotes and quoted notes."""
        return tuple(p for p in self.paragraphs if _is_footnote(p) or p.quoted)

    @property
    def text(self) -> str:
        """The body paragraphs joined by blank lines; quoted ones are prefixed with ``> ``."""
        return "\n\n".join(f"> {p.text}" if p.quoted else p.text for p in self.body)

    @property
    def notes(self) -> tuple[str, ...]:
        """The texts of the footnote paragraphs, in order."""
        return tuple(p.text for p in self.paragraphs if _is_footnote(p))


class Block(_ApiModel):
    """One block of a law's text (article, heading, provision...) with all its versions."""

    block_id: str
    kind: str
    """``tipo`` (``precepto``, ``nota_inicial``, ``preambulo``...), ``""`` when absent."""
    title: _Title
    versions: tuple[BlockVersion, ...]
    """Ascending by ``starts_on`` (undated first), document order kept for ties."""
    expires_on: date | None = None
    """The ``fecha_caducidad`` of the block when it is a real date: the publication date of the
    law that removed it, not the day the removal took effect (api-notes, third probe, "What
    fecha_caducidad means")."""
    expired_marker: bool = False
    """True when the block carries a ``fecha_caducidad`` at all, a real date or not. It is also
    set for the sentinel ``00000001`` and for any other non-date value, which leave
    ``expires_on`` empty."""


class GazetteItem(_ApiModel):
    """One entry of a daily summary: a disposition, an appointment, a grant or an announcement."""

    identifier: str
    """``BOE-A-...`` (sections 1, 2A, 2B, 3, T) or ``BOE-B-...`` (sections 4, 5A, 5B, 5C)."""
    title: str
    """The title with whitespace collapsed; the Spanish text is otherwise untouched."""
    section_code: str
    """``1``, ``2A``, ``2B``, ``3``, ``4``, ``5A``, ``5B``, ``5C``, ``T``... (the names of the
    sections change with the era, the codes do not)."""
    section_name: str
    department_code: str | None
    """``None`` where the source has an empty code (announcements of 1960)."""
    department_name: str | None
    heading: str | None
    """The name of the epígrafe the item sits under, when there is one."""
    issue_number: str
    """The ``numero`` of the issue the item belongs to."""
    first_page: int | None
    last_page: int | None
    pdf_url: str | None
    html_url: str | None
    """Missing on some announcements before 2009."""


class GazetteIssue(_ApiModel):
    """One issue (``diario``) of the gazette."""

    number: str
    """The issue number within the year, e.g. ``67``."""
    identifier: str
    """The summary identifier, e.g. ``BOE-S-2020-67``."""


class DailySummary(_ApiModel):
    """Everything one day's summary lists."""

    day: date
    issues: tuple[GazetteIssue, ...]
    """In the order the API gives them; a day can have two (extraordinary issue listed first)."""
    items: tuple[GazetteItem, ...]
    """In document order, across all issues."""
