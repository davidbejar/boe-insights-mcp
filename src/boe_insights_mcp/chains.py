"""Amendment chains: who amended a law, and who amended those, over known ``LawAnalysis`` records.

This module is pure: no I/O, no clock and no network. It classifies BOE relation codes into a few
kinds and walks the cross-references of ``LawAnalysis`` records (``models``) breadth-first,
returning frozen dataclasses. It never fetches anything: the caller passes the analyses it has,
reads ``Chain.to_fetch`` to learn which ones are still needed, fetches them and calls ``explore``
again until nothing is left to fetch. The tool layer converts the result into its own schemas.

Everything is deterministic: discovery order follows the order of the references in each analysis.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from boe_insights_mcp.models import LawAnalysis, LawReference

__all__ = [
    "AMENDING_KINDS",
    "Chain",
    "ChainEdge",
    "ChainNode",
    "Direction",
    "NodeReason",
    "RelationKind",
    "explore",
    "level_counts",
    "relation_kind",
]

Direction = Literal["amended_by", "amends"]
RelationKind = Literal[
    "modifies", "repeals", "adds", "suspends", "corrects", "cites", "judicial", "other"
]
NodeReason = Literal["", "depth_limit", "not_fetched", "unavailable"]

AMENDING_KINDS: frozenset[RelationKind] = frozenset({"modifies", "repeals", "adds", "suspends"})
"""The kinds that change the text of the norm they act on."""

MIN_DEPTH = 1
MAX_DEPTH = 3

# Classification is by relation CODE, never by relation text: the two sides of one reference use
# different voices ("MODIFICA" in ``anteriores``, "SE MODIFICA" in ``posteriores``) but share the
# same codes, and a few texts have no passive form at all (docs/api-notes.md section 5 and the
# amendment-chains notes of the fourth probe).
_CODE_KINDS: dict[str, RelationKind] = {}


def _register(kind: RelationKind, codes: Collection[int]) -> None:
    """Record ``kind`` for every numeric code in ``codes`` (module set-up only)."""
    for code in codes:
        _CODE_KINDS[str(code)] = kind


# 247 (ENMIENDAS / SE PUBLICA Enmienda) amends an international treaty, so it counts as a
# modification even though no ordinary law is involved.
_register("modifies", [270, 271, 272, 220, 221, 245, 247])
_register("repeals", [*range(210, 218), 230, 235])
_register("adds", [407, 408])
_register("suspends", [231])
_register("corrects", range(201, 205))
_register("judicial", [520, 530, 540, 552, 693, 694])
# 407 and 408 are registered above as "adds"; they must stay out of the 400 to 406 citation range.
_register("cites", [300, 301, 302, 303, 330, 331, *range(400, 407), *range(420, 428)])
_register("cites", [430, 440, 470, 480, 490])


def relation_kind(code: str) -> RelationKind:
    """Classify a BOE relation code; unknown or malformed codes are ``"other"``."""
    return _CODE_KINDS.get(code, "other")


@dataclass(frozen=True)
class ChainEdge:
    """One reference between two norms, oriented from the norm that acts to the one acted on."""

    amending: str
    """Id of the norm that acts."""
    amended: str
    """Id of the norm acted on."""
    code: str
    """The relation code."""
    relation: str
    """Relation text exactly as the side that was read states it (active or passive voice)."""
    kind: RelationKind
    detail: str
    """The reference text, e.g. a provision locator plus the other norm's rank and date."""


@dataclass(frozen=True)
class ChainNode:
    """One law in a chain."""

    law_id: str
    depth: int
    """0 for the root, otherwise the shortest distance from it."""
    expanded: bool
    """Whether its analysis was available and its references were followed."""
    reason: NodeReason
    """Why it was not expanded; empty when it was."""


@dataclass(frozen=True)
class Chain:
    """The result of one exploration."""

    root: str
    direction: Direction
    nodes: tuple[ChainNode, ...]
    """Breadth-first: by depth, then by order of discovery."""
    edges: tuple[ChainEdge, ...]
    """In order of discovery, without duplicates."""
    to_fetch: tuple[str, ...]
    """Ids whose analysis is needed to go on, in breadth-first order."""
    truncated: bool
    """Whether the node limit made the exploration drop at least one reference."""


