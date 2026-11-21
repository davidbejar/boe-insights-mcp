"""Use cases over the text of one article: read it, compare versions, list its history.

Like ``legislation``, these functions take a :class:`BoeClient` and plain arguments, return
pydantic models from ``schemas`` and know nothing of MCP. Each one follows the same two-request
flow: fetch the law's block index, let ``locator`` find the block the person means, then fetch
that block (all its versions) and answer with ``versions`` (version selection, diff, history).

Public use cases: ``read_article``, ``compare_article_versions`` and ``get_article_history``.
A wording that matches no block or several blocks is a normal result with candidates, not an
error; an unknown law raises ``BoeNotFoundError`` and bad arguments raise ``ValueError``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.identifiers import normalise_identifier
from boe_insights_mcp.locator import LocateResult, locate
from boe_insights_mcp.models import Block, BlockVersion
from boe_insights_mcp.schemas import (
    ArticleCandidate,
    ArticleChange,
    ArticleHistoryEvent,
    ArticleHistoryResult,
    ArticleLocation,
    CompareArticleVersionsResult,
    Provenance,
    ReadArticleResult,
    VersionInfo,
    madrid_today,
    utc_now,
)
from boe_insights_mcp.versions import (
    diff_versions,
    history,
    state_on,
    version_on,
)

__all__ = [
    "DEFAULT_CHANGES",
    "DEFAULT_PAGE_CHARS",
    "MAX_CHANGES",
    "MAX_PAGE_CHARS",
    "MIN_PAGE_CHARS",
    "compare_article_versions",
    "get_article_history",
    "read_article",
]

DEFAULT_PAGE_CHARS = 6000
MIN_PAGE_CHARS = 500
MAX_PAGE_CHARS = 20000
DEFAULT_CHANGES = 40
MAX_CHANGES = 200
_MAX_UNIFIED_CHARS = 6000
_MAX_HISTORY_EVENTS = 60

_PARAGRAPH_BREAK = "\n\n"
# Compared against when an article did not exist yet: every paragraph then shows as added.
_NOTHING = BlockVersion(source_law_id="", published_on=None, effective_from=None, paragraphs=())

_AMBIGUOUS_HINT = (
    "Several blocks have this designation (a Real Decreto and the Reglamento it approves can "
    "both have an 'Artículo 1'). Repeat the call with the block_id of one of the candidates "
    "as the article."
)
_NOT_FOUND_HINT = (
    "No block of this law matches that wording. The candidates are the closest titles: retry "
    "with a block_id or a title from them."
)


def _check_range(name: str, value: int, low: int, high: int) -> None:
    """Raise ``ValueError`` unless ``low <= value <= high``."""
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def _midnight_utc(day: date | None) -> datetime | None:
    """The given day as a UTC midnight datetime (the shape of ``Provenance``), or ``None``."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC) if day is not None else None


def _candidates(located: LocateResult) -> list[ArticleCandidate]:
    """The candidate blocks of a locate result as tool-facing records."""
    return [ArticleCandidate(block_id=e.block_id, title=e.title) for e in located.candidates]


async def _load(
    client: BoeClient, law_id: str, article: str, moment: datetime
) -> tuple[ArticleLocation, Block | None]:
    """Locate the article in the law's index and fetch its block.

    Returns the location (with candidates and a hint when nothing was found) and the block, which
    is ``None`` unless exactly one block matched. Raises ``ValueError`` for a malformed law id
    or an empty ``article`` (before any request) and ``BoeNotFoundError`` for an unknown law.
    """
    # A malformed id is passed through unchanged so the client's own check raises the ValueError.
    law_id = normalise_identifier(law_id) or law_id
    if not article.strip():
        raise ValueError("article must not be empty")
    index = await client.get_text_index(law_id)
    located = locate(index, article)
    entry = located.entry
    if entry is None:
        return (
            ArticleLocation(
                law_id=law_id,
                outcome=located.outcome,
                candidates=_candidates(located),
                hint=_AMBIGUOUS_HINT if located.outcome == "ambiguous" else _NOT_FOUND_HINT,
                provenance=Provenance(generated_at=moment),
            ),
            None,
        )
    # The index date is the block's revision: it makes the cached block long-lived (api-notes 6).
    block = await client.get_text_block(law_id, entry.block_id, updated_on=entry.updated_on)
    location = ArticleLocation(
        law_id=law_id,
        outcome="found",
        block_id=entry.block_id,
        block_title=entry.title,
        provenance=Provenance(generated_at=moment, data_updated_at=_midnight_utc(entry.updated_on)),
    )
    return location, block


