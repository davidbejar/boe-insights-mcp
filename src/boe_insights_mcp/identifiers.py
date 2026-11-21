"""BOE law identifiers: the pattern and the canonical spelling.

This module is pure and has no imports from the rest of the package, so ``client`` (which must
refuse malformed identifiers before building a URL) and ``references`` (which recognises them in
typed text) can share one definition without depending on each other.
"""

from __future__ import annotations

import re

__all__ = ["LAW_ID", "normalise_identifier"]

# BOE ids are `<gazette>-<series letter>-<year>-<number>`. This strict form, with an upper-case
# gazette prefix, is what the client accepts into a URL.
LAW_ID = re.compile(r"[A-Z]{2,6}-[A-Za-z]-[0-9]{4}-[0-9]{1,6}")
# People type any letter case, so normalisation accepts the same shape case-insensitively.
_TYPED_IDENTIFIER = re.compile(LAW_ID.pattern, re.IGNORECASE)


def normalise_identifier(text: str) -> str | None:
    """Return ``text`` as a canonical BOE identifier, or ``None`` if it is not one.

    Surrounding whitespace is ignored; the BOE series letter is upper-case, others as given
    (``boe-a-2015-10566`` gives ``BOE-A-2015-10566``). Every entry point that takes an
    identifier goes through this function, so they all accept the same spellings.
    """
    text = text.strip()
    if not _TYPED_IDENTIFIER.fullmatch(text):
        return None
    prefix, _, rest = text.partition("-")
    return f"{prefix}-{rest}".upper() if prefix.upper() == "BOE" else f"{prefix.upper()}-{rest}"
