"""Whether, and since when, a whole law is repealed: one answer shared by the use cases.

Two BOE signals say that a law is repealed as a whole, and neither is complete alone (api-notes,
"Initial note"): the ``/metadatos`` flag and date, and the "Norma derogada" initial note (block
``no``). A repealed law leaves no marker in its articles, so the article tools in ``articles``
must ask this module, and ``legislation.get_law_overview`` uses the same rules for its status.

The module has a pure half (:func:`whole_law_repeal`, :func:`assess_repeal`), built on
``versions`` (which reads the note), and one small I/O function (:func:`fetch_repeal_inputs`)
that gets the two signals from the ``client`` concurrently and degrades instead of failing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

from boe_insights_mcp.client import BoeClient
from boe_insights_mcp.errors import BoeError, BoeNotFoundError
from boe_insights_mcp.models import Block, BlockVersion, LawMetadata
from boe_insights_mcp.versions import repeal_date, repeal_note

__all__ = [
    "LawRepeal",
    "RepealInputs",
    "WholeLawRepeal",
    "assess_repeal",
    "fetch_repeal_inputs",
    "whole_law_repeal",
]


@dataclass(frozen=True)
class WholeLawRepeal:
    """What the signals say about a law's repeal as a whole, as of a day."""

    signalled: bool
    """The BOE flag or the initial note says the whole law is repealed."""
    on: date | None
    """The repeal date: the metadata's, else the note's; ``None`` when none is known."""
    announced: bool
    """Signalled, but dated after the day asked about: not in effect yet."""

    @property
    def in_effect(self) -> bool:
        """Whether the whole law is repealed on that day (signalled and not announced)."""
        return self.signalled and not self.announced


def whole_law_repeal(
    *,
    flagged: bool,
    flag_on: date | None,
    note_version: BlockVersion | None,
    note_on: date | None,
    today: date,
) -> WholeLawRepeal:
    """Combine the metadata flag (and its date) with the repeal note into one verdict.

    ``note_version`` is the initial-note version that announces a repeal (``None`` for no such
    note) and ``note_on`` the date it gives. Either signal is enough (api-notes, "Initial
    note"). When both give a date the earlier one is used: for Ley 30/1992 the note says the
    repeal took effect on 2016-10-02 while the metadata date, 2021-04-02, is when the last
    transitional provisions ended, and the earlier date is the one that matters for almost every
    article. A repeal dated after ``today`` is announced, not in effect.
    """
    signalled = flagged or note_version is not None
    dates = [d for d in (flag_on, note_on if note_version is not None else None) if d is not None]
    on = min(dates, default=None)
    return WholeLawRepeal(
        signalled=signalled,
        on=on,
        announced=signalled and on is not None and on > today,
    )


@dataclass(frozen=True)
class RepealInputs:
    """The raw signals fetched for a law; either may be missing after a failed request."""

    metadata: LawMetadata | None
    """``None`` when the metadata could not be fetched."""
    note: Block | None
    """The initial note; ``None`` when absent, not asked for, or not fetched."""
    note_checked: bool
    """False only when the note was asked for and could not be fetched."""


@dataclass(frozen=True)
class LawRepeal:
    """The outcome of checking a law for a whole-law repeal, for the article tools."""

    repealed: bool | None
    """True: repealed and in effect. False: not repealed (or only announced). ``None``: a signal
    that could have said so could not be checked."""
    on: date | None
    """The repeal date when known, in the future for an announced repeal."""
    announced: bool
    """A repeal is announced for a date after today."""
    unchecked: tuple[str, ...]
    """What could not be fetched: ``"metadata"`` and/or ``"initial note"``."""


async def fetch_repeal_inputs(client: BoeClient, law_id: str, *, with_note: bool) -> RepealInputs:
    """Fetch the metadata, and the initial note when ``with_note``, concurrently.

    A failure of either request (anything but an unknown law) is not raised: the missing input
    comes back as ``None``/unchecked, so the caller can degrade. ``BoeNotFoundError`` from the
    metadata means the law does not exist and propagates, as in every other tool.
    """

    async def metadata() -> LawMetadata | None:
        """The metadata; ``None`` after a BOE failure other than "not found"."""
        try:
            return await client.get_law_metadata(law_id)
        except BoeNotFoundError:
            raise
        except BoeError:
            return None

    async def note() -> tuple[Block | None, bool]:
        """The initial note and whether it could be checked (a 404 is "no note", checked)."""
        if not with_note:
            return None, True
        try:
            return await client.get_initial_note(law_id), True
        except BoeError:
            return None, False

    # A TaskGroup, like ``legislation._fetch_law``: when the law is unknown the other request is
    # cancelled instead of left running, and the failure is re-raised as itself, not as the
    # ``ExceptionGroup`` the group wraps it in (callers catch ``BoeError`` by type).
    try:
        async with asyncio.TaskGroup() as group:
            metadata_task = group.create_task(metadata())
            note_task = group.create_task(note())
    except ExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return RepealInputs(metadata_task.result(), *note_task.result())


def assess_repeal(inputs: RepealInputs, *, today: date) -> LawRepeal:
    """Decide from the fetched signals whether the whole law is repealed, and since when."""
    metadata = inputs.metadata
    # The note's date comes from its earliest repeal version (``repeal_date``), while the version
    # kept for quoting is the latest: the repeal took effect when the first note said so.
    verdict = whole_law_repeal(
        flagged=metadata.repealed if metadata is not None else False,
        flag_on=metadata.repealed_on if metadata is not None else None,
        note_version=repeal_note(inputs.note),
        note_on=repeal_date(inputs.note),
        today=today,
    )
    unchecked = tuple(
        name
        for name, missing in (
            ("metadata", metadata is None),
            ("initial note", not inputs.note_checked),
        )
        if missing
    )
    if verdict.signalled:
        repealed: bool | None = not verdict.announced
    else:
        # Nothing says repealed: that is only "not repealed" when every signal was checked.
        repealed = None if unchecked else False
    return LawRepeal(repealed, verdict.on, verdict.announced, unchecked)
