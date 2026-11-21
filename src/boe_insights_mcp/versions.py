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

from boe_insights_mcp.models import Block, BlockVersion

__all__ = [
    "ArticleState",
    "ParagraphChange",
    "VersionDiff",
    "VersionEvent",
    "diff_versions",
    "history",
    "is_repealed",
    "state_on",
    "version_on",
]

ArticleState = Literal["in_force", "repealed", "not_yet_in_force", "did_not_exist"]

# A repealed article keeps a version whose whole body is a marker such as ``(Derogado).``
# (api-notes 6). The marker must be the entire paragraph: "Derogado parcialmente..." or a
# sentence that merely mentions a repeal is live text.
_REPEAL_MARKER = re.compile(
    r"\(?\s*(derogad[oa]|suprimid[oa]|sin contenido|anulad[oa])\s*\)?\.?", re.IGNORECASE
)
# Minimum similarity for two paragraphs of an uneven replaced run to count as the same
# paragraph reworded rather than one removed and another added.
_PAIRING_THRESHOLD = 0.5


class _Frozen(BaseModel):
    """Base of the result models: immutable."""

    model_config = ConfigDict(frozen=True)


class ParagraphChange(_Frozen):
    """What happened to one body paragraph between two versions."""

    op: Literal["added", "removed", "changed", "unchanged"]
    old_text: str | None
    new_text: str | None
    inline: str | None
    """For ``changed`` only: the new paragraph with ``[-removed-]`` and ``{+added+}`` words."""


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
    # ``Block.versions`` is ascending by effective date, so the last qualifying version wins,
    # which also makes the later of two versions with the same date win. An undated version
    # counts as effective from the beginning.
    for version in reversed(block.versions):
        if version.effective_from is None or version.effective_from <= on:
            return version
    return None


def is_repealed(version: BlockVersion) -> bool:
    """Whether the body of a version is just a repeal marker such as ``(Derogado).``."""
    body = version.body
    if body and body[0].kind == "articulo":
        body = body[1:]  # the article heading survives a repeal
    return len(body) == 1 and _REPEAL_MARKER.fullmatch(body[0].text) is not None


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


def history(block: Block) -> tuple[VersionEvent, ...]:
    """One event per version: the original, then each amendment or repeal with its diff counts."""
    events: list[VersionEvent] = []
    previous: BlockVersion | None = None
    for index, version in enumerate(block.versions, start=1):
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
    """A ``changed`` entry with its word-level rendering."""
    return _change("changed", old_text, new_text, _inline(old_text, new_text))


def _pair_run(old: Sequence[str], new: Sequence[str]) -> list[ParagraphChange]:
    """Resolve a replaced run of paragraphs into changed, removed and added entries."""
    if len(old) == len(new):
        # Same length: the amendment almost certainly rewrote each paragraph in place, so pair
        # by position however different they look.
        return [_changed(o, n) for o, n in zip(old, new, strict=True)]
    # Uneven: pair paragraphs that are similar enough, keeping reading order, and treat the rest
    # as removed or added. An optimal order-preserving alignment (as in longest common
    # subsequence, weighted by similarity) avoids pairing greedily the wrong paragraphs and
    # crossing pairs, which would not read as a diff.
    ratios = [[_ratio(o, n) for n in new] for o in old]
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


def _ratio(old_text: str, new_text: str) -> float:
    """Similarity of two paragraphs, or 0.0 when below the pairing threshold."""
    ratio = SequenceMatcher(None, old_text, new_text, autojunk=False).ratio()
    return ratio if ratio >= _PAIRING_THRESHOLD else 0.0


def _alignment_scores(ratios: Sequence[Sequence[float]]) -> list[list[float]]:
    """Best total similarity of an order-preserving pairing of ``old[i:]`` with ``new[j:]``."""
    rows, cols = len(ratios), len(ratios[0]) if ratios else 0
    best = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            paired = ratios[i][j] + best[i + 1][j + 1] if ratios[i][j] else 0.0
            best[i][j] = max(paired, best[i + 1][j], best[i][j + 1])
    return best


def _inline(old_text: str, new_text: str) -> str:
    """The new paragraph with removed words as ``[-...-]`` and added words as ``{+...+}``."""
    # Tokens are whitespace-separated words with their punctuation attached, so a changed
    # "diez," shows as one removed and one added word. Removing the marks and the added words
    # gives back the old text; removing the marks and the removed words gives the new one.
    old_words, new_words = old_text.split(), new_text.split()
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
