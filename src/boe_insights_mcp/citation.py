"""Checking words attributed to a law against the consolidated text of an article.

This module is pure: no I/O, no clock, no network. It answers three questions about a quote:
how closely it matches one text (``match_quote``), how it matches each version of an article
(``match_versions``), and what single status the facts add up to (``citation_status``). The tool
layer fetches the law and the article, calls these functions and converts the frozen dataclasses
into its own schemas. It reports facts about wording and never judges legal correctness.

It builds on ``models`` (``Block``) and ``versions`` (``version_on``, which decides which
version is in effect on a date, so the version rules live in one place).

Words are compared as "comparison tokens" (``comparison_tokens``): case and typography never
matter, accents do. Only the article's own words are compared; the notes the BOE adds about it
are kept apart (``BlockVersion.own_paragraphs``). Everything is deterministic: equal inputs give
equal outputs, ties included.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from fractions import Fraction
from itertools import pairwise
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
    "in_force",
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
# Raised from 1/2: random sentences of other articles of the same law reached "partial" in about
# 1 case in 85 at 0.5, mostly through shared legal boilerplate ("de la Administración General del
# Estado"), and a "partial" reads as "the quote may come from here".
_PARTIAL = Fraction(3, 5)
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
# The work one ``match_quote`` or ``match_versions`` call may spend, counted in tokens compared and
# alignment steps (see ``_Budget``), never in seconds, so the same input always gets the same
# answer on any machine. When every quote word occurs in the text (a long quote made of the
# article's own words in another order) the pruning bounds in ``_best_window`` prove nothing and
# the search is quadratic or worse: 250 shuffled words against a 700-word article took minutes.
# The server will be exposed over HTTP, so no input may hold a worker for long. The figure sits
# about forty times the most the realistic cases in the tests need (about 24,000 units: a
# 350-word quote from another article, a 5,000-character quote, a 300-token near-match in 20,000
# tokens) and lets the shuffled worst case end in under two seconds on a development laptop.
_WORK_BUDGET = 1_000_000

# Words whose presence in a difference can reverse or change the figure of a provision. The
# comparison reports them as flags because the similarity score treats "no" like any other word.
_NEGATIONS = frozenset({"no", "ni", "nunca", "jamás", "tampoco", "sin", "salvo", "excepto"})
# "uno" and "una" are left out: as articles ("una vez inscritos") they are far more common than
# as figures, and flagging them would mark ordinary rewordings as changed numbers.
_NUMBER_WORDS = frozenset(
    {
        *("dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"),
        *("diez", "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete"),
        *("dieciocho", "diecinueve", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta"),
        *("setenta", "ochenta", "noventa", "cien", "mil"),
    }
)

# Quotation marks and the soft hyphen are invisible to the comparison: they are dropped without
# splitting the word, so an apostrophe inside a word like "d'Aragó" or a soft hyphen at a line
# break never creates a mismatch ("daragó" is one word, not "d" and "aragó").
_TRANSPARENT = "\u00ab\u00bb\u201c\u201d\u2018\u2019\u201e\u201a\u2039\u203a\"'\u00ad"
# Combining marks belong to the letter before them: a decomposed "é" is an "e" plus U+0301 and
# must stay one word with its neighbours until NFC recomposes it.
_MARKS = "̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯"
_WORD_CHAR = rf"(?:[^\W_]|[{_MARKS}])"
# Three typographic habits must not make two spellings of one word differ. They are absorbed into
# the token and removed when it is folded (``_fold_token``), so the token's span still covers the
# original spelling:
# * a dot between a word character and an ordinal indicator: "1.º" and "N.ª" are "1º" and "Nª";
# * the degree sign "°" typed in place of the masculine ordinal "º";
# * thousands separators: "3.000" is "3000" (groups of exactly three digits after the first).
_THOUSANDS = r"(?<![\d.])\d{1,3}(?:\.\d{3})+(?!\d)"
_ORDINALS = "ªº°"
# A transparent character only continues a word when a word character follows it, so the span of
# a token (used to cut the original text) never ends on a closing quotation mark.
_TOKEN = re.compile(
    rf"(?:{_THOUSANDS}|{_WORD_CHAR})"
    rf"(?:{_WORD_CHAR}|[{re.escape(_TRANSPARENT)}]+(?={_WORD_CHAR})|\.(?=[{_ORDINALS}])|°)*"
)
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
    omitted: tuple[str, ...] = ()
    """For an exact match of several segments: the text of the article skipped between each pair of
    consecutive segments, as published (``segments - 1`` entries)."""
    flags: tuple[str, ...] = ()
    """Words that can change the meaning: ``negation`` or ``number`` when such a word is among the
    differences, ``omitted_negation`` or ``omitted_number`` when it is in an omitted passage."""
    gave_up: bool = False
    """True when the search for an approximate match used up its work budget: the level is then
    ``none`` and says nothing about whether the quote is near the text."""


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
    gave_up: bool = False
    """True when ``current`` gave up (see ``QuoteMatch.gave_up``)."""
    editorial: bool = False
    """True when no version's own text matches the quote exactly or closely but the notes the BOE
    adds to some version do (``Paragraph`` footnotes and quoted notes)."""


# --- 1.1 comparison tokens ----------------------------------------------------------------------


def _fold(raw: str) -> str:
    """Normalise one raw word: NFC, case folded, recomposed again."""
    # Accents are kept and case is folded: in a legal text "término" (term) and "termino" (I
    # finish) are different words, so dropping accents would hide a real change of wording, while
    # capitalisation is typography ("Los convenios" opens a sentence, "los convenios" does not).
    # NFC runs on both sides of the fold because casefolding can leave a decomposed sequence.
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", raw).casefold())


def _token_form(raw: str) -> str:
    """The comparison form of one raw token: typography removed, then folded."""
    # The only dots a token can hold are the thousands separators and the dot before an ordinal
    # indicator that ``_TOKEN`` absorbs, and both are typography; the degree sign stands for "º".
    return _fold(_TRANSPARENT_CHARS.sub("", raw).replace(".", "").replace("°", "º"))


def _scan(text: str) -> list[tuple[str, int, int]]:
    """The comparison tokens of ``text`` with the character span of each in ``text``."""
    # Spans index the ORIGINAL string, which is why the scan works on it directly instead of on a
    # normalised copy: ``matched_text`` must be cut verbatim from the law.
    return [
        (_token_form(found.group()), found.start(), found.end()) for found in _TOKEN.finditer(text)
    ]


def comparison_tokens(text: str) -> list[str]:
    """The words of ``text`` as used to compare a quote with a law's text.

    Tokens are maximal runs of letters, digits and the ordinal indicators ``ª`` and ``º``; every
    other character separates, dashes included. Quotation marks and soft hyphens are dropped
    without splitting a word. Case is folded and accents are kept. The dot of "1.º" and "N.ª" and
    the dots of "3.000" are dropped, and the degree sign "°" equals "º".
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


