"""Use cases over the text of a law: read an article, compare versions, history, outline.

Like ``legislation``, these functions take a :class:`BoeClient` and plain arguments, return
pydantic models from ``schemas`` and know nothing of MCP. Each one follows the same flow: fetch
the law's block index, let ``locator`` find the block the person means (by wording, or by exact
block id), then fetch that block (all its versions) together with what says whether the whole law
is repealed (``repeal``), and answer with ``versions`` (version selection, diff, history).

Public use cases: ``read_article``, ``compare_article_versions``, ``get_article_history`` and
``get_law_structure``. The last one works on the whole law at once: it fetches the full text in one
request, builds the table of contents with ``structure`` and pages it.
A wording that matches no block or several blocks is a normal result with candidates, not an
error; an unknown law raises ``BoeNotFoundError`` and bad arguments raise ``ValueError``. The CPU
heavy parts (diff, history) run in a worker thread so they never block the event loop.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.clock import madrid_today, utc_now
from boe_insights_mcp.errors import BoeNotFoundError
from boe_insights_mcp.identifiers import normalise_identifier
from boe_insights_mcp.locator import LocateResult, locate, locate_block_id
from boe_insights_mcp.models import Block, BlockVersion, IndexEntry
from boe_insights_mcp.repeal import (
    LawRepeal,
    RepealInputs,
    assess_repeal,
    fetch_repeal_inputs,
)
from boe_insights_mcp.schemas import (
    ArticleCandidate,
    ArticleChange,
    ArticleHistoryEvent,
    ArticleHistoryResult,
    ArticleLocation,
    CompareArticleVersionsResult,
    LawStructureResult,
    Provenance,
    ReadArticleResult,
    StructureItem,
    VersionInfo,
)
from boe_insights_mcp.structure import StructureEntry, build_structure
from boe_insights_mcp.versions import (
    ArticleState,
    diff_versions,
    history,
    state_on,
    version_on,
)

__all__ = [
    "DEFAULT_CHANGES",
    "DEFAULT_COMPARE_CHARS",
    "DEFAULT_PAGE_CHARS",
    "DEFAULT_STRUCTURE_LIMIT",
    "MAX_ARTICLE_CHARS",
    "MAX_CHANGES",
    "MAX_COMPARE_CHARS",
    "MAX_PAGE_CHARS",
    "MAX_STRUCTURE_LIMIT",
    "MIN_COMPARE_CHARS",
    "MIN_PAGE_CHARS",
    "compare_article_versions",
    "get_article_history",
    "get_law_structure",
    "read_article",
]

DEFAULT_PAGE_CHARS = 6000
MIN_PAGE_CHARS = 500
MAX_PAGE_CHARS = 20000
# Entries per page of a table of contents. The Código Civil has about 2 400 blocks, so a page is
# bounded by ``limit`` however large the law is.
DEFAULT_STRUCTURE_LIMIT = 200
MAX_STRUCTURE_LIMIT = 400
DEFAULT_CHANGES = 40
MAX_CHANGES = 200
# Total characters of old_text, new_text and inline over all returned changes.
DEFAULT_COMPARE_CHARS = 8000
MIN_COMPARE_CHARS = 1000
MAX_COMPARE_CHARS = 30000
# Longest ``article`` wording accepted: a designation is a few words, and the locator's cost
# grows with the query (12 to 20 KB strings took 10 to 19 s on a 2,500-block index).
MAX_ARTICLE_CHARS = 200
_MAX_UNIFIED_CHARS = 6000
_MAX_HISTORY_EVENTS = 60
_MAX_NOTES_CHARS = 2000
_TRUNCATED = "…[truncated]"
# Smallest slice of the budget worth spending on a change that does not fit whole.
_MIN_CUT_CHARS = 100
# The id of the block that holds a law's initial note (api-notes, "Initial note").
_NOTE_BLOCK_ID = "no"

# The two kinds a "headings only" outline leaves out.
_PRECEPT_KINDS = frozenset({"article", "provision"})

_PARAGRAPH_BREAK = "\n\n"
# Compared against when an article did not exist yet: every paragraph then shows as added.
_NOTHING = BlockVersion(source_law_id="", published_on=None, effective_from=None, paragraphs=())

_AMBIGUOUS_HINT = (
    "Several blocks have this designation (a Real Decreto and the Reglamento it approves can "
    "both have an 'Artículo 1'). Repeat the call with block_id set to the block_id of one of "
    "the candidates, and without article."
)
_NOT_FOUND_HINT = (
    "No block of this law matches that wording. The candidates are the closest titles: retry "
    "with block_id set to the block_id of one of them, or with another wording."
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


@dataclass(frozen=True)
class _Loaded:
    """What the three tools share after locating and fetching: location, block and repeal."""

    location: ArticleLocation
    block: Block | None
    """``None`` unless exactly one block matched."""
    repeal: LawRepeal | None
    """``None`` when no block was fetched (nothing to judge)."""
    notes: tuple[str, ...]
    """Sentences for the result's ``note`` that arose while locating."""


