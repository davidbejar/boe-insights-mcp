"""The table of contents of a law, and the first-publication note of regional laws. Pure, no I/O.

``build_structure`` turns the flat list of ``Block`` models of a whole law (``textparse.
parse_full_text``) into the outline a reader would see: books, titles, chapters, sections,
articles and provisions, each with a label, a heading, a nesting level and a state on a date.
``parse_publication_note`` reads "Publicada en el BOPA núm. 206, de 23 de octubre de 2026." from
the notes of a regional law. ``articles`` (via ``legislation``) builds the tool results on top of
the first, ``legislation.get_law_overview`` uses the second; both lean on ``versions`` to pick
the text in force on a date.

The BOE gives no tree: ``/texto`` is a flat list of blocks and the hierarchy is implied by their
order (api-notes, third probe, "Text structure"). The rules below are therefore a reconstruction
from observed data, and they are deliberately forgiving: an irregular block never raises, it
becomes an entry of kind ``other``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from boe_insights_mcp.models import Block, BlockVersion, Paragraph
from boe_insights_mcp.versions import MONTHS, ArticleState, state_on, version_on

__all__ = [
    "EntryKind",
    "GazettePublication",
    "StructureEntry",
    "build_structure",
    "parse_publication_note",
]

EntryKind = Literal[
    "book",
    "title",
    "chapter",
    "section",
    "subsection",
    "group",
    "annex",
    "article",
    "provision",
    "preamble",
    "other",
]


@dataclass(frozen=True)
class StructureEntry:
    """One line of a law's table of contents."""

    block_id: str
    kind: EntryKind
    level: int
    """1 = top level; an entry's level is its parent heading's level + 1."""
    label: str
    """The designation: ``TÍTULO I``, ``Artículo 27``, ``Disposición final cuarta``."""
    heading: str
    """The subject line that follows the designation; ``""`` when there is none."""
    state: ArticleState


@dataclass(frozen=True)
class GazettePublication:
    """Where and when a regional law was first published."""

    gazette: str
    """The gazette's abbreviation, e.g. ``BOPA``."""
    number: str | None
    """The issue number; always set by the current pattern, optional for other wordings."""
    date: date


# Fixed nesting ranks of the headings that form a hierarchy. A lower rank encloses a higher one.
# Groups and annexes have no rank: they are top-level islands (see ``build_structure``).
_RANKS: dict[EntryKind, int] = {"book": 1, "title": 2, "chapter": 3, "section": 4, "subsection": 5}

# Heading classes that come as a ``*_num`` / ``*_tit`` pair of paragraphs, in precedence order
# a block can mix them (api-notes, third probe, "Text structure"), and then it is read as the
# outermost one.
_PAIRED_HEADINGS: tuple[tuple[str, EntryKind], ...] = (
    ("libro", "book"),
    ("titulo", "title"),
    ("capitulo", "chapter"),
)
# Heading classes written as one paragraph, which decide the kind when no pair is present.
_SINGLE_HEADINGS = frozenset({"seccion", "subseccion", "anexo", "capitulo"})

# Spanish ordinals for the designation split ("Sección primera. De la tutela"), with and without
# accents because the BOE text is not consistent. Matched case-insensitively.
_SPELLED_ORDINAL = (
    r"primer[oa]?|segund[oa]|tercer[oa]?|cuart[oa]|quint[oa]|sext[oa]|s[eé]ptim[oa]|octav[oa]"
    r"|noven[oa]|d[eé]cim[oa]|und[eé]cim[oa]|duod[eé]cim[oa]|[uú]nic[oa]"
)
# A designation word and its ordinal at the start of a heading. Digits take an optional ``.ª`` or
# ``.º`` suffix ("1.ª") and a Roman numeral must be upper case, so that a word that merely starts
# with I, V, X, L, C, D or M ("Comunes") is not read as one; the ordinal must be followed by the
# end, a space or a period, never by more letters.
_DESIGNATION = re.compile(
    r"""
    (?P<label>
        (?:(?i:secci[oó]n|subsecci[oó]n|cap[ií]tulo)\s+|§\s*)
        (?:\d+(?:\.?[ªº])?|[IVXLCDM]+|(?i:"""
    + _SPELLED_ORDINAL
    + r"""))
    )
    (?![^\W_]|[ªº])
    (?:\.?\s*(?P<rest>.*))?$
    """,
    re.VERBOSE | re.DOTALL,
)
# The end of an article's designation: a period followed by a space or the end. A period inside
# a number ("Artículo 1.902. Culpa") is followed by a digit and so does not end it.
_DESIGNATION_END = re.compile(r"\.(?:\s+|$)")
_ABBREVIATED_ARTICLE = re.compile(r"arts?\.\s*", re.IGNORECASE)
# What an article's label starts with: "Artículo", "Artículos", and the Código Civil's "Art",
# "Art.", "Arts" and "Arts." (with or without the period; api-notes, "Index title patterns").
_ARTICLE_LABEL = re.compile(r"(?:art[íi]culos?|arts?\.?)(?![^\W\d_])", re.IGNORECASE)