class _GaveUp(Exception):
    """Raised inside the window search when the work budget is spent."""


class _Budget:
    """A deterministic allowance of work for one matching call, shared by all its searches."""

    def __init__(self, limit: int | None = None) -> None:
        """A budget of ``limit`` units (``_WORK_BUDGET`` by default); ``spent`` counts the use."""
        self.limit = _WORK_BUDGET if limit is None else limit
        self.spent = 0

    def spend(self, units: int) -> None:
        """Use ``units``; raises ``_GaveUp`` once the total passes the limit."""
        self.spent += units
        if self.spent > self.limit:
            raise _GaveUp


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
    return _match_prepared(_split_quote(quote), _prepare_text(text), _Budget())


def _match_prepared(segments: Sequence[_Segment], text: _Text, budget: _Budget) -> QuoteMatch:
    """``match_quote`` for a quote already split into segments and a prepared text.

    An exact match is found before any budget is spent. When ``budget`` runs out during the
    approximate search the result is ``none`` with ``gave_up`` set.
    """
    exact = _exact_runs(segments, text)
    if exact is not None:
        first, last = exact[0][0], exact[-1][1]
        # What an omission mark skipped is shown verbatim: the quote hides it, and one skipped
        # word ("no", a figure) can reverse what the quoted words say.
        omitted = tuple(
            text.source[text.spans[before[1]][1] : text.spans[after[0]][0]].strip()
            for before, after in pairwise(exact)
        )
        flags = _flags(comparison_tokens(" ".join(omitted)), prefix="omitted_")
        return QuoteMatch(
            "exact", 1.0, text.passage(first, last), "", len(segments), omitted, flags
        )
    total = sum(len(s.tokens) for s in segments)
    if total < _MIN_APPROXIMATE_TOKENS:
        return QuoteMatch("none", 0.0, "", "", len(segments))

    alignments: list[_Aligned | None] = []
    position = 0
    weighted = Fraction(0)
    for segment in segments:
        try:
            aligned = _best_window(
                segment.tokens,
                text.tokens,
                position,
                _segment_floor(len(segment.tokens), total),
                budget,
            )
        except _GaveUp:
            return QuoteMatch("none", 0.0, "", "", len(segments), gave_up=True)
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
    differences, flags = _differences(segments, alignments, text)
    return QuoteMatch(
        level,
        float(similarity),
        text.passage(found[0].first, found[-1].last),
        differences,
        len(segments),
        flags=flags,
    )


