"""Find the block of a law's text index that a person means by "artículo 46 bis" or "DA 3".

The BOE text index lists blocks by an opaque id and a human title (``Artículo 46 bis`` is block
``a4-2``), so an article has to be found by title. The titles come in many shapes (see "Index
title patterns" in ``docs/api-notes.md``): bare ``Artículo 14``, ordinal words in old laws
(``Artículo primero``), the Código Civil's ``Art 1`` and range blocks (``Arts 127 a 130``),
provisions with or without an ordinal, annexes, empty and bracketed titles for the preamble.

The approach is one pipeline applied to the query and to every title alike:

1. ``_scan`` folds case and accents, drops punctuation and ordinal indicators and splits
   letters from digits, so ``Art.46bis``, ``ARTÍCULO 46 BIS.`` and ``art 46 bis`` give the same
   tokens; a thousands dot (``1.240``) stays inside one token. Each token remembers where it
   sat in the original text.
2. ``_parse_leading`` reads the leading tokens into a small structured ``_Designation`` (kind,
   provision type, number or range, Latin suffix). A query may carry more after the designation
   (``46.2``, ``14 de la Constitución``): that remainder is reported as ``ignored``. Text with no
   leading designation becomes an ``other`` designation compared by its whole normalised text.
3. ``locate`` compares designations, never strings, and ranks the closest titles when nothing
   matches.

The module is pure: no I/O, and it imports only from ``models`` and the standard library.
``articles`` calls it to turn the person's wording (or a block id) into a block of the index.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Literal

from boe_insights_mcp.models import IndexEntry

__all__ = ["LocateResult", "locate", "locate_block_id"]

_MAX_AMBIGUOUS = 10
_MAX_CANDIDATES = 8
# Similarity ranking compares the query with every title; bounding the query keeps that cheap
# even when a caller passes far more text than a designation can have.
_MAX_QUERY_TEXT = 200

# A position inside a law: (article number, Latin suffix rank). Rank 0 means no suffix, so
# (163, 0) < (163, 2) < (163, 4), which is the order of "163", "163 bis", "163 quater".
_Position = tuple[int, int]
_Kind = Literal["article", "provision", "annex", "preamble", "other"]


@dataclass(frozen=True)
class LocateResult:
    """The outcome of :func:`locate`."""

    outcome: Literal["found", "ambiguous", "not_found"]
    entry: IndexEntry | None
    """Set only when ``outcome`` is ``found``."""
    candidates: tuple[IndexEntry, ...]
    """``ambiguous``: every match (at most 10). ``not_found``: up to 8 closest titled blocks."""
    ignored: str | None = None
    """The part of the query after its leading designation that was not used, in the original
    wording (``.2`` for ``46.2``); ``None`` when nothing was left over."""


@dataclass(frozen=True)
class _Designation:
    """What a title or a query refers to, independent of how it was spelled."""

    kind: _Kind
    subtype: str = ""
    """For provisions, the type (``adicional``, ``transitoria``, ``derogatoria``, ``final``)."""
    start: _Position | None = None
    """The number; ``None`` for a sole article, a sole or ordinal-less provision or annex."""
    end: _Position | None = None
    """For range blocks (``Arts 127 a 130``), the last article; otherwise ``None``."""
    text: str = ""
    """For ``other``, the whole normalised text; empty for every other kind."""
    bare: bool = False
    """Nothing followed the provision type or ``anexo`` (as opposed to an explicit "única")."""

    @property
    def identity(self) -> tuple[object, ...]:
        """The fields that decide whether two designations mean the same block."""
        # ``bare`` is left out on purpose: "Disposición final" (no ordinal, in the Constitución)
        # and "Disposición final única" are the same provision.
        return (self.kind, self.subtype, self.start, self.end, self.text)


# --- Number words: table-driven and compositional ------------------------------------------

_UNITS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
}
_IRREGULAR_CARDINALS = {
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "veinte": 20,
}
_CARDINAL_TENS = {
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "setenta": 70,
    "ochenta": 80,
    "noventa": 90,
}
# "dieciséis" is dieci + seis and "veintidós" is veinti + dos.
_CARDINAL_PREFIXES = {"dieci": 10, "veinti": 20}

# Ordinal stems: the word without its gender ending ("tercero" and "tercera" both give "tercer").
_ORDINAL_UNITS = {
    "primer": 1,
    "segund": 2,
    "tercer": 3,
    "cuart": 4,
    "quint": 5,
    "sext": 6,
    "septim": 7,
    "octav": 8,
    "noven": 9,
}
_ORDINAL_TENS = {
    "decim": 10,
    "vigesim": 20,
    "trigesim": 30,
    "cuadragesim": 40,
    "quincuagesim": 50,
    "sexagesim": 60,
    "septuagesim": 70,
    "octogesim": 80,
    "nonagesim": 90,
}
_IRREGULAR_ORDINALS = {"undecim": 11, "duodecim": 12}

_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_ROMAN_NUMERAL = re.compile(r"c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})")

# Latin suffixes by rank. "cuarter" is the spelling the IVA law uses for "quater" (api-notes 5).
# Ranks above 9 are composed (``_latin_rank``): "terdecies" is "ter" + "decies" (13) and
# "quinvicies" is "quin" + "vicies" (25), which covers the whole family the IVA law goes through
# (up to "octovicies").
_LATIN_SUFFIXES = {
    "bis": 2,
    "ter": 3,
    "quater": 4,
    "cuarter": 4,
    "quinque": 5,
    "quinquies": 5,
    "sexies": 6,
    "septies": 7,
    "octies": 8,
    "nonies": 9,
    "novies": 9,
}
# "vivies" is a typo of the source: the IVA index has "Artículo 163 sexvivies" (api-notes
# "Index title patterns"); reading it as "vicies" makes "163 sexvicies" find that block.
_LATIN_TENS = {"decies": 10, "vicies": 20, "vivies": 20, "tricies": 30}
# What may stand in front of a tens word, by the unit it adds: "un", "duo", "ter"... and the
# fuller forms "quinquies", "sexies"... ("quinquiesdecies" next to "quinvicies").
_LATIN_UNITS = {
    "un": 1,
    "duo": 2,
    "ter": 3,
    "quater": 4,
    "cuarter": 4,
    "quin": 5,
    "quinque": 5,
    "quinquies": 5,
    "sex": 6,
    "sexies": 6,
    "sept": 7,
    "septies": 7,
    "oct": 8,
    "octo": 8,
    "octies": 8,
    "nov": 9,
    "novo": 9,
    "novies": 9,
    "non": 9,
    "nonies": 9,
}

_ARTICLE_HEADS = frozenset({"articulo", "articulos", "art", "arts"})
_RANGE_CONNECTORS = frozenset({"a", "al"})
_PROVISION_TYPES = frozenset({"adicional", "transitoria", "derogatoria", "final"})
_PROVISION_ABBREVIATIONS = {
    "da": "adicional",
    "dt": "transitoria",
    "dd": "derogatoria",
    "df": "final",
}
_SOLE_WORDS = frozenset({"unico", "unica"})
# Blocks with an empty title that are the preamble, by id (api-notes "Index title patterns").
_PREAMBLE_IDS = frozenset({"pr", "preambulo"})

# Returned when the query has a number but a block has none (``Artículo único``): ranks last.
_FAR = 10**6


def _strip_gender(word: str) -> str:
    """Return an ordinal word without its masculine or feminine ending."""
    return word[:-1] if len(word) > 1 and word[-1] in "oa" else word


def _cardinal(word: str) -> int | None:
    """Return the value of a one-word cardinal (``uno``, ``dieciséis``, ``veintidós``), if any."""
    if word in _UNITS:
        return _UNITS[word]
    if word in _IRREGULAR_CARDINALS:
        return _IRREGULAR_CARDINALS[word]
    if word in _CARDINAL_TENS:
        return _CARDINAL_TENS[word]
    for prefix, base in _CARDINAL_PREFIXES.items():
        if word.startswith(prefix) and word[len(prefix) :] in _UNITS:
            return base + _UNITS[word[len(prefix) :]]
    return None


def _ordinal(word: str) -> int | None:
    """Return the value of a one-word ordinal in either gender (``vigesimotercera``), if any."""
    stem = _strip_gender(word)
    if stem in _ORDINAL_UNITS:
        return _ORDINAL_UNITS[stem]
    if stem in _IRREGULAR_ORDINALS:
        return _IRREGULAR_ORDINALS[stem]
    for tens_stem, tens in _ORDINAL_TENS.items():
        if stem == tens_stem:
            return tens
        # Fused forms: decimo + tercer, vigesimo + quint, ...
        if stem.startswith(tens_stem + "o") and stem[len(tens_stem) + 1 :] in _ORDINAL_UNITS:
            return tens + _ORDINAL_UNITS[stem[len(tens_stem) + 1 :]]
    return None


def _parse_number(tokens: Sequence[str]) -> tuple[int, int] | None:
    """Read a number at the start of ``tokens``; return ``(value, tokens consumed)`` or ``None``.

    Accepts digits, cardinals (``treinta y uno``) and ordinals, which above the twentieth may be
    one word or two (``trigésimo primero``). The longest reading wins, so ``nonagésimo noveno``
    is 99 and not 90 followed by a stray word.
    """
    head = tokens[0]
    if head.isdigit():
        return int(head), 1
    if len(tokens) >= 3 and tokens[1] == "y" and head in _CARDINAL_TENS and tokens[2] in _UNITS:
        return _CARDINAL_TENS[head] + _UNITS[tokens[2]], 3
    if len(tokens) >= 2:
        tens, unit = _ordinal(head), _ordinal(tokens[1])
        if tens is not None and unit is not None and tens % 10 == 0 and 1 <= unit <= 9:
            return tens + unit, 2
    value = _cardinal(head)
    if value is None:
        value = _ordinal(head)
    return (value, 1) if value is not None else None


def _roman(token: str) -> int | None:
    """Return the value of a Roman numeral token (annex numbers), or ``None``."""
    if not token or not _ROMAN_NUMERAL.fullmatch(token):
        return None
    total, previous = 0, 0
    for char in reversed(token):
        value = _ROMAN_VALUES[char]
        total += value if value >= previous else -value
        previous = max(previous, value)
    return total


def _latin_rank(word: str) -> int:
    """Return the rank of a Latin suffix (``bis`` 2, ``terdecies`` 13, ``unvicies`` 21), else 0."""
    if word in _LATIN_SUFFIXES:
        return _LATIN_SUFFIXES[word]
    for tens_word, tens in _LATIN_TENS.items():
        if word.endswith(tens_word):
            units = word.removesuffix(tens_word)
            if not units:
                return tens
            if units in _LATIN_UNITS:
                return tens + _LATIN_UNITS[units]
    return 0


def _parse_position(tokens: Sequence[str]) -> tuple[_Position, int] | None:
    """Read a number plus an optional Latin suffix; return ``(position, tokens consumed)``."""
    number = _parse_number(tokens) if tokens else None
    if number is None:
        return None
    value, used = number
    suffix = _latin_rank(tokens[used]) if used < len(tokens) else 0
    return (value, suffix), used + (1 if suffix else 0)


# --- Normalisation pipeline -----------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    """One folded token and where it sat in the original text."""

    text: str
    start: int
    end: int
    """Offsets in the original string: ``original[start:end]`` is the token as written."""


# Alternatives in order: a number with thousands dots ("1.240"; three digits after the dot, so
# "46.2" is 46 followed by 2), a plain number, dotted single letters ("d.a."), a word, and "§".
_TOKEN = re.compile(r"\d{1,3}(?:\.\d{3})+(?!\d)|\d+|(?<![a-z])[a-z](?:\.[a-z](?![a-z]))+|[a-z]+|§")


def _fold(text: str) -> tuple[str, list[int]]:
    """Lower-case ``text`` without accents; also give, per output character, its original offset."""
    chars: list[str] = []
    origin: list[int] = []
    for offset, char in enumerate(text):
        for part in unicodedata.normalize("NFD", char.lower()):
            if not unicodedata.combining(part):
                chars.append(part)
                origin.append(offset)
    return "".join(chars), origin


def _scan(text: str) -> list[_Token]:
    """Reduce ``text`` to lower-case unaccented tokens, each with its place in ``text``.

    Case, accents, NBSP, repeated spaces, ordinal indicators (``3.ª``) and punctuation vanish;
    ``d.a.`` becomes ``da`` and ``1.240`` becomes ``1240``; letters and digits are split
    (``46bis`` -> ``46 bis``); ``§`` is kept as a token of its own because ``§ 1`` is a title,
    not an article.
    """
    folded, origin = _fold(text)
    tokens = []
    for match in _TOKEN.finditer(folded):
        tokens.append(
            _Token(
                match.group().replace(".", ""),
                origin[match.start()],
                origin[match.end() - 1] + 1,
            )
        )
    return tokens


def _tokenise(text: str) -> list[str]:
    """The token texts of ``text`` (see :func:`_scan`); the one normalisation step."""
    return [token.text for token in _scan(text)]


def _parse_article(tokens: Sequence[str]) -> tuple[_Designation, int] | None:
    """Read what follows ``artículo``: ``único``, a number with suffix, or a range."""
    if tokens[:1] and tokens[0] in _SOLE_WORDS:
        return _Designation("article"), 1
    first = _parse_position(tokens)
    if first is None:
        return None
    start, used = first
    if used < len(tokens) and tokens[used] in _RANGE_CONNECTORS:
        last = _parse_position(tokens[used + 1 :])
        if last is not None:
            return _Designation("article", start=start, end=last[0]), used + 1 + last[1]
    return _Designation("article", start=start), used


def _parse_numbered(
    kind: _Kind, subtype: str, tokens: Sequence[str], *, roman: bool
) -> tuple[_Designation, int]:
    """Read what follows a provision type or ``anexo``: nothing, ``única`` or a number."""
    if tokens and tokens[0] in _SOLE_WORDS:
        return _Designation(kind, subtype), 1
    number = _parse_number(tokens) if tokens else None
    if number is not None:
        return _Designation(kind, subtype, start=(number[0], 0)), number[1]
    value = _roman(tokens[0]) if roman and tokens else None
    if value is not None:
        return _Designation(kind, subtype, start=(value, 0)), 1
    # Nothing recognisable follows: a bare type. For a title, the rest is heading text.
    return _Designation(kind, subtype, bare=True), 0


def _parse_leading(tokens: Sequence[str]) -> tuple[_Designation, int] | None:
    """Recognise the designation at the start of ``tokens``; return it and the tokens used."""
    head = tokens[0]
    if head in _ARTICLE_HEADS:
        parsed = _parse_article(tokens[1:])
        return (parsed[0], parsed[1] + 1) if parsed else None
    if head.isdigit() or head in _SOLE_WORDS:
        return _parse_article(tokens)
    if head == "disposicion" and len(tokens) > 1 and tokens[1] in _PROVISION_TYPES:
        parsed = _parse_numbered("provision", tokens[1], tokens[2:], roman=False)
        return parsed[0], parsed[1] + 2
    if head in _PROVISION_ABBREVIATIONS:
        parsed = _parse_numbered(
            "provision", _PROVISION_ABBREVIATIONS[head], tokens[1:], roman=False
        )
        return parsed[0], parsed[1] + 1
    if head == "anexo":
        parsed = _parse_numbered("annex", "", tokens[1:], roman=True)
        return parsed[0], parsed[1] + 1
    if head == "preambulo":
        return _Designation("preamble"), 1
    if tokens[:3] == ["exposicion", "de", "motivos"]:
        return _Designation("preamble"), 3
    return None


# What may separate a title's designation from its heading text: "Artículo 5. Definiciones",
# "Art. 6.- Objeto". A bare space is not enough (see ``_designate_title``).
_HEADING_SEPARATOR = re.compile(r"[.:;\-\u2013\u2014]")


@lru_cache(maxsize=16384)
def _designate_title(title: str) -> _Designation | None:
    """Reduce a block title to a designation; ``None`` when it has no tokens at all.

    A designation may be followed by heading text (``Artículo 5. Definiciones``), which is
    ignored. For an article, though, a trailing word that follows the number after a space only
    is not heading text but a suffix we cannot read: ``Artículo 163 foobar`` must not be taken
    for ``Artículo 163``, so such a title is kept as ``other`` and matches only itself.
    """
    # Cached because ``locate`` designates every title of the index on each call and the Código
    # Civil index has 2,400 of them; titles are pure input, so the cache never goes stale.
    tokens = _scan(title)
    if not tokens:
        return None
    parsed = _parse_leading([token.text for token in tokens])
    if parsed is not None:
        designation, used = parsed
        gap = title[tokens[used - 1].end : tokens[used].start] if used < len(tokens) else ""
        if used == len(tokens) or designation.kind != "article" or _HEADING_SEPARATOR.search(gap):
            return designation
    # Bare ordinals ("Primera"), "§ 1", headings and free text: only an exact match counts.
    # Reading "tercera" as "any third provision" would pick an arbitrary block.
    return _Designation("other", text=" ".join(token.text for token in tokens))


def _designate_query(text: str) -> tuple[_Designation | None, str | None]:
    """Reduce a query to ``(designation, ignored)``; the designation is ``None`` without tokens.

    The designation is the leading one, and ``ignored`` is what follows it in the original
    wording (``.2`` for ``46.2``, ``de la Constitución`` for ``14 de la Constitución``), or
    ``None`` when no token was left over. Text with no leading designation is compared as a
    whole (``other``) and ignores nothing.
    """
    tokens = _scan(text)
    if not tokens:
        return None, None
    parsed = _parse_leading([token.text for token in tokens])
    if parsed is None:
        return _Designation("other", text=" ".join(token.text for token in tokens)), None
    designation, used = parsed
    if used >= len(tokens):
        return designation, None
    return designation, text[tokens[used - 1].end :].strip()


def _designate_entry(entry: IndexEntry) -> _Designation | None:
    """Designation of an index entry, falling back to its id for an untitled preamble."""
    designation = _designate_title(entry.title)
    if designation is None and entry.block_id in _PREAMBLE_IDS:
        return _Designation("preamble")
    return designation


# --- Matching -------------------------------------------------------------------------------


def _contains(block: _Designation, wanted: _Designation) -> bool:
    """Whether the range block ``block`` covers the single article ``wanted``."""
    return (
        block.kind == "article"
        and block.start is not None
        and block.end is not None
        and wanted.start is not None
        and block.start <= wanted.start <= block.end
    )


def _matches(
    query: _Designation, designated: list[tuple[IndexEntry, _Designation]]
) -> list[IndexEntry]:
    """Return every entry whose designation is the query's, in index order."""
    exact = [entry for entry, d in designated if d.identity == query.identity]
    if exact:
        # An exact block beats a range that also contains the article: "163 quater" is its own
        # block and also inside "Artículos 163 bis a 163 quáter"; the own block is the answer.
        return exact
    if query.kind == "article" and query.start is not None and query.end is None:
        return [entry for entry, d in designated if _contains(d, query)]
    if query.bare and query.kind in ("provision", "annex"):
        # "disposición transitoria" with no ordinal is only meaningful when the law has one.
        same = [e for e, d in designated if (d.kind, d.subtype) == (query.kind, query.subtype)]
        return same if len(same) == 1 else []
    return []