def _check_selector(article: str | None, block_id: str | None) -> None:
    """Raise ``ValueError`` unless exactly one of ``article`` and ``block_id`` is usable."""
    if (article is None) == (block_id is None):
        raise ValueError("give exactly one of article and block_id")
    if article is not None:
        if not article.strip():
            raise ValueError("article must not be empty")
        if len(article) > MAX_ARTICLE_CHARS:
            raise ValueError(
                f"article must be at most {MAX_ARTICLE_CHARS} characters: pass only the article "
                "or provision designation, e.g. '46 bis'"
            )
    elif not (block_id or "").strip():
        raise ValueError("block_id must not be empty")


def _newest_update(index: list[IndexEntry]) -> datetime | None:
    """The newest ``updated_on`` of the index as a UTC datetime; ``None`` when none is dated."""
    return _midnight_utc(max((e.updated_on for e in index if e.updated_on), default=None))


async def _fetch_block_and_repeal(
    client: BoeClient, law_id: str, entry: IndexEntry, with_note: bool, today: date
) -> tuple[Block, LawRepeal]:
    """Fetch the block and, concurrently, what says whether the whole law is repealed.

    The extra requests never fail the tool (see ``repeal.fetch_repeal_inputs``); only a block
    failure, or an unknown law, propagates.
    """
    # The index date is the block's revision: it makes the cached block long-lived (api-notes 6).
    try:
        async with asyncio.TaskGroup() as group:
            block_task = group.create_task(
                client.get_text_block(law_id, entry.block_id, updated_on=entry.updated_on)
            )
            repeal_task = group.create_task(
                fetch_repeal_inputs(client, law_id, with_note=with_note)
            )
    except ExceptionGroup as failures:
        # Callers and ``server._guard`` catch ``BoeError`` by type, not the group around it.
        raise failures.exceptions[0] from None
    return block_task.result(), assess_repeal(repeal_task.result(), today=today)


async def _load(
    client: BoeClient,
    law_id: str,
    article: str | None,
    block_id: str | None,
    moment: datetime,
    today: date,
) -> _Loaded:
    """Locate the article (or block id) in the law's index and fetch its block.

    Returns the location (with candidates and a hint when nothing was found), the block, which
    is ``None`` unless exactly one block matched, and the whole-law repeal check. Raises
    ``ValueError`` for a malformed law id or a bad ``article``/``block_id`` (before any request)
    and ``BoeNotFoundError`` for an unknown law.
    """
    # A malformed id is passed through unchanged so the client's own check raises the ValueError.
    law_id = normalise_identifier(law_id) or law_id
    _check_selector(article, block_id)
    index = await client.get_text_index(law_id)
    located = (
        locate_block_id(index, block_id) if block_id is not None else locate(index, article or "")
    )
    entry = located.entry
    if entry is None:
        return _Loaded(
            ArticleLocation(
                law_id=law_id,
                outcome=located.outcome,
                candidates=_candidates(located),
                hint=_AMBIGUOUS_HINT if located.outcome == "ambiguous" else _NOT_FOUND_HINT,
                # No block was read, so the newest date of the index is the freshest data seen.
                provenance=Provenance(generated_at=moment, data_updated_at=_newest_update(index)),
            ),
            None,
            None,
            (),
        )
    # The initial note costs a request, so it is fetched only for laws whose index lists it
    # (5 of the 13 indexes checked, api-notes "Initial note"); the metadata is always cached 6 h.
    with_note = any(e.block_id == _NOTE_BLOCK_ID for e in index)
    block, repeal = await _fetch_block_and_repeal(client, law_id, entry, with_note, today)
    location = ArticleLocation(
        law_id=law_id,
        outcome="found",
        block_id=entry.block_id,
        block_title=entry.title,
        provenance=Provenance(generated_at=moment, data_updated_at=_midnight_utc(entry.updated_on)),
    )
    notes = ()
    if located.ignored:
        notes = (
            f"Located {entry.title or entry.block_id}; the rest of the request "
            f"('{located.ignored}') was ignored: this tool returns whole articles.",
        )
    return _Loaded(location, block, repeal, notes)


