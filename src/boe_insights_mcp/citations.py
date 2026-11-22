"""Use case of ``verify_citation``: check citations of laws and articles against the BOE text.

For each citation it resolves the law (``legislation.resolve_reference``), locates and fetches the
article with the same steps ``read_article`` uses (``articles.load_article``, ``law_state``,
``law_repeal_note``), and compares quoted words with the article's versions (``citation``, which
is pure). The facts found are returned as ``schemas``; nothing here judges legal correctness.

A call checks its citations in order within a time budget. What is fetched is cached by ``client``,
so repeated laws cost nothing extra, and citations left over when the budget ends are returned
unchecked so the caller can ask again with just those.

Layering: ``server`` calls ``verify_citations`` -> ``legislation``, ``articles`` (shared steps) and
``citation`` (matching) -> ``client``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime

from boe_insights_mcp import articles, legislation
from boe_insights_mcp.citation import (
    CitationMatch,
    CitationStatus,
    QuoteMatch,
    citation_status,
    comparison_tokens,
    match_versions,
)
from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.clock import madrid_today, utc_now
from boe_insights_mcp.errors import BoeError, describe_error
from boe_insights_mcp.schemas import (
    ArticleCandidate,
    CitationCheck,
    CitationRequest,
    LawBrief,
    OtherVersionInfo,
    Provenance,
    QuoteMatchInfo,
    VerifyCitationResult,
)
from boe_insights_mcp.versions import state_on

__all__ = ["CITATION_TIME_LIMIT", "MAX_CITATIONS", "verify_citations"]

MAX_CITATIONS = 10
# Same budget as the other multi-request tools: no new citation starts in the last 5 seconds of 40.
CITATION_TIME_LIMIT = 40.0
_TIME_MARGIN = 5.0
_MAX_MATCHED_CHARS = 600
_MAX_OMITTED_CHARS = 300
# Candidates of any kind are few and short: they only have to let the model choose one.
_MAX_CANDIDATES = 5
_MAX_CANDIDATE_TITLE = 200
_NOT_CHECKED_TIME = "Not checked: time limit reached. Call again with the remaining citations."
_EDITORIAL_NOTE = (
    "The words match an editorial note of the BOE about this article, not the article's text."
)

_GAVE_UP_NOTE = (
    "The quote was too scrambled to compare in the time allowed; it does not match the text "
    "as written."
)


def _cap(text: str, limit: int = _MAX_MATCHED_CHARS) -> str:
    """Cut ``text`` to ``limit`` characters, the last one being "…" when cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _quote_info(match: QuoteMatch | None) -> QuoteMatchInfo:
    """The tool-facing form of a match; no version in effect reads as "none"."""
    if match is None:
        return QuoteMatchInfo(level="none", similarity=0.0, matched_text="", differences="")
    return QuoteMatchInfo(
        level=match.level,
        similarity=round(match.similarity, 2),
        matched_text=_cap(match.matched_text),
        differences=match.differences,
        flags=list(match.flags),
        omitted=[_cap(text, _MAX_OMITTED_CHARS) for text in match.omitted],
    )


def _other_version(found: CitationMatch) -> OtherVersionInfo | None:
    """The better-matching other version, or ``None`` when the current one is the best."""
    other = found.best_other
    if other is None or other.relation == "current":
        return None
    return OtherVersionInfo(
        relation=other.relation,
        starts_on=other.starts_on,
        ends_on=other.ends_on,
        level=other.match.level,
        similarity=round(other.match.similarity, 2),
        matched_text=_cap(other.match.matched_text),
        differences=other.match.differences,
    )


def _article_candidates(
    candidates: Sequence[ArticleCandidate],
) -> list[LawBrief | ArticleCandidate]:
    """At most ``_MAX_CANDIDATES`` article candidates with their titles cut."""
    return [
        ArticleCandidate(block_id=c.block_id, title=_cap(c.title, _MAX_CANDIDATE_TITLE))
        for c in candidates[:_MAX_CANDIDATES]
    ]