# "Publicada en el BOPA núm. 206, de 23 de octubre de 2026." (api-notes, third probe, "Regional
# laws and publication dates"). Only the abbreviation is case-sensitive: it starts with a capital
# and has at least two, so that a stray word ("Bopa", "bopa") is not taken for a gazette but
# "BOCyL" is. The issue number may have a thousands dot ("7.345") and is kept as written.
_PUBLICATION = re.compile(
    r"(?i:publicada\s+en\s+el)\s+(?P<gazette>(?=[A-Z][a-z]*[A-Z])[A-Z][A-Za-z]*)\s+"
    r"(?i:n[úu]m(?:ero)?\.?)\s*(?P<number>\d+(?:\.\d+)*)\s*,"
    r"\s*(?i:de)\s+(?P<day>\d{1,2})\s+(?i:de)\s+(?P<month>[^\W\d_]+)\s+(?i:de)\s+(?P<year>\d{4})"
    r"(?!\d)"
)


def build_structure(blocks: Sequence[Block], on: date) -> list[StructureEntry]:
    """The table of contents of a law as it stands on ``on``, in reading order.

    One entry per block that exists on that date (see the rules in the module's tests):
    signatures and initial notes are left out, so are blocks that had no text yet or
    that have expired; preambles come first. Never raises for odd blocks. Runs in one pass over
    the blocks, so a code with thousands of them is cheap.
    """
    entries: list[StructureEntry] = []
    preambles: list[StructureEntry] = []
    # Open headings that can still be a parent, outermost first, as (rank, level). A new heading
    # pops every open heading of the same or a deeper rank, so the stack is the path from the top
    # of the outline to the current heading and each push or pop happens once per block.
    open_headings: list[tuple[int, int]] = []
    # The nearest preceding heading of any kind, which is what articles hang from: (kind, level).
    last_heading: tuple[EntryKind, int] | None = None

    for block in blocks:
        if block.kind in ("firma", "nota_inicial") or _has_expired(block, on):
            continue
        state = state_on(block, on)
        if state == "did_not_exist":  # also covers a block without any version
            continue
        # A block that is published but takes effect later has no version in effect: its first
        # version is the upcoming text, which is what a table of contents should show.
        version = version_on(block, on) or block.versions[0]

        if block.kind in ("preambulo", "parte_dispositiva"):
            label = "Preámbulo" if block.kind == "preambulo" else "Parte dispositiva"
            preambles.append(StructureEntry(block.block_id, "preamble", 1, label, "", state))
            continue

        if block.kind == "encabezado" or (block.kind == "precepto" and _is_heading(version)):
            kind, label, heading = _read_heading(block, version)
            if kind in _RANKS:
                rank = _RANKS[kind]
                while open_headings and open_headings[-1][0] >= rank:
                    open_headings.pop()
                level = open_headings[-1][1] + 1 if open_headings else 1
                open_headings.append((rank, level))
                last_heading = (kind, level)
            elif kind in ("group", "annex"):
                # Groups ("DISPOSICIONES ADICIONALES") and annexes start a new top-level part of
                # the text, so whatever was open before them is closed.
                open_headings.clear()
                level = 1
                last_heading = (kind, level)
            else:  # "other": placed like an article and never a parent
                level = _below(last_heading)
        elif block.kind == "precepto":
            kind, label, heading = _read_precept(block, version)
            level = _precept_level(kind, label, last_heading)
        else:
            # An unknown ``tipo`` is kept rather than dropped, and placed like an article.
            kind, label, heading = "other", block.title or _first_text(version), ""
            level = _below(last_heading)
        entries.append(StructureEntry(block.block_id, kind, level, label, heading, state))

    # The Código Civil lists its enacting text and signature after the first blocks of the law
    # proper (api-notes, "Text structure": block order is not document order), so preambles are
    # moved to the front; everything else keeps the order the BOE gave.
    return preambles + entries