def _gap(query: _Designation, block: _Designation) -> int:
    """Distance in article or ordinal numbers between the query and a block's number or range."""
    if query.start is None:
        return 0
    if block.start is None:
        return _FAR
    wanted, low, high = query.start[0], block.start[0], (block.end or block.start)[0]
    return max(low - wanted, wanted - high, 0)


def _rank(
    query: _Designation, query_text: str, title: str, block: _Designation
) -> tuple[int, int, float]:
    """Sort key for a not-found candidate: same type by nearest number, then by similarity."""
    same_type = query.kind not in ("other", "preamble") and (block.kind, block.subtype) == (
        query.kind,
        query.subtype,
    )
    if same_type:
        return 0, _gap(query, block), 0.0
    similarity = SequenceMatcher(None, query_text, " ".join(_tokenise(title))).ratio()
    return 1, 0, -similarity


def _closest(
    query: _Designation, query_text: str, designated: list[tuple[IndexEntry, _Designation]]
) -> tuple[IndexEntry, ...]:
    """Up to eight titled blocks nearest to the query; untitled blocks are never offered."""
    titled = [(entry, d) for entry, d in designated if entry.title.strip()]
    query_text = query_text[:_MAX_QUERY_TEXT]
    # ``sorted`` is stable, so equally near blocks keep their index order.
    ranked = sorted(titled, key=lambda pair: _rank(query, query_text, pair[0].title, pair[1]))
    return tuple(entry for entry, _ in ranked[:_MAX_CANDIDATES])


