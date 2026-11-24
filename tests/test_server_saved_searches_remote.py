"""Each saved-search tool refuses to act as ``local`` when the server is served over HTTP.

The tool coroutines are called directly with a stand-in context whose lifespan says ``remote``,
outside any HTTP request: there is no token, so the only thing between the call and the ``local``
owner is the ``remote`` flag each tool passes on. A store that records every owner it is asked
about proves that no row was touched.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from boe_insights_mcp import server
from boe_insights_mcp.searchstore import SavedSearchStore

TOOL_CALLS: dict[str, Callable[[Any], Awaitable[Any]]] = {
    "save_search": lambda ctx: server.save_search(ctx, name="mia", words="vivienda"),
    "list_saved_searches": lambda ctx: server.list_saved_searches(ctx),
    "check_saved_searches": lambda ctx: server.check_saved_searches(ctx),
    "edit_saved_search": lambda ctx: server.edit_saved_search(ctx, name="mia", new_name="otra"),
    "delete_saved_search": lambda ctx: server.delete_saved_search(ctx, name="mia"),
}


class RecordingStore(SavedSearchStore):
    """An in-memory store that remembers every owner it was asked about."""

    def __init__(self) -> None:
        super().__init__(None)
        self.owners: list[str] = []

    async def list(self, owner: str) -> Any:
        self.owners.append(owner)
        return await super().list(owner)

    async def get(self, owner: str, name: str) -> Any:
        self.owners.append(owner)
        return await super().get(owner, name)

    async def add(self, search: Any) -> None:
        self.owners.append(search.owner)
        await super().add(search)


@pytest.mark.parametrize("tool", sorted(TOOL_CALLS))
async def test_over_http_without_a_token_the_tool_never_acts_as_local(tool: str) -> None:
    store = RecordingStore()
    ctx = SimpleNamespace(lifespan_context={"remote": True, "searches": store, "client": object()})

    with pytest.raises(ToolError, match="saved searches need an identified user"):
        await TOOL_CALLS[tool](ctx)

    assert store.owners == []
