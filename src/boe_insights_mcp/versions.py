"""Version selection, repeal detection, diffs and history of a block of law text.

This module is pure: it works on the ``Block`` models that ``textparse`` builds and does no I/O.
The tools in ``articles`` use it to answer "what did this article say on date X", "what changed
between two dates" and "when was it amended".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from difflib import SequenceMatcher, unified_diff
from typing import Literal

from pydantic import BaseModel, ConfigDict

from boe_insights_mcp.models import Block, BlockVersion, collapse_whitespace

__all__ = [
    "MONTHS",
    "ArticleState",
    "ParagraphChange",
    "VersionDiff",
    "VersionEvent",
    "diff_versions",
    "history",
    "is_repealed",
    "repeal_date",
    "repeal_note",
    "repeal_version_date",
    "state_on",
    "version_on",
]

ArticleState = Literal["in_force", "repealed", "not_yet_in_force", "did_not_exist"]

# A repealed article keeps a version whose whole body is a marker such as ``(Derogado).``
# (api-notes 6). The marker must be the entire paragraph: "Derogado parcialmente..." or a
# sentence that merely mentions a repeal is live text. ``Sin efecto`` was seen as the last
# version of a provision in the real data (RD 1624/1992, api-notes 6.1). The plural forms
# (``(Derogados)`` for a block that stands for several articles) are the same marker; they were
# not seen in the fixtures, so this is inferred from the singular.
_REPEAL_MARKER = re.compile(
    r"\(?\s*(derogad[oa]s?|suprimid[oa]s?|sin contenido|sin efecto|anulad[oa]s?(?:/as)?)\s*\)?\.?",
    re.IGNORECASE,
)
# Opening words of a law's initial note when the whole law is repealed (api-notes, "Initial
# note"); matched at the start of the first paragraph only, never anywhere in the text.
_REPEAL_NOTE_PREFIX = "norma derogada"
# "Norma derogada parcialmente" starts the same way but is not a whole-law repeal. Not seen in
# the sample (api-notes "Initial note" lists it as a possible variant), so excluded by wording.
_PARTIAL_NOTE_PREFIX = "norma derogada parcialmente"
# The date of a whole-law repeal in the note's first paragraph: "con efectos de 7 de diciembre
# de 2018" (all five wordings of api-notes "Initial note").
_NOTE_DATE = re.compile(r"con efectos de (\d{1,2}) de ([a-z]+) de (\d{4})", re.IGNORECASE)
# Spanish month names (lower case) to month numbers; also used by ``structure`` to read dates in
# publication notes. ``setiembre`` is an accepted older spelling.
MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        ),
        start=1,
    )
} | {"setiembre": 9}
# Minimum similarity for two paragraphs of an uneven replaced run to count as the same
# paragraph reworded rather than one removed and another added.
_PAIRING_THRESHOLD = 0.5
# Pairing paragraphs by similarity costs one character-level comparison per (old, new) pair, so
# it grows with the product of the two run sizes: 100 x 100 paragraphs of 260 characters took
# 12 s and 200 x 200 took 48 s. Beyond these limits (cells in the matrix, and the product of the
# two runs' total characters) the run is paired by position instead.
_MAX_PAIRING_CELLS = 1600
_MAX_PAIRING_CHARS = 30_000_000
# Largest word-level comparison attempted for one changed paragraph (old words x new words).
_MAX_INLINE_CELLS = 4_000_000


class _Frozen(BaseModel):
    """Base of the result models: immutable."""

    model_config = ConfigDict(frozen=True)


class ParagraphChange(_Frozen):
    """What happened to one body paragraph between two versions."""

    op: Literal["added", "removed", "changed", "unchanged"]
    old_text: str | None
    new_text: str | None
    inline: str | None
    """For ``changed`` only: the new paragraph with ``[-removed-]`` and ``{+added+}`` words.

    ``None`` for a multi-line paragraph (a table) or a very long one: use the two texts."""


class VersionDiff(_Frozen):
    """The paragraph-level difference between two versions of a block."""

    changes: tuple[ParagraphChange, ...]
    """Every body paragraph in reading order, ``unchanged`` ones included."""
    added: int
    removed: int
    changed: int
    unchanged: int
    identical: bool
    unified: str
    """Unified diff of the body text, one paragraph per line; empty when identical."""


class VersionEvent(_Frozen):
    """One entry of a block's amendment history."""

    index: int
    """1-based position in the block's version list."""
    kind: Literal["original", "amended", "repealed"]
    effective_from: date | None
    published_on: date | None
    source_law_id: str
    added: int
    removed: int
    changed: int


