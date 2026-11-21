"""Checking words attributed to a law against the consolidated text of an article.

This module is pure: no I/O, no clock, no network. It answers three questions about a quote:
how closely it matches one text (``match_quote``), how it matches each version of an article
(``match_versions``), and what single status the facts add up to (``citation_status``). The tool
layer fetches the law and the article, calls these functions and converts the frozen dataclasses
into its own schemas. It reports facts about wording and never judges legal correctness.

It builds on ``models`` (``Block``) and ``versions`` (``version_on``, which decides which
version is in effect on a date, so the version rules live in one place).

Words are compared as "comparison tokens" (``comparison_tokens``): case and typography never
matter, accents do. Everything is deterministic: equal inputs give equal outputs, ties included.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from fractions import Fraction
from typing import Literal

from boe_insights_mcp.models import Block
from boe_insights_mcp.versions import ArticleState, version_on

__all__ = [
    "CitationMatch",
    "CitationStatus",
    "MatchLevel",
    "QuoteMatch",
    "VersionMatch",
    "VersionRelation",
    "citation_status",
    "comparison_tokens",
    "match_quote",
    "match_versions",
]

MatchLevel = Literal["exact", "close", "partial", "none"]
VersionRelation = Literal["current", "earlier", "later"]
CitationStatus = Literal[
    "matches",
    "matches_older_version",
    "matches_later_version",
    "text_differs",
    "quote_not_found",
    "not_in_force",
    "article_not_found",
    "article_ambiguous",
    "law_not_found",
    "law_ambiguous",
]
Outcome = Literal["found", "ambiguous", "not_found"]

_CLOSE = Fraction(85, 100)
_PARTIAL = Fraction(1, 2)
# A quote this short matches by chance too easily: "uno dos tres" against "uno dos x tres" would
# score 0.75. Below this many tokens the only honest answers are "exact" and "none".
_MIN_APPROXIMATE_TOKENS = 4
_DIFFERENCES_CAP = 600
# Similarity of two versions is compared with a margin so that a near-tie between versions is
# not reported as "this older wording is better"; the tolerance absorbs float subtraction
# (0.60 - 0.55 is 0.04999999999999993, yet it is the margin pair the rule means to accept).
_MARGIN = 0.05
_MARGIN_TOLERANCE = 1e-9
_SEEDS = 3

# Quotation marks and the soft hyphen are invisible to the comparison: they are dropped without
# splitting the word, so an apostrophe inside a word like "d'Aragó" or a soft hyphen at a line
# break never creates a mismatch ("daragó" is one word, not "d" and "aragó").
_TRANSPARENT = "\u00ab\u00bb\u201c\u201d\u2018\u2019\u201e\u201a\u2039\u203a\"'\u00ad"
# Combining marks belong to the letter before them: a decomposed "é" is an "e" plus U+0301 and
# must stay one word with its neighbours until NFC recomposes it.
_MARKS = "̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯"
_WORD_CHAR = rf"(?:[^\W_]|[{_MARKS}])"
# A transparent character only continues a word when a word character follows it, so the span of
# a token (used to cut the original text) never ends on a closing quotation mark.
_TOKEN = re.compile(rf"{_WORD_CHAR}(?:{_WORD_CHAR}|[{re.escape(_TRANSPARENT)}]+(?={_WORD_CHAR}))*")
_TRANSPARENT_CHARS = re.compile(f"[{re.escape(_TRANSPARENT)}]")
_OMISSION = re.compile(r"\s*(?:\(\.\.\.\)|\[\.\.\.\]|…|\.{3,})\s*")


# --- data ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteMatch:
    """How closely a quote matches one text."""

    level: MatchLevel
    similarity: float
    """0.0 to 1.0."""
    matched_text: str
    """The passage of the original text the quote was aligned to; ``""`` for ``none``."""
    differences: str
    """Inline diff of quote against passage, ``""`` when exact or none."""
    segments: int
    """Number of quote segments (1 unless the quote has omission marks)."""


@dataclass(frozen=True)
class VersionMatch:
    """The match of a quote against one version of an article."""

    position: int
    """1-based index of the version in chronological order."""
    starts_on: date | None
    ends_on: date | None
    """The day before the next version starts; ``None`` for the latest."""
    match: QuoteMatch
    relation: VersionRelation
    """``current`` for the version in effect on the date, ``later`` for one that starts after
    it, ``earlier`` otherwise."""


@dataclass(frozen=True)
class CitationMatch:
    """The quote matched against the version in effect and the best other version."""

    current: VersionMatch | None
    """The version in effect on the date; ``None`` if none was."""
    best_other: VersionMatch | None
    """The best-matching other version, only when it is strictly better than ``current``."""


# --- 1.1 comparison tokens ----------------------------------------------------------------------


def _fold(raw: str) -> str:
    """Normalise one raw word: NFC, case folded, recomposed again."""
    # Accents are kept and case is folded: in a legal text "término" (term) and "termino" (I
    # finish) are different words, so dropping accents would hide a real change of wording, while
    # capitalisation is typography ("Los convenios" opens a sentence, "los convenios" does not).
    # NFC runs on both sides of the fold because casefolding can leave a decomposed sequence.
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", raw).casefold())


def _scan(text: str) -> list[tuple[str, int, int]]:
    """The comparison tokens of ``text`` with the character span of each in ``text``."""
    # Spans index the ORIGINAL string, which is why the scan works on it directly instead of on a
    # normalised copy: ``matched_text`` must be cut verbatim from the law.
    return [
        (
            _fold(_TRANSPARENT_CHARS.sub("", found.group())),
            found.start(),
            found.end(),
        )
        for found in _TOKEN.finditer(text)
    ]


def comparison_tokens(text: str) -> list[str]:
    """The words of ``text`` as used to compare a quote with a law's text.

    Tokens are maximal runs of letters, digits and the ordinal indicators ``ª`` and ``º``; every
    other character separates, dashes included. Quotation marks and soft hyphens are dropped
    without splitting a word. Case is folded and accents are kept.
    """
    return [token for token, _, _ in _scan(text)]


# --- 1.2 matching a quote against one text ------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    """One part of a quote between omission marks: comparison tokens and original spellings."""

    tokens: list[str]
    words: list[str]


@dataclass(frozen=True)
class _Text:
    """A text prepared for matching: comparison tokens and the original word of each."""

    source: str
    tokens: list[str]
    spans: list[tuple[int, int]]

    def word(self, index: int) -> str:
        """The original spelling of token ``index``, without soft hyphens."""
        start, end = self.spans[index]
        return self.source[start:end].replace("­", "")

    def passage(self, first: int, last: int) -> str:
        """The original text from the start of token ``first`` to the end of token ``last``."""
        return self.source[self.spans[first][0] : self.spans[last][1]]


@dataclass(frozen=True)
class _Aligned:
    """The window chosen for one segment: token indices (inclusive) and matching tokens."""

    first: int
    last: int
    matched: int


def _prepare_text(text: str) -> _Text:
    """Tokenise ``text`` once for matching."""
    scanned = _scan(text)
    return _Text(text, [t for t, _, _ in scanned], [(s, e) for _, s, e in scanned])


def _split_quote(quote: str) -> list[_Segment]:
    """The non-empty segments of a quote, split at omission marks.

    Raises ``ValueError`` when the quote has no token at all.
    """
    segments: list[_Segment] = []
    for part in _OMISSION.split(quote):
        scanned = _scan(part)
        if scanned:
            segments.append(
                _Segment(
                    tokens=[t for t, _, _ in scanned],
                    words=[part[s:e].replace("­", "") for _, s, e in scanned],
                )
            )
    if not segments:
        raise ValueError("The quote has no words to compare.")
    return segments


def match_quote(quote: str, text: str) -> QuoteMatch:
    """Compare ``quote`` with ``text``.

    The quote may contain omission marks (``(...)``, ``[...]``, ``…``, three or more dots); the
    segments between them must appear in order. Returns ``exact`` when every segment occurs as a
    contiguous run of words, otherwise the best approximate alignment graded ``close``,
    ``partial`` or ``none``. A quote with fewer than 4 words can only be ``exact`` or ``none``.
    Raises ``ValueError`` when the quote contains no words.
    """
    return _match_prepared(_split_quote(quote), _prepare_text(text))


def _match_prepared(segments: Sequence[_Segment], text: _Text) -> QuoteMatch:
    """``match_quote`` for a quote already split into segments and a prepared text."""
    exact = _exact_runs(segments, text)
    if exact is not None:
        first, last = exact[0][0], exact[-1][1]
        return QuoteMatch("exact", 1.0, text.passage(first, last), "", len(segments))
    total = sum(len(s.tokens) for s in segments)
    if total < _MIN_APPROXIMATE_TOKENS:
        return QuoteMatch("none", 0.0, "", "", len(segments))

    alignments: list[_Aligned | None] = []
    position = 0
    weighted = Fraction(0)
    for segment in segments:
        aligned = _best_window(segment.tokens, text.tokens, position)
        alignments.append(aligned)
        if aligned is not None:
            length = aligned.last - aligned.first + 1
            weighted += Fraction(
                aligned.matched * len(segment.tokens), max(len(segment.tokens), length)
            )
            position = aligned.last + 1
    similarity = weighted / total
    if similarity < _PARTIAL:
        return QuoteMatch("none", float(similarity), "", "", len(segments))
    level: MatchLevel = "close" if similarity >= _CLOSE else "partial"
    found = [a for a in alignments if a is not None]
    return QuoteMatch(
        level,
        float(similarity),
        text.passage(found[0].first, found[-1].last),
        _differences(segments, alignments, text),
        len(segments),
    )


def _exact_runs(segments: Sequence[_Segment], text: _Text) -> list[tuple[int, int]] | None:
    """Token spans of every segment as contiguous runs in order, or ``None`` if one is missing."""
    runs: list[tuple[int, int]] = []
    position = 0
    for segment in segments:
        start = _find_run(segment.tokens, text.tokens, position)
        if start is None:
            return None
        end = start + len(segment.tokens)
        runs.append((start, end - 1))
        position = end
    return runs


def _find_run(needle: Sequence[str], tokens: Sequence[str], start: int) -> int | None:
    """Index of the first occurrence of ``needle`` in ``tokens`` at or after ``start``."""
    # The earliest occurrence is also the best choice for the next segment: it leaves the most
    # text to the right, so greedy placement finds a placement whenever one exists.
    first = needle[0]
    size = len(needle)
    at = start
    while True:
        try:
            at = tokens.index(first, at)
        except ValueError:
            return None
        if list(tokens[at : at + size]) == list(needle):
            return at
        at += 1


def _evaluate(
    segment: Sequence[str], tokens: Sequence[str], first: int, last: int
) -> tuple[int, int, int] | None:
    """Matching tokens of ``segment`` against ``tokens[first..last]``, when the window is valid.

    Returns ``(matched, first_offset, last_offset)`` of the first and last matched token inside
    the window, or ``None`` when no token matches. The window is only a legitimate candidate
    when both offsets touch its ends; callers check that.
    """
    window = tokens[first : last + 1]
    blocks = SequenceMatcher(None, segment, window, autojunk=False).get_matching_blocks()[:-1]
    if not blocks:
        return None
    matched = sum(block.size for block in blocks)
    return matched, blocks[0].b, blocks[-1].b + blocks[-1].size - 1


def _overlaps(
    tokens: Sequence[str], positions: Sequence[int], wanted: Counter[str], width: int
) -> list[int]:
    """For each candidate start, the most quote words a window of ``width`` tokens could match.

    The count is the multiset overlap of the window with the segment, a ceiling on what any
    in-order alignment can match. It is kept with two pointers, so the whole pass is linear.
    """
    counts: Counter[str] = Counter()
    overlap = 0
    left = right = positions[0]
    result: list[int] = []
    for start in positions:
        while left < start:
            token = tokens[left]
            if token in wanted:
                if counts[token] <= wanted[token]:
                    overlap -= 1
                counts[token] -= 1
            left += 1
        stop = min(start + width, len(tokens))
        while right < stop:
            token = tokens[right]
            if token in wanted:
                counts[token] += 1
                if counts[token] <= wanted[token]:
                    overlap += 1
            right += 1
        result.append(overlap)
    return result


def _best_window(segment: Sequence[str], tokens: Sequence[str], lo: int) -> _Aligned | None:
    """The window of ``tokens[lo:]`` that best matches ``segment``, or ``None`` if none does.

    The score of a window is ``matching / max(len(segment), len(window))`` with the matching
    count of ``SequenceMatcher``, and a window must start and end on a matched token. Ties go
    to the earliest start, then the shorter window. The result is the exact optimum; the search
    is only pruned with bounds that can never discard a window that would win:

    * A window can only start or end at a token that occurs in the segment (otherwise that end
      is unmatched and trimming it gives a better score), so only those positions are tried.
    * No alignment matches more tokens than the multiset overlap of window and segment, so
      ``min(len(segment), overlap) / max(len(segment), width)`` is an upper bound on the score,
      cheap to maintain while a window grows. A window whose bound cannot beat the best score
      known is never aligned, and growing stops once even a perfect overlap could not beat it.
    * The best known score starts from the densest windows of the segment's own length, which
      are real windows scored for real, so a quote that nearly appears in the text prunes
      almost everything at once. Scanning starts in order and replaces the best only on a strictly
      higher score, which gives the tie-break for free.
    """
    size = len(segment)
    wanted = Counter(segment)
    positions = [p for p in range(lo, len(tokens)) if tokens[p] in wanted]
    if not positions:
        return None

    floor = _seed_score(segment, tokens, positions, wanted)
    # Windows with a bound of at least ``floor`` are worth looking at. The prefilter asks only
    # for starts whose longest still-useful window holds enough of the segment's words.
    bound_n, bound_d, strict = floor[0], floor[1], False
    widest = min(len(tokens) - lo, (size * bound_d) // bound_n)
    capacity = _overlaps(tokens, positions, wanted, widest)

    best: tuple[int, int, int, int] | None = None  # matched, denominator, first, last

    def beats(numerator: int, denominator: int) -> bool:
        """Whether a score could still improve on the best known one."""
        left, right = numerator * bound_d, bound_n * denominator
        return left > right if strict else left >= right

    for a, start in enumerate(positions):
        if capacity[a] * bound_d < bound_n * size:
            continue
        counts: Counter[str] = Counter()
        overlap = 0
        for b in range(a, len(positions)):
            end = positions[b]
            length = end - start + 1
            if not beats(size, length):
                break
            token = tokens[end]
            counts[token] += 1
            if counts[token] <= wanted[token]:
                overlap += 1
            if not beats(min(size, overlap), max(size, length)):
                continue
            outcome = _evaluate(segment, tokens, start, end)
            if outcome is None or outcome[1] != 0 or outcome[2] != length - 1:
                continue
            matched, denominator = outcome[0], max(size, length)
            if best is None or matched * best[1] > best[0] * denominator:
                best = (matched, denominator, start, end)
                if matched * bound_d >= bound_n * denominator:
                    bound_n, bound_d, strict = matched, denominator, True
    if best is None:
        return None
    return _Aligned(first=best[2], last=best[3], matched=best[0])


def _seed_score(
    segment: Sequence[str], tokens: Sequence[str], positions: Sequence[int], wanted: Counter[str]
) -> tuple[int, int]:
    """A score some real window achieves, used to start the pruning; ``(matched, denominator)``.

    Takes the windows of the segment's own length holding the most of its words (a few, spread
    apart) and scores each for real. Falls back to a lone matching word, which always scores
    ``1 / len(segment)``, so the returned score never exceeds the true optimum.
    """
    size = len(segment)
    best = (1, size)
    capacity = _overlaps(tokens, positions, wanted, size)
    chosen: list[int] = []
    for a in sorted(range(len(positions)), key=lambda i: (-capacity[i], i)):
        if len(chosen) == _SEEDS:
            break
        if any(abs(positions[a] - positions[c]) <= size // 2 for c in chosen):
            continue
        chosen.append(a)
        first = positions[a]
        last = positions[bisect_right(positions, first + size - 1) - 1]
        for _ in range(3):
            outcome = _evaluate(segment, tokens, first, last)
            if outcome is None:
                break
            matched, low, high = outcome
            if low == 0 and high == last - first:
                # Compare matched/denominator by cross-multiplication to stay in integers.
                denominator = max(size, last - first + 1)
                if matched * best[1] > best[0] * denominator:
                    best = (matched, denominator)
                break
            # The alignment left the ends unmatched: retry on the trimmed window.
            first, last = first + low, first + high
    return best


def _differences(
    segments: Sequence[_Segment], alignments: Sequence[_Aligned | None], text: _Text
) -> str:
    """Inline diff of the quote against the aligned passages, capped at 600 characters.

    ``[-words-]`` are words only in the quote and ``{+words+}`` words only in the law. The
    alignment runs on the comparison tokens, so case, punctuation and typography never show up
    as differences, but each word is displayed in its original spelling (the law's for words that
    agree, so the reader sees the text as published).
    """
    parts: list[str] = []
    for segment, aligned in zip(segments, alignments, strict=True):
        if aligned is None:
            parts.append(f"[-{' '.join(segment.words)}-]")
            continue
        window = range(aligned.first, aligned.last + 1)
        law_tokens = [text.tokens[i] for i in window]
        pieces: list[str] = []
        opcodes = SequenceMatcher(None, segment.tokens, law_tokens, autojunk=False).get_opcodes()
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                pieces.extend(text.word(aligned.first + j) for j in range(j1, j2))
                continue
            if i2 > i1:
                pieces.append(f"[-{' '.join(segment.words[i1:i2])}-]")
            if j2 > j1:
                pieces.append(
                    f"{{+{' '.join(text.word(aligned.first + j) for j in range(j1, j2))}+}}"
                )
        parts.append(" ".join(pieces))
    joined = " (...) ".join(parts)
    if len(joined) > _DIFFERENCES_CAP:
        return joined[:_DIFFERENCES_CAP].rstrip() + "…"
    return joined


# --- 1.3 matching across an article's versions --------------------------------------------------

_RANK: dict[MatchLevel, int] = {"none": 0, "partial": 1, "close": 2, "exact": 3}


def match_versions(quote: str, block: Block, on: date) -> CitationMatch:
    """Match ``quote`` against every version of ``block``.

    ``current`` is the version in effect on ``on``. ``best_other`` is the best-matching other
    version, earlier or later, and only when it matches strictly better than ``current``: a
    higher level, or the same level with a similarity higher by at least 0.05. Among other
    versions of equal level and similarity the latest wins. Raises ``ValueError`` when the quote
    contains no words.
    """
    segments = _split_quote(quote)
    in_effect = version_on(block, on)
    versions = block.versions
    current: VersionMatch | None = None
    others: list[VersionMatch] = []
    for index, version in enumerate(versions):
        following = versions[index + 1].starts_on if index + 1 < len(versions) else None
        starts = version.starts_on
        relation: VersionRelation
        if version is in_effect:
            relation = "current"
        elif starts is not None and starts > on:
            relation = "later"
        else:
            relation = "earlier"
        text = _prepare_text("\n".join(p.text for p in version.paragraphs))
        found = VersionMatch(
            position=index + 1,
            starts_on=starts,
            ends_on=None if following is None else following - timedelta(days=1),
            match=_match_prepared(segments, text),
            relation=relation,
        )
        if relation == "current":
            current = found
        else:
            others.append(found)

    # A version that matches nothing is never a useful "other wording", even if it scores a
    # little above a current version that matches nothing either (similarity noise).
    candidates = [v for v in others if v.match.level != "none"]
    if not candidates:
        return CitationMatch(current=current, best_other=None)
    # ``max`` keeps the first of equal keys and the position is part of the key, so among equal
    # level and similarity the latest version wins: it is the most recent wording that says it.
    best = max(candidates, key=lambda v: (_RANK[v.match.level], v.match.similarity, v.position))
    if current is None or _clearly_better(best.match, current.match):
        return CitationMatch(current=current, best_other=best)
    return CitationMatch(current=current, best_other=None)


def _clearly_better(other: QuoteMatch, current: QuoteMatch) -> bool:
    """Whether ``other`` matches strictly better than ``current`` by level or by the margin."""
    # Same level needs a margin so that a 1-word wobble between two near-identical versions does
    # not make the in-force text look like the wrong one; a higher level always counts because
    # the levels are the thresholds a reader acts on.
    if _RANK[other.level] != _RANK[current.level]:
        return _RANK[other.level] > _RANK[current.level]
    return other.similarity - current.similarity >= _MARGIN - _MARGIN_TOLERANCE


# --- 1.4 the outcome of one check ---------------------------------------------------------------


def citation_status(
    *,
    law_outcome: Outcome,
    article_outcome: Outcome | None,
    state: ArticleState | None,
    law_repealed: bool,
    quote_given: bool,
    match: CitationMatch | None,
) -> CitationStatus:
    """The single status that the facts of one citation add up to.

    Precedence, first that applies: the law is not found or ambiguous, the article is not found
    or ambiguous; then, with a quote, a better other version (exact or close), no match at all,
    an exact match that is not in force, an exact match in force, anything else
    (``text_differs``); without a quote, whether the article is in force. Raises ``ValueError``
    when the law was found but no article outcome is given.
    """
    if law_outcome == "not_found":
        return "law_not_found"
    if law_outcome == "ambiguous":
        return "law_ambiguous"
    if article_outcome is None:
        raise ValueError("A found law needs an article outcome.")
    if article_outcome == "not_found":
        return "article_not_found"
    if article_outcome == "ambiguous":
        return "article_ambiguous"

    not_in_force = state != "in_force" or law_repealed
    if not quote_given:
        return "not_in_force" if not_in_force else "matches"

    other = None if match is None else match.best_other
    if other is not None and other.match.level in ("exact", "close"):
        return "matches_later_version" if other.relation == "later" else "matches_older_version"
    current = None if match is None or match.current is None else match.current.match.level
    if current is None or current == "none":
        return "quote_not_found"
    # Only an exact quote can be "not in force": a misquote of a repealed article is first of all
    # a misquote, and the state is reported separately.
    if current == "exact":
        return "not_in_force" if not_in_force else "matches"
    return "text_differs"