def explore(
    root: str,
    analyses: Mapping[str, LawAnalysis | None],
    *,
    direction: Direction,
    max_depth: int = 2,
    kinds: Collection[RelationKind] = AMENDING_KINDS,
    fetchable: Callable[[str], bool] = lambda law_id: True,
    max_nodes: int = 150,
) -> Chain:
    """Walk amendment references breadth-first from ``root`` over the analyses known so far.

    ``analyses`` maps a law id to its analysis, or to ``None`` when the analysis is known to be
    unavailable; an id that is absent has not been fetched yet. ``amended_by`` follows each law's
    ``later`` references, ``amends`` its ``earlier`` ones. Raises ``ValueError`` when
    ``max_depth`` is outside 1..3 or ``max_nodes`` is below 1.
    """
    if not MIN_DEPTH <= max_depth <= MAX_DEPTH:
        raise ValueError(f"max_depth must be between {MIN_DEPTH} and {MAX_DEPTH}, got {max_depth}")
    if max_nodes < 1:
        raise ValueError(f"max_nodes must be at least 1, got {max_nodes}")

    # The node list doubles as the breadth-first queue: nodes are appended in discovery order and
    # processed by index, so depth never decreases along the list.
    discovered: list[tuple[str, int]] = [(root, 0)]
    # A law is a node once, at the depth where it was first met. Breadth-first order reaches every
    # law by a shortest path first, so the first sighting is already the shortest depth.
    known: set[str] = {root}
    nodes: list[ChainNode] = []
    edges: list[ChainEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()
    to_fetch: list[str] = []
    truncated = False

    index = 0
    while index < len(discovered):
        law_id, depth = discovered[index]
        index += 1

        analysis = analyses.get(law_id)
        if depth >= max_depth:
            # Checked first so that a node at the limit is never queued for fetching: its
            # references would lie beyond the requested depth.
            nodes.append(ChainNode(law_id, depth, False, "depth_limit"))
            continue
        if analysis is None:
            if law_id in analyses or (law_id != root and not fetchable(law_id)):
                # Ids outside the consolidated collection (corrections, court proceedings,
                # regional decrees, EU norms: about 29% of referenced ids) answer 404 on
                # /analisis, so asking for them would only waste requests (api-notes, fourth
                # probe, "Nodes outside the catalogue"). A recorded ``None`` is a failed fetch.
                nodes.append(ChainNode(law_id, depth, False, "unavailable"))
            else:
                nodes.append(ChainNode(law_id, depth, False, "not_fetched"))
                to_fetch.append(law_id)
            continue

        nodes.append(ChainNode(law_id, depth, True, ""))
        for reference in _references(analysis, direction):
            if not reference.law_id or reference.law_id == law_id:
                # An empty id is a regional decree the BOE does not index (api-notes, fourth
                # probe); a self reference carries no chain information and would only add a loop.
                continue
            code = reference.relation.code
            kind = relation_kind(code)
            if kind not in kinds:
                continue
            if reference.law_id not in known:
                if len(known) >= max_nodes:
                    # No new node past the limit, and no edge to a node that does not exist.
                    truncated = True
                    continue
                known.add(reference.law_id)
                discovered.append((reference.law_id, depth + 1))

            if direction == "amended_by":
                amending, amended = reference.law_id, law_id
            else:
                amending, amended = law_id, reference.law_id
            key = (amending, amended, code)
            # Edges to nodes already known are kept: a cycle or a diamond is exactly the
            # structure the caller wants to see. Each side of an amendment is listed on both laws
            # (mirrored, api-notes fourth probe), so the same (amending, amended, code) can be met
            # twice; the first occurrence wins, which keeps the text of the side read first.
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                ChainEdge(
                    amending=amending,
                    amended=amended,
                    code=code,
                    relation=reference.relation.text,
                    kind=kind,
                    detail=reference.text,
                )
            )

    return Chain(
        root=root,
        direction=direction,
        nodes=tuple(nodes),
        edges=tuple(edges),
        to_fetch=tuple(to_fetch),
        truncated=truncated,
    )


def _references(analysis: LawAnalysis, direction: Direction) -> list[LawReference]:
    """The references to follow from one law: who acted on it, or what it acted on."""
    # ``later`` lists the norms that act on this one, ``earlier`` the norms this one acts on
    # (api-notes, fourth probe, "Direction").
    return analysis.later if direction == "amended_by" else analysis.earlier


def level_counts(chain: Chain) -> tuple[int, ...]:
    """Number of nodes at depth 0, 1, 2, ... up to the deepest node of the chain."""
    counts = [0] * (max((n.depth for n in chain.nodes), default=-1) + 1)
    for node in chain.nodes:
        counts[node.depth] += 1
    return tuple(counts)
