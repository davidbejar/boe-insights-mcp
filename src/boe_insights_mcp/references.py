"""Pure parsing of human references to Spanish laws, such as ``Ley 40/2015``.

No I/O happens here. A reference is either a BOE identifier (``BOE-A-2015-10566``) or an
official number (``40/2015``), optionally preceded by a rank word (``Ley``, ``RDL``, ...).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from boe_insights_mcp.client import LAW_ID

__all__ = ["ParsedReference", "fold", "normalise_identifier", "parse_reference"]

# BOE ids are `<gazette>-<series letter>-<year>-<number>`; accept any letter case when typed.
_IDENTIFIER = re.compile(LAW_ID.pattern, re.IGNORECASE)

# (BOE `rangos` code, canonical name, regexes for the rank words). The regexes run on text that
# is lower-cased and stripped of accents; dots in abbreviations are optional. The regional
# ranks come last. Alternatives are tried with the number that must follow, so the longest
# rank wins ("Real Decreto-ley 6/2019" is never read as "Decreto-ley" or "Real Decreto").
_RANKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("1290", "Ley Orgánica", (r"ley\s+organica", r"l\.?o\.?")),
    ("1320", "Real Decreto-ley", (r"real\s+decreto[\s-]+ley", r"rd[\s-]*ley", r"r\.?d\.?l\.?")),
    (
        "1310",
        "Real Decreto Legislativo",
        (r"real\s+decreto\s+legislativo", r"rd\s+legislativo", r"r\.?d\.?leg\.?"),
    ),
    ("1340", "Real Decreto", (r"real\s+decreto", r"r\.?d\.?")),
    ("1300", "Ley", (r"ley",)),
    ("1350", "Orden", (r"orden",)),
    ("1370", "Resolución", (r"resolucion",)),
    ("1390", "Circular", (r"circular",)),
    ("1410", "Instrucción", (r"instruccion",)),
    ("1450", "Ley Foral", (r"ley\s+foral",)),
    ("1470", "Decreto Legislativo", (r"decreto\s+legislativo",)),
    ("1480", "Decreto Foral Legislativo", (r"decreto\s+foral\s+legislativo",)),
    ("1500", "Decreto-ley", (r"decreto[\s-]+ley",)),
    ("1325", "Decreto-ley Foral", (r"decreto[\s-]+ley\s+foral",)),
    ("1510", "Decreto", (r"decreto",)),
)
_RANK_NAMES = {code: name for code, name, _ in _RANKS}
# Ranks that only regional (autonomous community) legislation uses; state laws never carry them
# in the consolidated legislation except for a few pre-1978 decrees.
_REGIONAL_RANKS = frozenset({"1450", "1470", "1480", "1500", "1325", "1510"})

# The 17 autonomous communities (Spanish, Catalan, Basque, Galician and Valencian forms) and
# words that mark a law as regional. Matched on folded text, as whole words.
_REGION_WORDS = (
    r"andalucia|aragon|asturias|baleares|balears|canarias|cantabria|castilla[\s-]+la[\s-]+mancha"
    r"|castilla\s+y\s+leon|cataluna|catalunya|valenciana|valenciano|extremadura|galicia|madrid"
    r"|murcia|navarra|rioja|euskadi|pais\s+vasco|generalitat|foral|autonomic\w*"
)
_REGION = re.compile(rf"(?<![a-z])(?:{_REGION_WORDS})(?![a-z])")
# The word before a bare number may be a rank this module does not know ("Decreto Foral").
_NUMBER_MARK = re.compile(r"(?:\bn\.?[º°o]?\.?|\bnum(?:ero)?\.?)\s*$")
_LAST_WORD = re.compile(r"([a-z]+)[.\s]*$")
_CONNECTORS = frozenset(
    {
        *("de", "del", "la", "el", "los", "las", "un", "una", "y", "e", "o", "u", "a", "al"),
        *("en", "con", "por", "segun", "ver", "vease", "art", "arts", "articulo", "articulos"),
        *("ref", "referencia"),
    }
)

# Years 1800-2100; an optional letter prefix covers ministerial orders such as `APA/315/2020`.
_NUMBER = r"(?:[a-z]{1,6}/)?\d{1,4}/(?:1[89]\d{2}|20\d{2}|2100)(?!\d)"
_BOUNDARY = r"(?<![\w.\-/])"
_NUMBER_ONLY = re.compile(rf"{_BOUNDARY}(?P<number>{_NUMBER})")
_RANK_GROUPS = "|".join(f"(?P<r{code}>{'|'.join(words)})" for code, _, words in _RANKS)
_RANK_AND_NUMBER = re.compile(rf"{_BOUNDARY}(?:{_RANK_GROUPS})\s*(?P<number>{_NUMBER})")


@dataclass(frozen=True)
class ParsedReference:
    """The result of :func:`parse_reference`."""

    kind: Literal["identifier", "numbered"]
    identifier: str | None = None
    """For ``identifier``: the BOE identifier, e.g. ``BOE-A-2015-10566``."""
    rank_code: str | None = None
    """For ``numbered``: the BOE rank code, or ``None`` if no rank word was given."""
    rank_name: str | None = None
    """The canonical Spanish name of ``rank_code``, e.g. ``Real Decreto-ley``."""
    official_number: str | None = None
    """For ``numbered``: the official number, e.g. ``40/2015``."""
    unrecognised_rank_word: str | None = None
    """For ``numbered`` without a rank: a rank-like word before the number that is not known."""
    region: str | None = None
    """For ``numbered``: a region name or regional marker found in the text, if any."""

    @property
    def regional_rank(self) -> bool:
        """Whether ``rank_code`` is a rank used by regional legislation."""
        return self.rank_code in _REGIONAL_RANKS

    @property
    def may_be_regional(self) -> bool:
        """Whether the text signals that the law may be regional rather than state."""
        return (
            self.regional_rank or self.unrecognised_rank_word is not None or self.region is not None
        )


def fold(text: str) -> str:
    """Lower-case ``text`` and strip accents, so ``Orgánica`` matches ``organica``."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise_identifier(text: str) -> str | None:
    """Return ``text`` as a canonical BOE identifier, or ``None`` if it is not one.

    Surrounding whitespace is ignored; the BOE series letter is upper-case, others as given
    (``boe-a-2015-10566`` gives ``BOE-A-2015-10566``). Every entry point that takes an
    identifier goes through this function, so they all accept the same spellings.
    """
    text = text.strip()
    if not _IDENTIFIER.fullmatch(text):
        return None
    prefix, _, rest = text.partition("-")
    return f"{prefix}-{rest}".upper() if prefix.upper() == "BOE" else f"{prefix.upper()}-{rest}"