def _law_state(
    block_state: ArticleState, repeal: LawRepeal | None, on: date, *, as_of_given: bool
) -> ArticleState:
    """The state on ``on``, overridden to ``repealed`` when the whole law was repealed by then.

    A repealed law leaves no marker in its articles, so the block alone says ``in_force``. With
    no known repeal date, today's answer is ``repealed``, but an explicit ``as_of`` is left to
    the block: it may well predate the repeal.
    """
    if repeal is not None and repeal.repealed:
        if repeal.on is None:
            return block_state if as_of_given else "repealed"
        if on >= repeal.on:
            return "repealed"
    return block_state


def _repeal_note(repeal: LawRepeal | None, on: date, state: ArticleState) -> str | None:
    """A sentence about the whole law's repeal for a result about ``on``, or ``None``."""
    if repeal is None:
        return None
    if repeal.repealed is None:
        missing = " and ".join(repeal.unchecked)
        return (
            f"The {missing} could not be fetched, so a repeal of the whole law would not show here."
        )
    since = f" on {repeal.on.isoformat()}" if repeal.on is not None else " (the BOE gives no date)"
    if repeal.announced:
        return f"The whole law is announced to be repealed{since}; it is not repealed yet."
    if not repeal.repealed:
        return None
    if state == "repealed":
        return (
            f"The whole law was repealed{since}, so this article is no longer in force; the text "
            "shown is its last consolidated version."
        )
    return f"The whole law was repealed later{since}: this text was in force on {on.isoformat()}."


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


def _cap_notes(notes: list[str]) -> tuple[list[str], bool]:
    """Keep footnotes up to ``_MAX_NOTES_CHARS`` characters in all; say whether any was cut."""
    kept: list[str] = []
    used = 0
    for note in notes:
        if used + len(note) <= _MAX_NOTES_CHARS:
            kept.append(note)
            used += len(note)
            continue
        room = _MAX_NOTES_CHARS - used
        if room > len(_TRUNCATED):
            kept.append(note[: room - len(_TRUNCATED)] + _TRUNCATED)
        return kept, True
    return kept, False