def _law_candidates(candidates: Sequence[LawBrief]) -> list[LawBrief | ArticleCandidate]:
    """At most ``_MAX_CANDIDATES`` law candidates with their titles cut."""
    return [
        c.model_copy(update={"title": _cap(c.title, _MAX_CANDIDATE_TITLE)})
        for c in candidates[:_MAX_CANDIDATES]
    ]


def _validate(citations: Sequence[CitationRequest]) -> None:
    """Raise ``ValueError`` for a bad argument, before any request is made.

    A quote without a single word cannot be compared, and an article wording longer than the
    locator accepts would only fail later, in the middle of the call; both are caught here so
    that one bad citation does not leave the others half checked.
    """
    if not 1 <= len(citations) <= MAX_CITATIONS:
        raise ValueError(f"citations must hold between 1 and {MAX_CITATIONS} items")
    for number, citation in enumerate(citations, start=1):
        if not citation.law.strip():
            raise ValueError(f"citation {number}: law must not be empty")
        if citation.block_id is None and not (citation.article or "").strip():
            raise ValueError(f"citation {number}: give article or block_id")
        if citation.block_id is not None and not citation.block_id.strip():
            raise ValueError(f"citation {number}: block_id must not be empty")
        if citation.article is not None and len(citation.article) > articles.MAX_ARTICLE_CHARS:
            raise ValueError(
                f"citation {number}: article must be at most {articles.MAX_ARTICLE_CHARS} "
                "characters: pass only the article or provision designation"
            )
        if citation.quote is not None and not comparison_tokens(citation.quote):
            raise ValueError(f"citation {number}: quote must contain words")


async def _check(
    client: BoeClient,
    citation: CitationRequest,
    moment: datetime,
    today: date,
    out_of_time: Callable[[], bool],
) -> tuple[CitationCheck, datetime | None]:
    """Check one citation; also return the BOE's update stamp of the article, when it was read.

    ``out_of_time`` says whether the call's time budget is spent; matching is not started then.
    Raises ``BoeError`` for a failed request; the caller turns that into an unchecked citation.
    """
    resolved = await legislation.resolve_reference(client, citation.law, now=moment)
    if resolved.status != "resolved":
        # Not resolved means no law, or several that could be meant (a lone uncertain match is
        # ambiguous too): either way the article cannot be looked up.
        status: CitationStatus = (
            "law_not_found" if resolved.status == "not_found" else "law_ambiguous"
        )
        return (
            CitationCheck(
                law=citation.law,
                article=citation.article,
                status=status,
                candidates=_law_candidates(resolved.matches),
                note=resolved.hint,
            ),
            None,
        )
    law = resolved.matches[0]
    on = citation.as_of if citation.as_of is not None else today
    # A block id replaces the wording, as in ``read_article``: it is how an ambiguous article is
    # told apart.
    loaded = await articles.load_article(
        client,
        law.identifier,
        None if citation.block_id is not None else citation.article,
        citation.block_id,
        moment,
        today,
    )
    location = loaded.location
    if loaded.block is None:
        ambiguous = location.outcome == "ambiguous"
        return (
            CitationCheck(
                law=citation.law,
                article=citation.article,
                status="article_ambiguous" if ambiguous else "article_not_found",
                law_id=law.identifier,
                law_title=law.title,
                as_of=on,
                candidates=_article_candidates(location.candidates),
                note=location.hint,
            ),
            location.provenance.data_updated_at,
        )

    block, repeal = loaded.block, loaded.repeal
    as_of_given = citation.as_of is not None
    state = articles.law_state(state_on(block, on), repeal, on, as_of_given=as_of_given)
    # Whether the whole law is repealed ON THE DATE, which is what the status needs: a law
    # repealed after ``as_of`` was in force on it (``read_article`` treats it the same way).
    repealed_then = (
        articles.law_state("in_force", repeal, on, as_of_given=as_of_given) == "repealed"
    )
    quote = citation.quote
    found: CitationMatch | None = None
    if quote is not None:
        # The comparison is CPU work proportional to the article's versions (the Código Civil has
        # articles with dozens), so it runs in a worker thread like the diffs in ``articles``.
        # The thread cannot be cancelled, so the budget is checked before it starts.
        if out_of_time():
            return (
                _not_checked(citation, _NOT_CHECKED_TIME),
                location.provenance.data_updated_at,
            )
        found = await asyncio.to_thread(match_versions, quote, block, on)
    status = citation_status(
        law_outcome="found",
        article_outcome="found",
        state=state,
        law_repealed=repealed_then,
        quote_given=quote is not None,
        match=found,
    )
    notes: list[str] = []
    if loaded.ignored:
        notes.append(
            f"Checked against the whole article; '{loaded.ignored}' was not used to narrow "
            "the text."
        )
    if found is not None and found.gave_up and status in ("text_differs", "quote_not_found"):
        # The search for an approximate match ran out of its work budget, so "none" is not a
        # measurement: say so, and do not let a half-search suggest a near miss.
        status = "quote_not_found"
        notes.append(_GAVE_UP_NOTE)
    elif status == "quote_not_found" and found is not None and found.editorial:
        notes.append(_EDITORIAL_NOTE)
    if (sentence := articles.law_repeal_note(repeal, on, state)) is not None:
        notes.append(sentence)
    other = _other_version(found) if found is not None else None
    return (
        CitationCheck(
            law=citation.law,
            article=citation.article,
            status=status,
            law_id=law.identifier,
            law_title=law.title,
            block_id=location.block_id,
            article_label=location.block_title,
            state=state,
            law_repealed=repeal.repealed if repeal is not None else None,
            as_of=on,
            quote_match=(
                _quote_info(found.current.match if found.current is not None else None)
                if found is not None
                else None
            ),
            other_version=other,
            note=" ".join(notes) or None,
        ),
        location.provenance.data_updated_at,
    )