def version_on(block: Block, on: date) -> BlockVersion | None:
    """The latest version in effect on ``on``, or ``None`` when no version qualifies yet."""
    # ``Block.versions`` is ascending by ``starts_on``, so the last qualifying version wins,
    # which also makes the later of two versions with the same date win. A version with no date
    # at all counts as applying from the beginning. ``starts_on`` and not ``effective_from``
    # because ``fecha_vigencia`` can be empty on a first or last version (api-notes 6.1).
    for version in reversed(block.versions):
        if version.starts_on is None or version.starts_on <= on:
            return version
    return None


def is_repealed(version: BlockVersion) -> bool:
    """Whether the body of a version is just a repeal marker such as ``(Derogado).``."""
    # Quoted paragraphs are notes about the text ("Téngase en cuenta...", citations), not the
    # text, so they neither hide a marker nor count as live text; footnotes are already left out.
    body = tuple(p for p in version.body if not p.quoted)
    if body and body[0].kind == "articulo":
        body = body[1:]  # the article heading survives a repeal
    return len(body) == 1 and _REPEAL_MARKER.fullmatch(body[0].text) is not None


def repeal_note(note: Block | None) -> BlockVersion | None:
    """The latest version of an initial note that announces a repeal, or ``None``."""
    if note is None:
        return None
    # Latest by start date among the repeal versions only: a later version of the note may be an
    # unrelated one ("Incluye las correcciones de errores...", api-notes, "Initial note") and
    # must not hide the repeal. ``max`` returns the first of equal keys, so ties keep document
    # order by scanning the reversed list (the later one wins).
    candidates = [v for v in reversed(note.versions) if _announces_repeal(v)]
    return max(candidates, key=lambda v: v.start_key, default=None)


def repeal_date(note: Block | None) -> date | None:
    """When the whole-law repeal announced by an initial note takes effect, or ``None``.

    The date is read from "con efectos de <d> de <mes> de <yyyy>" in the first paragraph of the
    earliest repeal version; failing that, it is the start date of that earliest version.
    ``None`` when the note announces no repeal or no date can be found.
    """
    if note is None:
        return None
    # Earliest, not latest like ``repeal_note``: a later version of the note only restates the
    # repeal (Ley 11/2007 has two such versions, the second published in 2018), and the repeal
    # took effect when the first one said. ``min`` keeps the first of equal keys, so ties keep
    # document order.
    repeals = [v for v in note.versions if _announces_repeal(v)]
    if not repeals:
        return None
    return repeal_version_date(min(repeals, key=lambda v: v.start_key))


def repeal_version_date(version: BlockVersion) -> date | None:
    """The repeal date of one note version: its wording's date, else its own start date."""
    return _wording_date(version) or version.starts_on


def _wording_date(version: BlockVersion) -> date | None:
    """The date in "con efectos de <d> de <mes> de <yyyy>" of the first paragraph, if valid."""
    match = _NOTE_DATE.search(collapse_whitespace(version.paragraphs[0].text))
    if match is None or match.group(2).casefold() not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1)))
    except ValueError:  # an impossible day such as 31 de abril
        return None


def _announces_repeal(version: BlockVersion) -> bool:
    """Whether the first paragraph starts with "Norma derogada" (case and spacing ignored).

    "Norma derogada parcialmente" is not a repeal of the whole law and does not count.
    """
    if not version.paragraphs:
        return False
    first = collapse_whitespace(version.paragraphs[0].text).casefold()
    return first.startswith(_REPEAL_NOTE_PREFIX) and not first.startswith(_PARTIAL_NOTE_PREFIX)


def state_on(block: Block, on: date) -> ArticleState:
    """The state of the block on ``on``: in force, repealed, not yet in force or non-existent."""
    version = version_on(block, on)
    if version is not None:
        return "repealed" if is_repealed(version) else "in_force"
    # No version applies: either the text was already published but starts later, or nothing
    # existed yet. Only the earliest version tells (later ones come after it by definition).
    earliest_published = block.versions[0].published_on if block.versions else None
    if earliest_published is not None and on >= earliest_published:
        return "not_yet_in_force"
    return "did_not_exist"


