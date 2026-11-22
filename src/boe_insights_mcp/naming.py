"""Names for saved searches: slugs and proposed names (pure functions, no I/O).

A saved search is addressed by a short slug that is unique per owner. This module is the only
place that decides what a valid name looks like and how a name is derived from a search's
filters. ``saved_searches`` uses it to normalise a name the model gives and to propose one when
it gives none; ``searchstore`` stores whatever name it is handed and enforces uniqueness.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection

__all__ = ["MAX_NAME", "gazette_base", "law_base", "propose_name", "slugify"]

MAX_NAME = 60
"""Longest name, in characters; keeps names readable in a list and in a prompt."""

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Return ``text`` as a lowercase ASCII slug of at most ``MAX_NAME`` characters.

    Accents are removed, every run of other characters becomes one ``-``, and the ends are
    trimmed. May return an empty string when ``text`` has no letters or digits.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    slug = _NOT_ALNUM.sub("-", ascii_text).strip("-")
    # Cutting can leave a dash at the end (the cut fell on a separator), so strip again.
    return slug[:MAX_NAME].rstrip("-")


def propose_name(base: str, taken: Collection[str]) -> str:
    """Return ``base`` if free, else ``base-2``, ``base-3``, ... (the first free one).

    An empty ``base`` counts as ``search``. The base is shortened when a suffix would push the
    result past ``MAX_NAME``.
    """
    base = base or "search"
    if base not in taken:
        return base
    number = 2
    while True:
        suffix = f"-{number}"
        candidate = base[: MAX_NAME - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            return candidate
        number += 1


def gazette_base(words: str | None, section: str | None, department_code: str | None) -> str:
    """Return a base name for a gazette search from its filters (``""`` when it has none)."""
    parts: list[str] = []
    if words:
        parts.append(slugify(words))
    if section:
        parts.append(f"seccion-{slugify(section)}")
    if department_code:
        parts.append(f"dept-{slugify(department_code)}")
    return slugify("-".join(part for part in parts if part))


def law_base(rank: str | None, official_number: str | None, law_id: str) -> str:
    """Return a base name for a law search: ``ley-40-2015``, or its id when not known."""
    if rank and official_number:
        return slugify(f"{rank} {official_number}")
    return slugify(law_id)