def _segment_floor(size: int, total: int) -> Fraction | None:
    """The lowest score one segment can have while the whole quote still reaches ``_PARTIAL``.

    The other segments score at most 1, so a segment of ``size`` words among ``total`` must score
    at least ``1 - (1 - _PARTIAL) * total / size``; below that the quote is "none" whatever the
    rest does. ``None`` when that is not above 0 (a short segment next to long ones).
    """
    floor = 1 - (1 - _PARTIAL) * Fraction(total, size)
    return floor if floor > 0 else None


def _flags(words: Iterable[str], prefix: str = "") -> tuple[str, ...]:
    """The meaning-changing kinds present among ``words``, each as ``prefix`` plus its name."""
    found = list(words)
    flags: list[str] = []
    if any(word in _NEGATIONS for word in found):
        flags.append(f"{prefix}negation")
    if any(word in _NUMBER_WORDS or any(char.isdigit() for char in word) for word in found):
        flags.append(f"{prefix}number")
    return tuple(flags)


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
    segment: Sequence[str], tokens: Sequence[str], first: int, last: int, budget: _Budget
) -> tuple[int, int, int] | None:
    """Matching tokens of ``segment`` against ``tokens[first..last]``, when the window is valid.

    Returns ``(matched, first_offset, last_offset)`` of the first and last matched token inside
    the window, or ``None`` when no token matches. The window is only a legitimate candidate
    when both offsets touch its ends; callers check that.
    """
    window = tokens[first : last + 1]
    budget.spend(len(segment) + len(window))
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