async def read_article(
    client: BoeClient,
    law_id: str,
    article: str | None = None,
    *,
    block_id: str | None = None,
    as_of: date | None = None,
    max_chars: int = DEFAULT_PAGE_CHARS,
    offset: int = 0,
    today: date | None = None,
    now: datetime | None = None,
) -> ReadArticleResult:
    """Return the text of one article as in force on ``as_of`` (default: today in Madrid).

    Give exactly one of ``article`` (a designation such as "46 bis") and ``block_id`` (an exact
    block id from a candidates list). The body text is returned from character ``offset``, at
    most ``max_chars`` of it; when it is cut, ``next_offset`` says where to continue. When the
    whole law is repealed the state is ``repealed`` although the article carries no marker.
    Raises ``ValueError`` for a malformed law id, a bad ``article``/``block_id``, ``max_chars``
    outside its range or an ``offset`` beyond the text.
    """
    _check_range("max_chars", max_chars, MIN_PAGE_CHARS, MAX_PAGE_CHARS)
    if offset < 0:
        raise ValueError("offset must not be negative")
    moment = utc_now(now)
    day = madrid_today(moment, today)
    on = as_of if as_of is not None else day
    loaded = await _load(client, law_id, article, block_id, moment, day)
    location, block, repeal = loaded.location, loaded.block, loaded.repeal
    if block is None:
        return ReadArticleResult(**location.model_dump(), as_of=on)
    state = _law_state(state_on(block, on), repeal, on, as_of_given=as_of is not None)
    position = _index_on(block, on)
    notes: list[str] = list(loaded.notes)
    if position is None and state_on(block, on) == "not_yet_in_force":
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
    if (repeal_sentence := _repeal_note(repeal, on, state)) is not None:
        notes.append(repeal_sentence)
    last = len(block.versions) - 1
    footnotes, notes_truncated = _cap_notes(
        list(block.versions[position].notes) if position is not None and offset == 0 else []
    )
    return ReadArticleResult(
        **location.model_dump(),
        as_of=on,
        state=state,
        law_repealed=repeal.repealed if repeal is not None else None,
        law_repealed_on=repeal.on if repeal is not None else None,
        version=_info(block, position) if position is not None else None,
        version_count=len(block.versions),
        is_latest=position == last if position is not None else None,
        superseded_on=(
            block.versions[position + 1].starts_on
            if position is not None and position < last
            else None
        ),
        text=shown,
        notes=footnotes,
        notes_truncated=notes_truncated,
        total_chars=len(full),
        truncated=next_offset is not None,
        next_offset=next_offset,
        note=" ".join(notes) or None,
    )


def _cut(text: str, room: int) -> str:
    """``text`` cut to at most ``room`` characters, ending in the truncation marker when cut."""
    if len(text) <= room:
        return text
    return text[: max(0, room - len(_TRUNCATED))] + _TRUNCATED


def _size(change: ArticleChange) -> int:
    """The characters a change spends of the budget: its three texts."""
    return sum(len(t) for t in (change.old_text, change.new_text, change.inline) if t)