def _index_on(block: Block, on: date) -> int | None:
    """The 0-based position of the version in force on ``on``, or ``None`` when there is none."""
    version = version_on(block, on)
    if version is None:
        return None
    # Identity, not equality: two versions can have identical content.
    return next(i for i, candidate in enumerate(block.versions) if candidate is version)


def _info(block: Block, position: int) -> VersionInfo:
    """Describe the version at ``position`` (0-based) of ``block``."""
    version = block.versions[position]
    return VersionInfo(
        index=position + 1,
        starts_on=version.starts_on,
        effective_from=version.effective_from,
        published_on=version.published_on,
        introduced_by=version.source_law_id,
    )


def _page(text: str, offset: int, max_chars: int) -> tuple[str, int | None]:
    """Cut ``text[offset:]`` to at most ``max_chars``; return the part and the next offset.

    The cut goes after the last paragraph separator inside the window when there is one, else at
    the limit. The separator stays at the end of the part and the next offset is exactly where
    the part ends, so the parts concatenate to the original text with no gap or overlap.
    """
    end = offset + max_chars
    if end >= len(text):
        return text[offset:], None
    cut = text.rfind(_PARAGRAPH_BREAK, offset, end)
    if cut > offset:
        end = cut + len(_PARAGRAPH_BREAK)
    return text[offset:end], end


async def read_article(
    client: BoeClient,
    law_id: str,
    article: str,
    *,
    as_of: date | None = None,
    max_chars: int = DEFAULT_PAGE_CHARS,
    offset: int = 0,
    today: date | None = None,
    now: datetime | None = None,
) -> ReadArticleResult:
    """Return the text of one article as in force on ``as_of`` (default: today in Madrid).

    The body text is returned from character ``offset``, at most ``max_chars`` of it; when it is
    cut, ``next_offset`` says where to continue. Raises ``ValueError`` for a malformed law id, a
    blank ``article``, ``max_chars`` outside its range or an ``offset`` beyond the text.
    """
    _check_range("max_chars", max_chars, MIN_PAGE_CHARS, MAX_PAGE_CHARS)
    if offset < 0:
        raise ValueError("offset must not be negative")
    moment = utc_now(now)
    on = as_of if as_of is not None else madrid_today(moment, today)
    location, block = await _load(client, law_id, article, moment)
    if block is None:
        return ReadArticleResult(**location.model_dump(), as_of=on)
    state = state_on(block, on)
    position = _index_on(block, on)
    notes: list[str] = []
    if position is None and state == "not_yet_in_force":
        # Published but not yet effective: the first version is the upcoming one (every version
        # starts after ``on``), which is what a reader asking about this date wants to see.
        position = 0
        notes.append(
            f"On {on.isoformat()} this text was published but not yet in force; it takes effect "
            f"on {block.versions[0].starts_on}."
        )
    elif position is None:
        notes.append(f"The article did not exist on {on.isoformat()}: no version was in force.")
    full = block.versions[position].text if position is not None else ""
    if offset > len(full):
        raise ValueError(f"offset is beyond the end of the text ({len(full)} characters)")
    shown, next_offset = _page(full, offset, max_chars)
    if next_offset is not None:
        notes.append(
            f"Text cut after {next_offset} of {len(full)} characters: call again with "
            f"offset={next_offset} and the same other arguments for the next part."
        )
    last = len(block.versions) - 1
    return ReadArticleResult(
        **location.model_dump(),
        as_of=on,
        state=state,
        version=_info(block, position) if position is not None else None,
        version_count=len(block.versions),
        is_latest=position == last if position is not None else None,
        superseded_on=(
            block.versions[position + 1].starts_on
            if position is not None and position < last
            else None
        ),
        text=shown,
        notes=list(block.versions[position].notes) if position is not None and offset == 0 else [],
        total_chars=len(full),
        truncated=next_offset is not None,
        next_offset=next_offset,
        note=" ".join(notes) or None,
    )