def _best_window(
    segment: Sequence[str],
    tokens: Sequence[str],
    lo: int,
    floor: Fraction | None,
    budget: _Budget,
) -> _Aligned | None:
    """The window of ``tokens[lo:]`` that best matches ``segment``, or ``None`` if none does.

    The score of a window is ``matching / max(len(segment), len(window))`` with the matching
    count of ``SequenceMatcher``, and a window must start and end on a matched token. Ties go
    to the earliest start, then the shorter window. With ``floor`` given, only windows scoring at
    least ``floor`` are looked for (``None`` is returned when there is none); among those the
    result is the exact optimum. The search is only pruned with bounds that can never discard a
    window that would win:

    * A window can only start or end at a token that occurs in the segment (otherwise that end
      is unmatched and trimming it gives a better score), so only those positions are tried.
    * No alignment matches more tokens than the multiset overlap of window and segment, so
      ``min(len(segment), overlap) / max(len(segment), width)`` is an upper bound on the score,
      cheap to maintain while a window grows. A window whose bound cannot beat the best score
      known is never aligned, and growing stops once even a perfect overlap could not beat it.
    * The best known score starts from the densest windows of the segment's own length, which
      are real windows scored for real, so a quote that nearly appears in the text prunes
      almost everything at once, or from ``floor`` when that is higher. Scanning starts in order
      and replaces the best only on a strictly higher score, which gives the tie-break for free.

    Every window step and every alignment is charged to ``budget``; ``_GaveUp`` propagates to the
    caller when it is spent.
    """
    size = len(segment)
    wanted = Counter(segment)
    positions = [p for p in range(lo, len(tokens)) if tokens[p] in wanted]
    if not positions:
        return None

    budget.spend(len(tokens) - lo)
    floor_n, floor_d = _seed_score(segment, tokens, positions, wanted, budget)
    # A quote that is nowhere in the text has no seed worth anything, and the search would then
    # try thousands of windows that all end up below the "partial" threshold and are reported as
    # "none" anyway. Starting from the threshold makes those windows prunable from the first step;
    # a window scoring exactly ``floor`` is still looked at (the first comparison is not strict).
    if floor is not None and floor.numerator * floor_d > floor_n * floor.denominator:
        floor_n, floor_d = floor.numerator, floor.denominator
    # Windows with a bound of at least ``floor`` are worth looking at. The prefilter asks only
    # for starts whose longest still-useful window holds enough of the segment's words.
    bound_n, bound_d, strict = floor_n, floor_d, False
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
            budget.spend(1)
            if not beats(size, length):
                break
            token = tokens[end]
            counts[token] += 1
            if counts[token] <= wanted[token]:
                overlap += 1
            if not beats(min(size, overlap), max(size, length)):
                continue
            outcome = _evaluate(segment, tokens, start, end, budget)
            if outcome is None or outcome[1] != 0 or outcome[2] != length - 1:
                continue
            matched, denominator = outcome[0], max(size, length)
            # The overlap bound above is only a ceiling: the real score can still fall below
            # the floor, and such a window must not be returned.
            if not beats(matched, denominator):
                continue
            if best is None or matched * best[1] > best[0] * denominator:
                best = (matched, denominator, start, end)
                if matched * bound_d >= bound_n * denominator:
                    bound_n, bound_d, strict = matched, denominator, True
    if best is None:
        return None
    return _Aligned(first=best[2], last=best[3], matched=best[0])


def _seed_score(
    segment: Sequence[str],
    tokens: Sequence[str],
    positions: Sequence[int],
    wanted: Counter[str],
    budget: _Budget,
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
            outcome = _evaluate(segment, tokens, first, last, budget)
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
) -> tuple[str, tuple[str, ...]]:
    """Inline diff of the quote against the aligned passages, and the flags of its words.

    The diff is capped at 600 characters. ``[-words-]`` are words only in the quote and
    ``{+words+}`` words only in the law. The alignment runs on the comparison tokens, so case,
    punctuation and typography never show up as differences, but each word is displayed in its
    original spelling (the law's for words that agree, so the reader sees the text as published).
    The flags look at every differing word, also past the cap.
    """
    parts: list[str] = []
    differing: list[str] = []
    for segment, aligned in zip(segments, alignments, strict=True):
        if aligned is None:
            parts.append(f"[-{' '.join(segment.words)}-]")
            differing.extend(segment.tokens)
            continue
        window = range(aligned.first, aligned.last + 1)
        law_tokens = [text.tokens[i] for i in window]
        pieces: list[str] = []
        opcodes = SequenceMatcher(None, segment.tokens, law_tokens, autojunk=False).get_opcodes()
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                pieces.extend(text.word(aligned.first + j) for j in range(j1, j2))
                continue
            differing.extend(segment.tokens[i1:i2])
            differing.extend(law_tokens[j1:j2])
            if i2 > i1:
                pieces.append(f"[-{' '.join(segment.words[i1:i2])}-]")
            if j2 > j1:
                pieces.append(
                    f"{{+{' '.join(text.word(aligned.first + j) for j in range(j1, j2))}+}}"
                )
        parts.append(" ".join(pieces))
    joined = " (...) ".join(parts)
    flags = _flags(differing)
    if len(joined) > _DIFFERENCES_CAP:
        return joined[:_DIFFERENCES_CAP].rstrip() + "…", flags
    return joined, flags


# --- 1.3 matching across an article's versions --------------------------------------------------