def diff_versions(old: BlockVersion, new: BlockVersion) -> VersionDiff:
    """Compare the body paragraphs (footnotes excluded) of two versions by text."""
    old_texts = [p.text for p in old.body]
    new_texts = [p.text for p in new.body]
    changes: list[ParagraphChange] = []
    matcher = SequenceMatcher(None, old_texts, new_texts, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            changes.extend(_change("unchanged", t, t) for t in old_texts[i1:i2])
        elif tag == "delete":
            changes.extend(_change("removed", t, None) for t in old_texts[i1:i2])
        elif tag == "insert":
            changes.extend(_change("added", None, t) for t in new_texts[j1:j2])
        else:
            changes.extend(_pair_run(old_texts[i1:i2], new_texts[j1:j2]))
    counts = {op: sum(c.op == op for c in changes) for op in ("added", "removed", "changed")}
    return VersionDiff(
        changes=tuple(changes),
        unchanged=len(changes) - sum(counts.values()),
        identical=not any(counts.values()),
        unified=_unified(old_texts, new_texts),
        **counts,
    )


def history(block: Block, *, limit: int | None = None) -> tuple[VersionEvent, ...]:
    """One event per version: the original, then each amendment or repeal with its diff counts.

    With ``limit`` only the last ``limit`` events are built (and diffed); each keeps its
    position in the whole list as ``index``.
    """
    # Diffing is the expensive part and the caller shows only the newest events, so the older
    # ones are not computed at all.
    first = 0 if limit is None else max(0, len(block.versions) - limit)
    events: list[VersionEvent] = []
    previous: BlockVersion | None = block.versions[first - 1] if first > 0 else None
    for index, version in enumerate(block.versions[first:], start=first + 1):
        if previous is None:
            kind: Literal["original", "amended", "repealed"] = "original"
            added = removed = changed = 0
        else:
            kind = "repealed" if is_repealed(version) else "amended"
            diff = diff_versions(previous, version)
            added, removed, changed = diff.added, diff.removed, diff.changed
        events.append(
            VersionEvent(
                index=index,
                kind=kind,
                effective_from=version.effective_from,
                published_on=version.published_on,
                source_law_id=version.source_law_id,
                added=added,
                removed=removed,
                changed=changed,
            )
        )
        previous = version
    return tuple(events)


def _change(
    op: Literal["added", "removed", "changed", "unchanged"],
    old_text: str | None,
    new_text: str | None,
    inline: str | None = None,
) -> ParagraphChange:
    """Build a ``ParagraphChange``."""
    return ParagraphChange(op=op, old_text=old_text, new_text=new_text, inline=inline)


def _changed(old_text: str, new_text: str) -> ParagraphChange:
    """A ``changed`` entry with its word-level rendering (when that can represent it)."""
    return _change("changed", old_text, new_text, _inline(old_text, new_text))


def _pair_by_position(old: Sequence[str], new: Sequence[str]) -> list[ParagraphChange]:
    """Pair paragraphs one to one in order; the longer side's remainder is removed or added."""
    changes = [_changed(o, n) for o, n in zip(old, new, strict=False)]
    changes.extend(_change("removed", t, None) for t in old[len(new) :])
    changes.extend(_change("added", None, t) for t in new[len(old) :])
    return changes


def _pair_run(old: Sequence[str], new: Sequence[str]) -> list[ParagraphChange]:
    """Resolve a replaced run of paragraphs into changed, removed and added entries."""
    if len(old) == len(new):
        # Same length: the amendment almost certainly rewrote each paragraph in place, so pair
        # by position however different they look.
        return _pair_by_position(old, new)
    if (
        len(old) * len(new) > _MAX_PAIRING_CELLS
        or sum(map(len, old)) * sum(map(len, new)) > _MAX_PAIRING_CHARS
    ):
        # Too many comparisons to afford (see ``_MAX_PAIRING_CELLS``). Positions are a poor guess
        # when a paragraph was inserted, but the diff then still reports every paragraph and
        # finishes in bounded time.
        return _pair_by_position(old, new)
    # Uneven: pair paragraphs that are similar enough, keeping reading order, and treat the rest
    # as removed or added. An optimal order-preserving alignment (as in longest common
    # subsequence, weighted by similarity) avoids pairing greedily the wrong paragraphs and
    # crossing pairs, which would not read as a diff.
    ratios = _ratios(old, new)
    best = _alignment_scores(ratios)
    changes: list[ParagraphChange] = []
    i = j = 0
    while i < len(old) or j < len(new):
        if i == len(old):
            changes.append(_change("added", None, new[j]))
            j += 1
        elif j == len(new) or best[i][j] == best[i + 1][j]:
            changes.append(_change("removed", old[i], None))
            i += 1
        elif ratios[i][j] and best[i][j] == ratios[i][j] + best[i + 1][j + 1]:
            changes.append(_changed(old[i], new[j]))
            i += 1
            j += 1
        else:
            changes.append(_change("added", None, new[j]))
            j += 1
    return changes


def _ratios(old: Sequence[str], new: Sequence[str]) -> list[list[float]]:
    """Similarity of every (old, new) pair; 0.0 below the pairing threshold."""
    # One matcher per new paragraph keeps its index of the text (``set_seq2`` is the side that
    # ``SequenceMatcher`` preprocesses), and only ``set_seq1`` changes along a column. The two
    # upper bounds are far cheaper than ``ratio`` and discard most unrelated pairs: when a bound
    # is already below the threshold the real ratio is too.
    matchers = []
    for text in new:
        matcher = SequenceMatcher(None, autojunk=False)
        matcher.set_seq2(text)
        matchers.append(matcher)
    rows = []
    for text in old:
        row = []
        for matcher in matchers:
            matcher.set_seq1(text)
            if (
                matcher.real_quick_ratio() < _PAIRING_THRESHOLD
                or matcher.quick_ratio() < _PAIRING_THRESHOLD
            ):
                row.append(0.0)
                continue
            ratio = matcher.ratio()
            row.append(ratio if ratio >= _PAIRING_THRESHOLD else 0.0)
        rows.append(row)
    return rows


def _alignment_scores(ratios: Sequence[Sequence[float]]) -> list[list[float]]:
    """Best total similarity of an order-preserving pairing of ``old[i:]`` with ``new[j:]``."""
    rows, cols = len(ratios), len(ratios[0]) if ratios else 0
    best = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            paired = ratios[i][j] + best[i + 1][j + 1] if ratios[i][j] else 0.0
            best[i][j] = max(paired, best[i + 1][j], best[i][j + 1])
    return best


def _inline(old_text: str, new_text: str) -> str | None:
    """The new paragraph with removed words as ``[-...-]`` and added words as ``{+...+}``.

    ``None`` when marks cannot stand for the change: a multi-line paragraph (a table, whose
    rows are separated by newlines that the word split would flatten) or one so long that the
    word-level comparison would be too slow. The caller then shows both texts instead.
    """
    if "\n" in old_text or "\n" in new_text:
        return None
    # Tokens are whitespace-separated words with their punctuation attached, so a changed
    # "diez," shows as one removed and one added word. For single-line text with single spaces
    # (what the parser produces), removing the marks and the added words gives back the old
    # text, and removing the marks and the removed words gives the new one.
    old_words, new_words = old_text.split(), new_text.split()
    if len(old_words) * len(new_words) > _MAX_INLINE_CELLS:
        return None
    pieces: list[str] = []
    matcher = SequenceMatcher(None, old_words, new_words, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pieces.extend(old_words[i1:i2])
            continue
        if i2 > i1:
            pieces.append(f"[-{' '.join(old_words[i1:i2])}-]")
        if j2 > j1:
            pieces.append(f"{{+{' '.join(new_words[j1:j2])}+}}")
    return " ".join(pieces)


def _unified(old_texts: Sequence[str], new_texts: Sequence[str]) -> str:
    """Unified diff with one paragraph per line; empty when nothing differs."""
    # Table paragraphs contain newlines; flatten them so that each paragraph stays on one line.
    old_lines = [t.replace("\n", " / ") for t in old_texts]
    new_lines = [t.replace("\n", " / ") for t in new_texts]
    return "\n".join(unified_diff(old_lines, new_lines, "old", "new", lineterm=""))