def _has_expired(block: Block, on: date) -> bool:
    """Whether the block no longer exists on ``on`` according to its ``fecha_caducidad``."""
    if not block.expired_marker:
        return False
    # ``fecha_caducidad`` is the publication date of the law that removed the block, not the day
    # the removal took effect, so it can lie years before the last version starts (api-notes,
    # third probe, "What fecha_caducidad means"). Until that last version starts the block still
    # applies; from the later of the two dates it is gone. The Código Civil has 91 such blocks
    # that were still in force in 2015. The sentinel ``00000001`` gives no date at all, and the
    # last version's start is then the only evidence there is; with no start either, the block is
    # skipped for every date.
    last_start = max((v.starts_on for v in block.versions if v.starts_on is not None), default=None)
    if block.expires_on is not None:
        gone = max(block.expires_on, last_start) if last_start is not None else block.expires_on
    else:
        gone = last_start if last_start is not None else date.min
    return gone <= on


def _below(last_heading: tuple[EntryKind, int] | None) -> int:
    """The level of something that sits directly under the nearest heading (1 when none)."""
    return last_heading[1] + 1 if last_heading is not None else 1


def _precept_level(kind: EntryKind, label: str, last_heading: tuple[EntryKind, int] | None) -> int:
    """The level of an article or provision given the nearest preceding heading."""
    # A "Disposición ..." belongs under a group heading only when one precedes it. The Asturias
    # law and Ley 40/2015 have no heading block for their disposiciones, so theirs follow the last
    # chapter or article of the text and must not be nested under it: they are top level.
    if kind == "provision" and label.casefold().startswith("disposición"):
        if last_heading is not None and last_heading[0] == "group":
            return last_heading[1] + 1
        return 1
    return _below(last_heading)


# Paragraph classes that make a block a heading when no ``articulo`` paragraph is present.
_HEADING_CLASSES = frozenset(
    {
        "seccion",
        "subseccion",
        "capitulo",
        "capitulo_num",
        "capitulo_tit",
        "titulo_num",
        "titulo_tit",
        "libro_num",
        "libro_tit",
    }
)


def _is_heading(version: BlockVersion) -> bool:
    """Whether a ``precepto`` block is really a heading: heading paragraphs, no ``articulo``."""
    # The Código Civil types headings such as ``sprimera-2`` ("Sección 1.ª ...") and ``tx``
    # ("TÍTULO X") as ``precepto`` (api-notes, third probe, "Text structure"). Without this they
    # would be listed as provisions and break the nesting of everything after them.
    kinds = {p.kind for p in version.paragraphs}
    return "articulo" not in kinds and not kinds.isdisjoint(_HEADING_CLASSES)


def _first_text(version: BlockVersion) -> str:
    """The text of the first paragraph of a version, ``""`` when it has none."""
    return version.paragraphs[0].text.strip() if version.paragraphs else ""


def _first_of_kind(version: BlockVersion, *kinds: str) -> Paragraph | None:
    """The first paragraph of the version whose class is one of ``kinds``."""
    return next((p for p in version.paragraphs if p.kind in kinds), None)


