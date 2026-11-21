"""Use case of ``explore_amendment_chain``: follow amendments between laws, a few levels deep.

``chains`` holds the pure walk over ``LawAnalysis`` records and never fetches anything. This
module is the I/O around it: it reads the analyses the walk asks for (``Chain.to_fetch``), within
a request budget and a time budget, and presents the result as ``schemas``. The catalogue snapshot
(``catalogue``) tells which referenced ids belong to the consolidated collection and gives the
titles. Like ``trends.most_amended_laws``, a call examines a limited number of laws and can be
repeated to continue, because everything fetched is cached by ``client``.

Layering: ``server`` calls ``explore_amendment_chain`` -> ``chains`` (walk) and ``client`` (one
analysis per request) and ``catalogue`` (ids and titles) -> ``schemas``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Literal

from boe_insights_mcp import chains
from boe_insights_mcp.catalogue import CatalogueStore, ProgressCallback
from boe_insights_mcp.chains import AMENDING_KINDS, Chain, Direction, RelationKind
from boe_insights_mcp.clock import utc_now
from boe_insights_mcp.errors import BoeError
from boe_insights_mcp.identifiers import normalise_identifier
from boe_insights_mcp.models import LawAnalysis, LawSummary
from boe_insights_mcp.schemas import (
    AmendmentChainResult,
    ChainEdgeInfo,
    ChainNodeInfo,
    Provenance,
)
from boe_insights_mcp.trends import AnalysisClient, MonotonicProgress

__all__ = [
    "CHAIN_TIME_LIMIT",
    "DEFAULT_DEPTH",
    "DEFAULT_MAX_LAWS",
    "MAX_EDGES",
    "MAX_LAWS",
    "MAX_NODES",
    "Include",
    "explore_amendment_chain",
]

Include = Literal["amendments", "all"]

DEFAULT_DEPTH = 2
DEFAULT_MAX_LAWS = 25
# The most fresh analyses (one request each) one call may read.
MAX_LAWS = 50
# The chain stops growing at this many laws; the result lists them all.
MAX_NODES = 150
# A chain of 150 laws held up to about 400 edges in the recorded analyses; the cap keeps the
# result readable and ``edges_total`` says how many were found.
MAX_EDGES = 400
_MAX_DETAIL = 200
# Same budget as ``most_amended_laws``: no fresh request starts in the last 5 seconds of 40.
CHAIN_TIME_LIMIT = 40.0
_TIME_MARGIN = 5.0

# "all" adds everything the BOE relates besides amendments: corrections, citations and court
# proceedings. The kinds are those of ``chains.RelationKind``.
_ALL_KINDS: frozenset[RelationKind] = frozenset(
    {"modifies", "repeals", "adds", "suspends", "corrects", "cites", "judicial", "other"}
)


def _shorten(text: str) -> str:
    """Cut a reference text to ``_MAX_DETAIL`` characters, the last one being "…" when cut."""
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"


def _incomplete_note(examined: int, pending: int) -> str:
    """What to tell the caller when laws are still to be read."""
    return (
        f"Examined {examined} laws, {pending} still to read. "
        "Call again with the same arguments to continue."
    )


def _node_info(node: chains.ChainNode, laws: Mapping[str, LawSummary]) -> ChainNodeInfo:
    """The tool-facing record of a node, with the collection's title, rank and date when known."""
    law = laws.get(node.law_id)
    return ChainNodeInfo(
        law_id=node.law_id,
        depth=node.depth,
        title=law.title if law is not None else None,
        rank=law.rank.text if law is not None and law.rank is not None else None,
        published_on=law.published_on if law is not None else None,
        in_collection=law is not None,
        expanded=node.expanded,
        reason=node.reason or None,
    )