def _parse_identifier(text: str) -> ParsedReference | None:
    identifier = normalise_identifier(text)
    if identifier is None:
        return None
    return ParsedReference(kind="identifier", identifier=identifier)


def parse_reference(text: str) -> ParsedReference | None:
    """Parse a reference to a law, or return ``None`` if it has no identifier or number.

    Accepts an identifier (``BOE-A-2015-10566``) or an official number with an optional rank
    word (``Ley 40/2015``, ``RDL 6/2019``), also inside a longer phrase. The first rank and
    number pair wins; without a rank word the first number is used. Years must have four
    digits (1800-2100).
    """
    identifier = _parse_identifier(text.strip())
    if identifier is not None:
        return identifier
    folded = fold(text)
    region = _REGION.search(folded)
    region_text = region.group(0) if region else None
    with_rank = _RANK_AND_NUMBER.search(folded)
    if with_rank is not None:
        code = next(code for code, _, _ in _RANKS if with_rank.group(f"r{code}"))
        return ParsedReference(
            kind="numbered",
            rank_code=code,
            rank_name=_RANK_NAMES[code],
            official_number=with_rank.group("number").upper(),
            region=region_text,
        )
    bare = _NUMBER_ONLY.search(folded)
    if bare is None:
        return None
    return ParsedReference(
        kind="numbered",
        official_number=bare.group("number").upper(),
        unrecognised_rank_word=_word_before(folded[: bare.start()]),
        region=region_text,
    )


def _word_before(prefix: str) -> str | None:
    """Return the word just before a number if it could be a rank (not a connector word)."""
    word = _LAST_WORD.search(_NUMBER_MARK.sub("", prefix.rstrip()))
    if word is None or word.group(1) in _CONNECTORS:
        return None
    return word.group(1)