_RANK: dict[MatchLevel, int] = {"none": 0, "partial": 1, "close": 2, "exact": 3}


def match_versions(quote: str, block: Block, on: date) -> CitationMatch:
    """Match ``quote`` against every version of ``block``.

    ``current`` is the version in effect on ``on``. ``best_other`` is the best-matching other
    version, earlier or later, and only when it matches strictly better than ``current``: a
    higher level, or the same level with a similarity higher by at least 0.05. Among other
    versions of equal level and similarity the latest wins. Only the versions' own text is
    compared; ``editorial`` is set when the quote is found only in the notes about them. Raises
    ``ValueError`` when the quote contains no words.
    """
    segments = _split_quote(quote)
    # One budget for the whole call: a block with dozens of versions must not multiply the work.
    budget = _Budget()
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
        text = _prepare_text("\n".join(p.text for p in version.own_paragraphs))
        found = VersionMatch(
            position=index + 1,
            starts_on=starts,
            ends_on=None if following is None else following - timedelta(days=1),
            match=_match_prepared(segments, text, budget),
            relation=relation,
        )
        if relation == "current":
            current = found
        else:
            others.append(found)

    # A version that matches nothing is never a useful "other wording", even if it scores a
    # little above a current version that matches nothing either (similarity noise).
    candidates = [v for v in others if v.match.level != "none"]
    editorial = False
    if not any(
        v.match.level in ("exact", "close") for v in [*others, *([current] if current else [])]
    ):
        # Nothing in the articles' own words fits, so see whether the quote was lifted from the
        # notes the BOE adds under the text ("Se modifica el apartado 8 por ..."). Worth a second
        # pass only here: when a version already matches, the notes cannot change the answer.
        editorial = any(
            _match_prepared(
                segments,
                _prepare_text("\n".join(p.text for p in v.editorial_paragraphs)),
                budget,
            ).level
            in ("exact", "close")
            for v in versions
            if v.editorial_paragraphs
        )
    gave_up = current is not None and current.match.gave_up
    if not candidates:
        return CitationMatch(current=current, best_other=None, gave_up=gave_up, editorial=editorial)
    # ``max`` keeps the first of equal keys and the position is part of the key, so among equal
    # level and similarity the latest version wins: it is the most recent wording that says it.
    best = max(candidates, key=lambda v: (_RANK[v.match.level], v.match.similarity, v.position))
    if current is None or _clearly_better(best.match, current.match):
        return CitationMatch(current=current, best_other=best, gave_up=gave_up, editorial=editorial)
    return CitationMatch(current=current, best_other=None, gave_up=gave_up, editorial=editorial)


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
    or ambiguous; then, with a quote, no version in effect on the date (``not_in_force``), a quote
    found only in an editorial note, another version that matches exactly, no match at all, an
    exact match that is not in force, an exact match in force, anything else (``text_differs``,
    which includes another version that only matches closely); without a quote, whether the
    article is in force. Raises ``ValueError`` when the law was found but no article outcome is
    given.
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
        # Without words to compare nothing can "match": the most that is known is that the
        # article exists and is in force.
        return "not_in_force" if not_in_force else "in_force"

    if match is not None and match.current is None:
        # Before the article existed or took effect there is no text to quote from; a version
        # that starts later is reported as ``other_version``, but the citation is not in force.
        return "not_in_force"
    if match is not None and match.editorial:
        return "quote_not_found"
    other = None if match is None else match.best_other
    # A close wording of another version is only a near miss: claiming "this is the older text"
    # needs the same words, so a close other version falls through to ``text_differs`` below.
    if other is not None and other.match.level == "exact":
        return "matches_later_version" if other.relation == "later" else "matches_older_version"
    current = None if match is None or match.current is None else match.current.match.level
    if current is None or current == "none":
        if other is not None and other.match.level == "close":
            return "text_differs"
        return "quote_not_found"
    # Only an exact quote can be "not in force": a misquote of a repealed article is first of all
    # a misquote, and the state is reported separately.
    if current == "exact":
        return "not_in_force" if not_in_force else "matches"
    return "text_differs"