async def explore_amendment_chain(
    store: CatalogueStore,
    client: AnalysisClient,
    law_id: str,
    *,
    direction: Direction = "amended_by",
    depth: int = DEFAULT_DEPTH,
    include: Include = "amendments",
    max_laws: int = DEFAULT_MAX_LAWS,
    progress: ProgressCallback | None = None,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit: float = CHAIN_TIME_LIMIT,
) -> AmendmentChainResult:
    """Follow the amendments of ``law_id`` up to ``depth`` levels, reading a bounded number of laws.

    At most ``max_laws`` analyses that are not cached are read per call, and no fresh request
    starts once ``time_limit`` seconds minus a 5 s margin have passed since the call began;
    ``complete`` is false then, and calling again with the same arguments continues from the cache.
    Raises ``ValueError`` for a malformed identifier or an argument out of range, and the client's
    error when the starting law's own analysis cannot be read (``BoeNotFoundError`` for an
    unknown law).
    """
    # The clock starts before the catalogue download for the reason given in ``most_amended_laws``:
    # the caller gives up on the whole call, not on the loop.
    deadline = clock() + time_limit - _TIME_MARGIN
    root = normalise_identifier(law_id)
    if root is None:
        raise ValueError(f"invalid law identifier: {law_id!r}")
    if not chains.MIN_DEPTH <= depth <= chains.MAX_DEPTH:
        raise ValueError(f"depth must be between {chains.MIN_DEPTH} and {chains.MAX_DEPTH}")
    if not 1 <= max_laws <= MAX_LAWS:
        raise ValueError(f"max_laws must be between 1 and {MAX_LAWS}")
    if direction not in ("amended_by", "amends"):
        raise ValueError("direction must be 'amended_by' or 'amends'")
    if include not in ("amendments", "all"):
        raise ValueError("include must be 'amendments' or 'all'")
    kinds = AMENDING_KINDS if include == "amendments" else _ALL_KINDS
    moment = utc_now(now)

    reporter = MonotonicProgress(progress) if progress is not None else None
    catalogue = await store.get(progress=reporter)
    laws = {law.identifier: law for law in catalogue.laws}

    # What is known so far. A law maps to its analysis, or to ``None`` when its analysis is
    # permanently unavailable; a law that was not read yet, or failed temporarily, is absent, so
    # ``explore`` keeps asking for it.
    known: dict[str, LawAnalysis | None] = {}
    # Ids that failed temporarily in this call: not asked again in the same call, because a
    # request that just failed would most likely fail again and burn the budget.
    failed_ids: set[str] = set()
    examined = fresh = 0
    stopped = False

    while True:
        chain = _walk(root, known, direction, depth, kinds, laws)
        # A temporarily failed id stays in ``to_fetch`` (it is still pending), but is skipped here.
        todo = [law for law in chain.to_fetch if law not in failed_ids]
        if stopped or not todo:
            break
        for target in todo:
            cached = await client.has_cached_analysis(target)
            # A cached analysis costs nothing, so it never counts against the budget; the first
            # uncached one beyond the budget ends the round and the rest waits for the next call.
            if not cached and (fresh >= max_laws or clock() >= deadline):
                stopped = True
                break
            try:
                known[target] = await client.get_law_analysis(target)
            except BoeError as error:
                if target == root:
                    # Without the starting law's own analysis there is nothing to show: an
                    # unknown law is an error, as in every other tool.
                    raise
                if error.retryable:
                    failed_ids.add(target)
                else:
                    # A permanent failure (404 for ids outside the collection that the snapshot
                    # does not know yet, or an unreadable body) ends the chain at this node.
                    known[target] = None
            else:
                examined += 1
            if not cached:
                fresh += 1
            if reporter is not None:
                await reporter(
                    examined + len(failed_ids),
                    None,
                    f"Read {examined} laws ({len(chain.to_fetch)} needed so far)",
                )

    pending = len(chain.to_fetch)
    # A temporarily failed id is in ``to_fetch``, so ``failed`` never exceeds ``pending``.
    failed = len(failed_ids)
    complete = pending == 0 and failed == 0
    notes: list[str] = []
    if not complete:
        notes.append(_incomplete_note(examined, pending))
    if chain.truncated:
        notes.append(f"Stopped at {MAX_NODES} laws; use a smaller depth or include='amendments'.")
    return AmendmentChainResult(
        law_id=root,
        direction=direction,
        depth=depth,
        nodes=[_node_info(node, laws) for node in chain.nodes],
        edges=[
            ChainEdgeInfo(
                amending=edge.amending,
                amended=edge.amended,
                relation=edge.relation,
                kind=edge.kind,
                detail=_shorten(edge.detail),
            )
            for edge in chain.edges[:MAX_EDGES]
        ],
        edges_total=len(chain.edges),
        levels=list(chains.level_counts(chain)),
        examined=examined,
        pending=pending,
        failed=failed,
        complete=complete,
        truncated=chain.truncated,
        note=" ".join(notes) or None,
        provenance=Provenance(generated_at=moment, data_updated_at=catalogue.newest_update),
    )


def _walk(
    root: str,
    known: Mapping[str, LawAnalysis | None],
    direction: Direction,
    depth: int,
    kinds: frozenset[RelationKind],
    laws: Mapping[str, LawSummary],
) -> Chain:
    """Run the pure walk over what is known, treating only collection laws as fetchable."""
    # Ids outside the consolidated collection are never requested: their analysis is a 404
    # (api-notes, fourth probe, "Nodes outside the catalogue"), so a request would be wasted.
    return chains.explore(
        root,
        known,
        direction=direction,
        max_depth=depth,
        kinds=kinds,
        fetchable=lambda law_id: law_id in laws,
        max_nodes=MAX_NODES,
    )
