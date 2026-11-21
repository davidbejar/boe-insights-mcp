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

__all__ = ["ParsedReference", "parse_reference"]

# BOE ids are `<gazette>-<series letter>-<year>-<number>`; accept any letter case when typed.
_IDENTIFIER = re.compile(LAW_ID.pattern, re.IGNORECASE)

# (BOE `rangos` code, canonical name, regexes for the rank words). The regexes run on text that
# is lower-cased and stripped of accents; dots in abbreviations are optional.
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
)
_RANK_NAMES = {code: name for code, name, _ in _RANKS}

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


def _fold(text: str) -> str:
    """Lower-case ``text`` and strip accents, so ``Orgánica`` matches ``organica``."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _parse_identifier(text: str) -> ParsedReference | None:
    """Recognise a BOE-style identifier; the BOE series letter is upper-case, others as given."""
    if not _IDENTIFIER.fullmatch(text):
        return None
    prefix, _, rest = text.partition("-")
    identifier = (
        f"{prefix}-{rest}".upper() if prefix.upper() == "BOE" else f"{prefix.upper()}-{rest}"
    )
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
    folded = _fold(text)
    with_rank = _RANK_AND_NUMBER.search(folded)
    if with_rank is not None:
        code = next(code for code, _, _ in _RANKS if with_rank.group(f"r{code}"))
        return ParsedReference(
            kind="numbered",
            rank_code=code,
            rank_name=_RANK_NAMES[code],
            official_number=with_rank.group("number").upper(),
        )
    bare = _NUMBER_ONLY.search(folded)
    if bare is None:
        return None
    return ParsedReference(kind="numbered", official_number=bare.group("number").upper())