def _fit(change: ArticleChange, room: int) -> ArticleChange:
    """The change with each of its texts cut so that together they take at most ``room``."""
    texts = [t for t in (change.old_text, change.new_text, change.inline) if t is not None]
    share = max(room // max(1, len(texts)), len(_TRUNCATED))
    return ArticleChange(
        op=change.op,
        old_text=_cut(change.old_text, share) if change.old_text is not None else None,
        new_text=_cut(change.new_text, share) if change.new_text is not None else None,
        inline=_cut(change.inline, share) if change.inline is not None else None,
    )


def _budgeted(
    changes: list[ArticleChange], max_changes: int, max_chars: int
) -> tuple[list[ArticleChange], bool]:
    """Keep changes in order within ``max_changes`` and ``max_chars``; say whether any was lost.

    The budget counts the characters of the texts. A change that does not fit in what is left is
    cut, with a marker, to the room that remains (when that is worth showing), rather than
    dropped, so even a single enormous paragraph shows its start; the list ends there.
    """
    kept: list[ArticleChange] = []
    room = max_chars
    for change in changes:
        if len(kept) == max_changes:
            return kept, True
        size = _size(change)
        if size <= room:
            kept.append(change)
            room -= size
            continue
        if room >= _MIN_CUT_CHARS or not kept:
            kept.append(_fit(change, room))
        return kept, True
    return kept, False


def _as_change(
    op: str, old_text: str | None, new_text: str | None, inline: str | None
) -> ArticleChange:
    """The tool-facing change: a reworded paragraph is given as ``inline`` alone when it exists."""
    # Showing both texts and the marked-up one would triple the size of every rewording; the
    # marked-up paragraph carries both. A multi-line paragraph (a table) has no ``inline``
    # (``versions._inline``), so it keeps both texts, which hold its line breaks.
    if op == "changed" and inline is not None:
        old_text = new_text = None
    return ArticleChange(op=op, old_text=old_text, new_text=new_text, inline=inline)  # type: ignore[arg-type]


async def compare_article_versions(
    client: BoeClient,
    law_id: str,
    article: str | None,
    from_date: date,
    to_date: date | None = None,
    max_changes: int = DEFAULT_CHANGES,
    max_chars: int = DEFAULT_COMPARE_CHARS,
    *,
    block_id: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> CompareArticleVersionsResult:
    """Compare the article as in force on ``from_date`` and on ``to_date`` (default: today).

    The listed changes hold at most ``max_changes`` paragraphs and ``max_chars`` characters of
    text in all; ``changes_truncated`` says when either limit cut the list. Raises
    ``ValueError`` when ``from_date`` is after ``to_date`` or ``max_changes``/``max_chars`` are
    out of range, besides the errors of :func:`read_article`.
    """
    _check_range("max_changes", max_changes, 1, MAX_CHANGES)
    _check_range("max_chars", max_chars, MIN_COMPARE_CHARS, MAX_COMPARE_CHARS)
    moment = utc_now(now)
    day = madrid_today(moment, today)
    end = to_date if to_date is not None else day
    if from_date > end:
        raise ValueError("from_date must not be after to_date (which is today when omitted)")
    loaded = await _load(client, law_id, article, block_id, moment, day)
    location, block, repeal = loaded.location, loaded.block, loaded.repeal
    if block is None:
        return CompareArticleVersionsResult(
            **location.model_dump(), from_date=from_date, to_date=end
        )
    repeal_fields = {
        "law_repealed": repeal.repealed if repeal is not None else None,
        "law_repealed_on": repeal.on if repeal is not None else None,
    }
    notes = list(loaded.notes)
    repeal_sentence = _compare_repeal_note(repeal, end)
    start, stop = _index_on(block, from_date), _index_on(block, end)
    if stop is None:
        # Versions are ordered by start date, so nothing in force on the later date means
        # nothing on the earlier one either.
        notes.append(f"The article {_absence(block, end)}, so there is nothing to compare.")
        return CompareArticleVersionsResult(
            **location.model_dump(),
            **repeal_fields,
            from_date=from_date,
            to_date=end,
            note=" ".join([*notes, *([repeal_sentence] if repeal_sentence else [])]),
        )
    old = block.versions[start] if start is not None else _NOTHING
    # The diff is CPU work (character and word comparisons): off the event loop, so that a large
    # article does not stall every other request of the server.
    diff = await asyncio.to_thread(diff_versions, old, block.versions[stop])
    # The versions that took effect after the old one, up to and including the new one.
    first = 0 if start is None else start + 1
    amending = list(dict.fromkeys(v.source_law_id for v in block.versions[first : stop + 1]))
    changes, truncated = _budgeted(
        [
            _as_change(c.op, c.old_text, c.new_text, c.inline)
            for c in diff.changes
            if c.op != "unchanged"
        ],
        max_changes,
        max_chars,
    )
    if start is None:
        notes.append(
            f"The article {_absence(block, from_date)}; it is compared with an empty text, so "
            "everything shows as added."
        )
    elif start == stop:
        notes.append("Both dates fall in the same version, so the text is identical.")
    if repeal_sentence:
        notes.append(repeal_sentence)
    return CompareArticleVersionsResult(
        **location.model_dump(),
        **repeal_fields,
        from_date=from_date,
        to_date=end,
        from_version=_info(block, start) if start is not None else None,
        to_version=_info(block, stop),
        identical=diff.identical,
        added=diff.added,
        removed=diff.removed,
        changed=diff.changed,
        unchanged=diff.unchanged,
        changes=changes,
        changes_truncated=truncated,
        intermediate_versions=max(0, stop - first),
        amending_laws=amending,
        unified_diff=diff.unified[:_MAX_UNIFIED_CHARS],
        unified_truncated=len(diff.unified) > _MAX_UNIFIED_CHARS,
        note=" ".join(notes) or None,
    )


def _absence(block: Block, on: date) -> str:
    """Why no version applies on ``on``: "was not yet in force" (published) or "did not exist"."""
    if state_on(block, on) == "not_yet_in_force":
        return (
            f"was not yet in force on {on.isoformat()} (it takes effect on "
            f"{block.versions[0].starts_on})"
        )
    return f"did not exist on {on.isoformat()} (no version was in force)"


def _compare_repeal_note(repeal: LawRepeal | None, end: date) -> str | None:
    """The repeal sentence for a comparison up to ``end``, only when it concerns that range."""
    if repeal is None:
        return None
    if repeal.repealed is None:
        return _repeal_note(repeal, end, "in_force")
    # A repeal after ``end`` is not part of the compared range.
    if repeal.on is not None and repeal.on > end and not repeal.announced:
        return None
    if repeal.repealed or repeal.announced:
        since = (
            f" on {repeal.on.isoformat()}" if repeal.on is not None else " (the BOE gives no date)"
        )
        verb = "was repealed" if repeal.repealed else "is announced to be repealed"
        return f"The whole law {verb}{since}; the article is no longer in force from then."
    return None


async def get_article_history(
    client: BoeClient,
    law_id: str,
    article: str | None = None,
    *,
    block_id: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> ArticleHistoryResult:
    """List every version of the article: when it took effect, which norm did it, what changed.

    At most the 60 most recent versions are returned (``truncated`` says when older ones were
    left out). ``current_state`` is ``repealed`` also when the whole law is repealed. Raises the
    errors of :func:`read_article` that apply.
    """
    moment = utc_now(now)
    day = madrid_today(moment, today)
    loaded = await _load(client, law_id, article, block_id, moment, day)
    location, block, repeal = loaded.location, loaded.block, loaded.repeal
    if block is None:
        return ArticleHistoryResult(**location.model_dump())
    # Only the events that are returned are built, and off the event loop: each one is a diff.
    events = await asyncio.to_thread(history, block, limit=_MAX_HISTORY_EVENTS)
    state = _law_state(state_on(block, day), repeal, day, as_of_given=False)
    notes = list(loaded.notes)
    if (sentence := _repeal_note(repeal, day, state)) is not None:
        notes.append(sentence)
    return ArticleHistoryResult(
        **location.model_dump(),
        law_repealed=repeal.repealed if repeal is not None else None,
        law_repealed_on=repeal.on if repeal is not None else None,
        version_count=len(block.versions),
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
            for e in events
        ],
        truncated=len(block.versions) > _MAX_HISTORY_EVENTS,
        current_state=state,
        note=" ".join(notes) or None,
    )


StructureDetail = Literal["headings", "all"]


async def _fetch_structure_inputs(
    client: BoeClient, law_id: str
) -> tuple[list[Block], RepealInputs]:
    """Fetch the whole text and the metadata concurrently; an unknown law raises.

    The metadata request never fails the tool apart from "not found" (see
    ``repeal.fetch_repeal_inputs``); the full text is what the answer is made of, so its failure
    propagates.
    """
    try:
        async with asyncio.TaskGroup() as group:
            text_task = group.create_task(client.get_full_text(law_id))
            repeal_task = group.create_task(fetch_repeal_inputs(client, law_id, with_note=False))
    except ExceptionGroup as failures:
        # Callers and ``server._guard`` catch ``BoeError`` and ``ValueError`` by type, not the
        # group around them. When both requests fail, "not found" is the more useful one (it says
        # the law does not exist); otherwise the arbitrary first is as good as any.
        errors = failures.exceptions
        raise next((e for e in errors if isinstance(e, BoeNotFoundError)), errors[0]) from None
    return text_task.result(), repeal_task.result()


def _structure_notes(
    entries: list[StructureEntry],
    repeal: LawRepeal,
    on: date,
    *,
    as_of_given: bool,
    effective_from: date | None,
) -> list[str]:
    """Sentences for the result's ``note``: empty, not yet in force, or repealed (whole law)."""
    notes: list[str] = []
    upcoming = sum(1 for e in entries if e.state == "not_yet_in_force")
    if not entries:
        notes.append(f"The law's text did not exist on {on.isoformat()}: there are no entries.")
    elif upcoming == len(entries):
        takes_effect = (
            f"; it takes effect on {effective_from.isoformat()}"
            if effective_from is not None and effective_from > on
            else ""
        )
        notes.append(
            f"On {on.isoformat()} this law was published but is not yet in force{takes_effect}. "
            "Every entry shows the upcoming text."
        )
    elif upcoming:
        notes.append(
            f"{upcoming} of {len(entries)} entries are published but not yet in force on "
            f"{on.isoformat()}."
        )
    # A repealed law's articles carry no repeal marker (api-notes, "Initial note"), so every
    # entry reads "in force" and only this sentence tells the reader. Putting ``repealed`` on each
    # entry instead would defeat the compact output.
    if (
        repeal.repealed
        and _law_state("in_force", repeal, on, as_of_given=as_of_given) == "repealed"
    ):
        since = f" on {repeal.on.isoformat()}" if repeal.on is not None else " (no date given)"
        notes.append(
            f"The whole law was repealed{since}, so none of these entries is in force; the "
            "table shows its last consolidated text."
        )
    elif (sentence := _repeal_note(repeal, on, "in_force")) is not None:
        notes.append(sentence)
    return notes


async def get_law_structure(
    client: BoeClient,
    law_id: str,
    *,
    as_of: date | None = None,
    detail: StructureDetail = "all",
    offset: int = 0,
    limit: int = DEFAULT_STRUCTURE_LIMIT,
    today: date | None = None,
    now: datetime | None = None,
) -> LawStructureResult:
    """Return a page of a law's table of contents as of ``as_of`` (default: today in Madrid).

    ``detail="headings"`` leaves out articles and provisions. The page holds at most ``limit``
    entries from ``offset`` on; ``counts`` always describes the whole law. Costs two requests:
    the full text and the metadata. Raises ``ValueError`` for a malformed law id, a bad
    ``detail``, ``limit`` or ``offset``, and ``BoeNotFoundError`` for an unknown law.
    """
    if detail not in ("headings", "all"):
        raise ValueError("detail must be 'headings' or 'all'")
    _check_range("limit", limit, 1, MAX_STRUCTURE_LIMIT)
    if offset < 0:
        raise ValueError("offset must not be negative")
    moment = utc_now(now)
    on = as_of if as_of is not None else madrid_today(moment, today)
    # A malformed id is passed through unchanged so the client's own check raises the ValueError.
    law_id = normalise_identifier(law_id) or law_id
    blocks, inputs = await _fetch_structure_inputs(client, law_id)
    # The initial note is block ``no`` of the text we already hold, so the repeal assessment needs
    # no request of its own (``articles._load`` has to ask for it because it only has an index).
    note_block = next((block for block in blocks if block.block_id == "no"), None)
    repeal = assess_repeal(
        RepealInputs(inputs.metadata, note_block, note_checked=True),
        today=madrid_today(moment, today),
    )

    entries = build_structure(blocks, on)
    shown = entries if detail == "all" else [e for e in entries if e.kind not in _PRECEPT_KINDS]
    if offset > len(shown):
        raise ValueError(f"offset is beyond the end of the structure ({len(shown)} entries)")
    page = shown[offset : offset + limit]
    end = offset + len(page)
    metadata = inputs.metadata
    return LawStructureResult(
        law_id=law_id,
        title=metadata.title if metadata is not None else law_id,
        as_of=on,
        entries=[
            StructureItem(
                block_id=e.block_id,
                kind=e.kind,
                level=e.level,
                label=e.label,
                heading=e.heading,
                # ``in_force`` is the norm and is left out; ``did_not_exist`` never reaches here
                # because ``build_structure`` skips such blocks.
                state=e.state if e.state in ("repealed", "not_yet_in_force") else None,  # type: ignore[arg-type]
            )
            for e in page
        ],
        returned=len(page),
        total_entries=len(shown),
        has_more=end < len(shown),
        next_offset=end if end < len(shown) else None,
        counts=dict(Counter(e.kind for e in entries)),
        note=" ".join(
            _structure_notes(
                entries,
                repeal,
                on,
                as_of_given=as_of is not None,
                effective_from=metadata.effective_from if metadata is not None else None,
            )
        )
        or None,
        provenance=Provenance(
            generated_at=moment,
            data_updated_at=metadata.updated_at if metadata is not None else None,
        ),
    )