def _not_checked(citation: CitationRequest, note: str) -> CitationCheck:
    """A citation that got no status, with the reason in its note."""
    return CitationCheck(law=citation.law, article=citation.article, status=None, note=note)


async def verify_citations(
    client: BoeClient,
    citations: Sequence[CitationRequest],
    *,
    now: datetime | None = None,
    today: date | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit: float = CITATION_TIME_LIMIT,
) -> VerifyCitationResult:
    """Check 1 to ``MAX_CITATIONS`` citations in order, within ``time_limit`` seconds.

    No citation starts once fewer than 5 seconds of the limit remain: it and the later ones come
    back with ``status`` ``None`` and are counted in ``not_checked``. A citation whose requests
    fail with a ``BoeError`` is unchecked too (its note carries the error) and does not stop the
    others. Raises ``ValueError`` for a bad argument, before any request.
    """
    started = clock()
    _validate(citations)
    moment = utc_now(now)
    current = madrid_today(moment, today)
    results: list[CitationCheck] = []
    newest: datetime | None = None
    for citation in citations:
        if time_limit - (clock() - started) < _TIME_MARGIN:
            results.append(_not_checked(citation, _NOT_CHECKED_TIME))
            continue
        try:
            check, stamp = await _check(
                client, citation, moment, current, lambda: clock() - started >= time_limit
            )
        except BoeError as error:
            # One citation failing must not sink the others (the BOE could be briefly down for
            # one law). ``str(error)`` can carry the API's own (Spanish) text, so the note uses
            # the curated message the tool errors use.
            results.append(
                _not_checked(
                    citation,
                    f"Not checked: {describe_error(error, 'verify_citation')} "
                    "Call again with this citation.",
                )
            )
            continue
        results.append(check)
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    unchecked = sum(1 for check in results if check.status is None)
    return VerifyCitationResult(
        results=results,
        checked=len(results) - unchecked,
        not_checked=unchecked,
        provenance=Provenance(generated_at=moment, data_updated_at=newest),
    )