async def compare_article_versions(
    client: BoeClient,
    law_id: str,
    article: str,
    from_date: date,
    to_date: date | None = None,
    max_changes: int = DEFAULT_CHANGES,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> CompareArticleVersionsResult:
    """Compare the article as in force on ``from_date`` and on ``to_date`` (default: today).

    Raises ``ValueError`` when ``from_date`` is after ``to_date`` or ``max_changes`` is out of
    range, besides the errors of :func:`read_article`.
    """
    _check_range("max_changes", max_changes, 1, MAX_CHANGES)
    moment = utc_now(now)
    end = to_date if to_date is not None else madrid_today(moment, today)
    if from_date > end:
        raise ValueError("from_date must not be after to_date (which is today when omitted)")
    location, block = await _load(client, law_id, article, moment)
    if block is None:
        return CompareArticleVersionsResult(
            **location.model_dump(), from_date=from_date, to_date=end
        )
    start, stop = _index_on(block, from_date), _index_on(block, end)
    if stop is None:
        # Versions are ordered by start date, so nothing in force on the later date means
        # nothing on the earlier one either.
        return CompareArticleVersionsResult(
            **location.model_dump(),
            from_date=from_date,
            to_date=end,
            note=f"The article did not exist on {end.isoformat()} (no version was in force), "
            "so there is nothing to compare.",
        )
    old = block.versions[start] if start is not None else _NOTHING
    diff = diff_versions(old, block.versions[stop])
    # The versions that took effect after the old one, up to and including the new one.
    first = 0 if start is None else start + 1
    amending = list(dict.fromkeys(v.source_law_id for v in block.versions[first : stop + 1]))
    changes = [
        ArticleChange(op=c.op, old_text=c.old_text, new_text=c.new_text, inline=c.inline)
        for c in diff.changes
        if c.op != "unchanged"
    ]
    note = None
    if start is None:
        note = (
            f"The article did not exist on {from_date.isoformat()} (no version was in force); it "
            "is compared with an empty text, so everything shows as added."
        )
    elif start == stop:
        note = "Both dates fall in the same version, so the text is identical."
    return CompareArticleVersionsResult(
        **location.model_dump(),
        from_date=from_date,
        to_date=end,
        from_version=_info(block, start) if start is not None else None,
        to_version=_info(block, stop),
        identical=diff.identical,
        added=diff.added,
        removed=diff.removed,
        changed=diff.changed,
        unchanged=diff.unchanged,
        changes=changes[:max_changes],
        changes_truncated=len(changes) > max_changes,
        intermediate_versions=max(0, stop - first),
        amending_laws=amending,
        unified_diff=diff.unified[:_MAX_UNIFIED_CHARS],
        unified_truncated=len(diff.unified) > _MAX_UNIFIED_CHARS,
        note=note,
    )


async def get_article_history(
    client: BoeClient,
    law_id: str,
    article: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> ArticleHistoryResult:
    """List every version of the article: when it took effect, which norm did it, what changed.

    At most the 60 most recent versions are returned (``truncated`` says when older ones were
    left out). Raises the errors of :func:`read_article` that apply.
    """
    moment = utc_now(now)
    location, block = await _load(client, law_id, article, moment)
    if block is None:
        return ArticleHistoryResult(**location.model_dump())
    events = history(block)
    return ArticleHistoryResult(
        **location.model_dump(),
        version_count=len(events),
        events=[
            ArticleHistoryEvent(
                index=e.index,
                kind=e.kind,
                effective_from=e.effective_from,
                published_on=e.published_on,
                introduced_by=e.source_law_id,
                added=e.added,
                removed=e.removed,
                changed=e.changed,
            )
            for e in events[-_MAX_HISTORY_EVENTS:]
        ],
        truncated=len(events) > _MAX_HISTORY_EVENTS,
        current_state=state_on(block, madrid_today(moment, today)),
    )