def _read_heading(block: Block, version: BlockVersion) -> tuple[EntryKind, str, str]:
    """Kind, label and heading of an ``encabezado`` block, from the version in effect."""
    # Number/title pairs win over single paragraphs: ``tiii-3`` of the Código Civil has a
    # ``titulo_num`` + ``titulo_tit`` and also a ``seccion`` paragraph, and it is a title.
    for prefix, kind in _PAIRED_HEADINGS:
        number = _first_of_kind(version, f"{prefix}_num")
        title = _first_of_kind(version, f"{prefix}_tit")
        if number is not None or title is not None:
            return kind, number.text.strip() if number else "", title.text.strip() if title else ""
    single = next((p for p in version.paragraphs if p.kind in _SINGLE_HEADINGS), None)
    if single is not None:
        text = single.text.strip()
        if single.kind == "seccion":
            return ("section", *_split_designation(text))
        if single.kind == "subseccion":
            return ("subsection", *_split_designation(text))
        if single.kind == "anexo":
            return "annex", text, ""
        # A ``capitulo`` paragraph is a real chapter when it says so ("CAPÍTULO ÚNICO", a chapter
        # of a title with no other chapters) and a grouping otherwise ("DISPOSICIONES
        # ADICIONALES", whose blocks have no heading of their own). Both use the same class.
        if text.casefold().startswith(("capítulo", "capitulo")):
            return ("chapter", *_split_designation(text))
        return "group", text, ""
    return "other", block.title or _first_text(version) or block.block_id, ""


def _split_designation(text: str) -> tuple[str, str]:
    """Split "Sección 1.ª De la guarda" into ("Sección 1.ª", "De la guarda").

    Without a recognised designation word and ordinal the whole text is the label.
    """
    match = _DESIGNATION.match(text)
    if match is None:
        return text, ""
    # A period right after a spelled ordinal or a Roman numeral ("Sección primera. De...") is
    # punctuation, not part of the label; the pattern consumes it, and ``1.ª`` keeps its own.
    return match["label"], _trim_period(match["rest"] or "")


def _trim_period(text: str) -> str:
    """Strip whitespace and one trailing period (but not an ellipsis)."""
    text = text.strip()
    if text.endswith(".") and not text.endswith("..."):
        text = text[:-1]
    return text.rstrip()


def _read_precept(block: Block, version: BlockVersion) -> tuple[EntryKind, str, str]:
    """Kind, label and heading of a ``precepto`` block (an article or a provision)."""
    paragraph = _first_of_kind(version, "articulo")
    if paragraph is None:
        label, heading = block.title or block.block_id, ""
    else:
        label, heading = _split_article(paragraph.text.strip())
    return ("article" if _ARTICLE_LABEL.match(label) else "provision"), label, heading


def _split_article(text: str) -> tuple[str, str]:
    """Split "Artículo 27. Garantía de la salud." into ("Artículo 27", "Garantía de la salud")."""
    # The abbreviations "Art. 1873 bis." and "Arts. 127 a 130." (a block standing for a range of
    # articles in the Código Civil) have a period of their own right after the word, which would
    # otherwise be taken for the end of the designation, so the search starts after it.
    abbreviation = _ABBREVIATED_ARTICLE.match(text)
    end = _DESIGNATION_END.search(text, abbreviation.end() if abbreviation else 0)
    if end is None:
        return text, ""
    return text[: end.start()].strip(), _trim_period(text[end.end() :])


def parse_publication_note(notes: Sequence[str]) -> GazettePublication | None:
    """The first-publication data found in the notes of a law, or ``None``.

    Scans every note for "Publicada en el <GAZETTE> núm. <N>, de <D> de <mes> de <YYYY>". The
    first match with a real calendar date wins; an impossible date is skipped, not an error.
    """
    # Every note is scanned, not just the first: ``/analisis`` merges all of a law's notes into
    # one list, and the publication note often follows others such as "Vigencia hasta..."
    # (api-notes, third probe, "Regional laws and publication dates").
    for note in notes:
        for match in _PUBLICATION.finditer(note):
            month = MONTHS.get(match["month"].casefold())
            if month is None:
                continue
            try:
                when = date(int(match["year"]), month, int(match["day"]))
            except ValueError:  # 31 de febrero and the like
                continue
            return GazettePublication(match["gazette"], match["number"], when)
    return None
