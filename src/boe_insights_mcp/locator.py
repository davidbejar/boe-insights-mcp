"""Find the block of a law's text index that a person means by "artículo 46 bis" or "DA 3".

The BOE text index lists blocks by an opaque id and a human title (``Artículo 46 bis`` is block
``a4-2``), so an article has to be found by title. The titles come in many shapes (see "Index
title patterns" in ``docs/api-notes.md``): bare ``Artículo 14``, ordinal words in old laws
(``Artículo primero``), the Código Civil's ``Art 1`` and range blocks (``Arts 127 a 130``),
provisions with or without an ordinal, annexes, empty and bracketed titles for the preamble.

The approach is one pipeline applied to the query and to every title alike:

1. ``_tokenise`` folds case and accents, drops punctuation and ordinal indicators and splits
   letters from digits, so ``Art.46bis``, ``ARTÍCULO 46 BIS.`` and ``art 46 bis`` give the same
   tokens.
2. ``_designate`` reads the tokens into a small structured ``_Designation`` (kind, provision
   type, number or range, Latin suffix). Anything it does not recognise becomes an ``other``
   designation that is compared by its whole normalised text.
3. ``locate`` compares designations, never strings, and ranks the closest titles when nothing
   matches.

The module is pure: no I/O, and it imports only from ``models`` and the standard library.
``versions`` and ``articles`` (increment 2) call it to turn the person's wording into a block id.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from boe_insights_mcp.models import IndexEntry

__all__ = ["LocateResult", "locate"]

_MAX_AMBIGUOUS = 10
_MAX_CANDIDATES = 8

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
# Ranks 13 to 19 are composed (``_latin_rank``): "terdecies" is "ter" + "decies".
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
    "decies": 10,
    "undecies": 11,
    "duodecies": 12,
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
    """Return the rank of a Latin suffix (``bis`` 2, ``quater`` 4, ``terdecies`` 13), else 0."""
    if word in _LATIN_SUFFIXES:
        return _LATIN_SUFFIXES[word]
    # The RD 1624/1992 index goes up to "Artículo 61 septiesdecies".
    units = word.removesuffix("decies")
    if units != word and 3 <= _LATIN_SUFFIXES.get(units, 0) <= 9:
        return 10 + _LATIN_SUFFIXES[units]
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


def _tokenise(text: str) -> list[str]:
    """Reduce ``text`` to lower-case unaccented tokens; the one normalisation step.

    Case, accents, NBSP, repeated spaces, ordinal indicators (``3.ª``) and punctuation vanish;
    ``d.a.`` becomes ``da``; letters and digits are split (``46bis`` -> ``46 bis``); ``§`` is
    kept as a token of its own because ``§ 1`` is a title, not an article.
    """
    decomposed = unicodedata.normalize("NFD", text.lower())
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\b)", "", folded)
    folded = re.sub(r"§", " § ", folded)
    folded = re.sub(r"[^a-z0-9§]+", " ", folded)
    folded = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", folded)
    return folded.split()


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


def _designate(text: str, *, whole: bool) -> _Designation | None:
    """Reduce a title or query to a designation; ``None`` when it has no tokens at all.

    With ``whole`` (queries) every token must belong to the designation, otherwise the text is
    compared as a whole; without it (titles) a designation may be followed by heading text
    (``Artículo 5. Definiciones``).
    """
    tokens = _tokenise(text)
    if not tokens:
        return None
    parsed = _parse_leading(tokens)
    if parsed is not None and (not whole or parsed[1] == len(tokens)):
        return parsed[0]
    # Bare ordinals ("Primera"), "§ 1", headings and free text: only an exact match counts.
    # Reading "tercera" as "any third provision" would pick an arbitrary block.
    return _Designation("other", text=" ".join(tokens))


def _designate_entry(entry: IndexEntry) -> _Designation | None:
    """Designation of an index entry, falling back to its id for an untitled preamble."""
    designation = _designate(entry.title, whole=False)
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
    # ``sorted`` is stable, so equally near blocks keep their index order.
    ranked = sorted(titled, key=lambda pair: _rank(query, query_text, pair[0].title, pair[1]))
    return tuple(entry for entry, _ in ranked[:_MAX_CANDIDATES])


def locate(index: Sequence[IndexEntry], query: str) -> LocateResult:
    """Find the block of ``index`` that ``query`` refers to.

    A query equal to a block id is found at once. Otherwise the query and each title are reduced
    to a designation: one match is ``found``, several are ``ambiguous`` (index order, at most
    ten) and none is ``not_found`` with up to eight close titles. Raises ``ValueError`` for an
    empty or whitespace-only query.
    """
    text = query.strip()
    if not text:
        raise ValueError("The block query must not be empty.")
    for entry in index:
        if entry.block_id == text:
            return LocateResult("found", entry, ())
    wanted = _designate(text, whole=True)
    designated = [(e, d) for e in index if (d := _designate_entry(e)) is not None]
    if wanted is None:
        # Only punctuation: nothing to compare, so only offer titles.
        return LocateResult("not_found", None, _closest(_Designation("other"), "", designated))
    matches = _matches(wanted, designated)
    if len(matches) == 1:
        return LocateResult("found", matches[0], ())
    if matches:
        # Duplicate designations are real (a Real Decreto and the Reglamento it approves both
        # have "Artículo 1", api-notes "Index title patterns"). Picking the first would silently
        # answer about the wrong text, so the caller gets all of them to choose from.
        return LocateResult("ambiguous", None, tuple(matches[:_MAX_AMBIGUOUS]))
    query_text = " ".join(_tokenise(text))
    return LocateResult("not_found", None, _closest(wanted, query_text, designated))