def locate(index: Sequence[IndexEntry], query: str) -> LocateResult:
    """Find the block of ``index`` that ``query`` refers to.

    The query is reduced to a designation and compared with the designation of each title: one
    match is ``found``, several are ``ambiguous`` (index order, at most ten) and none is
    ``not_found`` with up to eight close titles. Text after the leading designation (``.2`` in
    ``46.2``) does not take part and is returned as ``ignored``. Only a query that is no
    designation at all (and matches no title as a whole) may be a block id; a designation such
    as ``dt`` never is, use :func:`locate_block_id` for ids. Raises ``ValueError`` for an empty
    or whitespace-only query.
    """
    text = query.strip()
    if not text:
        raise ValueError("The block query must not be empty.")
    wanted, ignored = _designate_query(text)
    designated = [(e, d) for e in index if (d := _designate_entry(e)) is not None]
    if wanted is None:
        # Only punctuation: nothing to compare, so only offer titles.
        return LocateResult("not_found", None, _closest(_Designation("other"), "", designated))
    matches = _matches(wanted, designated)
    if len(matches) == 1:
        return LocateResult("found", matches[0], (), ignored)
    if matches:
        # Duplicate designations are real (a Real Decreto and the Reglamento it approves both
        # have "Artículo 1", api-notes "Index title patterns"). Picking the first would silently
        # answer about the wrong text, so the caller gets all of them to choose from.
        return LocateResult("ambiguous", None, tuple(matches[:_MAX_AMBIGUOUS]), ignored)
    if wanted.kind == "other":
        # An id is a last resort, after the titles: otherwise "primera" would resolve by id while
        # "Primera" is ambiguous, and an id such as "dt" would silently beat the designation.
        for entry in index:
            if entry.block_id == text:
                return LocateResult("found", entry, ())
    query_text = " ".join(_tokenise(text))
    return LocateResult("not_found", None, _closest(wanted, query_text, designated), ignored)


def locate_block_id(index: Sequence[IndexEntry], block_id: str) -> LocateResult:
    """Find the block whose id is exactly ``block_id``; else ``not_found`` with close titles.

    This is the escape hatch of :func:`locate` for blocks that a designation cannot single out
    (duplicated titles, bare ordinals, ids such as ``dt``). The comparison is exact and
    case-sensitive. Raises ``ValueError`` for an empty or whitespace-only id.
    """
    text = block_id.strip()
    if not text:
        raise ValueError("The block id must not be empty.")
    for entry in index:
        if entry.block_id == text:
            return LocateResult("found", entry, ())
    designated = [(e, d) for e in index if (d := _designate_entry(e)) is not None]
    other = _Designation("other")
    return LocateResult("not_found", None, _closest(other, " ".join(_tokenise(text)), designated))
