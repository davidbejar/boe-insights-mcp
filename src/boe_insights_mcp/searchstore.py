"""Persistent store of saved searches (standard-library ``sqlite3``).

A saved search is a filter plus a bookmark (see ``saved_searches``). This module is only the
storage: it knows nothing about the BOE and does not interpret filters. It is deliberately
separate from the response cache: the cache may be deleted at any time, a person's saved
searches may not. Every operation takes the owner and touches only that owner's rows; there is
no method that reads across owners.

sqlite3 blocks, so each operation runs in a worker thread (``asyncio.to_thread``) on a single
connection, serialised by a lock so two operations never interleave.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TypeVar

__all__ = [
    "DuplicateNameError",
    "SavedSearch",
    "SavedSearchStore",
    "SearchKind",
    "SearchLimitError",
    "SearchNotFoundError",
    "SearchStoreError",
]

SearchKind = Literal["gazette", "law"]

_SCHEMA_VERSION = 1
_T = TypeVar("_T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_searches (
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    words TEXT,
    section TEXT,
    department_code TEXT,
    law_id TEXT,
    law_title TEXT,
    seen TEXT NOT NULL,
    checked_through TEXT,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    PRIMARY KEY (owner, name)
)
"""

_COLUMNS = (
    "owner, name, kind, words, section, department_code, law_id, law_title, seen, "
    "checked_through, created_at, last_checked_at"
)


@dataclass(frozen=True)
class SavedSearch:
    """One saved search: a filter (gazette or law) and its bookmark."""

    owner: str
    name: str
    """A slug, unique per owner."""
    kind: SearchKind
    created_at: datetime
    """UTC, timezone-aware."""
    words: str | None = None
    """Gazette: title words."""
    section: str | None = None
    """Gazette: section code."""
    department_code: str | None = None
    """Gazette: department code."""
    law_id: str | None = None
    """Law: BOE identifier of the followed law."""
    law_title: str | None = None
    """Law: its title when saved."""
    seen: frozenset[str] = frozenset()
    """Law: keys of the references already reported."""
    checked_through: date | None = None
    """Gazette: the last day fully read."""
    last_checked_at: datetime | None = None
    """When a check last moved the bookmark; ``None`` if never checked."""


class SearchStoreError(Exception):
    """Base class of the store's errors."""


class DuplicateNameError(SearchStoreError):
    """The owner already has a search with that name."""


class SearchLimitError(SearchStoreError):
    """The owner already has the maximum number of searches."""


class SearchNotFoundError(SearchStoreError):
    """The owner has no search with that name."""


class SavedSearchStore:
    """SQLite-backed store of ``SavedSearch`` rows, per owner.

    ``path=None`` keeps everything in memory (tests, and the memory cache backend). With a
    path, the parent directory is created private (0o700) and the file private (0o600).
    """

    def __init__(self, path: Path | None, *, max_per_owner: int = 20) -> None:
        """Open (and if needed create) the store at ``path``.

        Raises ``SearchStoreError`` when the file was written by a newer program; the file is
        left untouched in that case.
        """
        self._max = max_per_owner
        self._lock = threading.Lock()
        if path is None:
            target = ":memory:"
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create the file ourselves so its mode is 0o600 from the first byte; sqlite would
            # use the umask. It holds what a person follows.
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            target = str(path)
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE) so a check and its
        # insert are one atomic step. check_same_thread=False: to_thread uses several threads.
        self._db = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
        try:
            self._initialise()
        except BaseException:
            self._db.close()
            raise

    def _initialise(self) -> None:
        """Create the schema on a new file; refuse a file from a newer program."""
        (version,) = self._db.execute("PRAGMA user_version").fetchone()
        if version > _SCHEMA_VERSION:
            raise SearchStoreError(
                f"the saved-searches file has version {version}, newer than this program "
                f"understands ({_SCHEMA_VERSION}); update boe-insights-mcp"
            )
        if version == 0:
            self._db.execute(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    async def add(self, search: SavedSearch) -> None:
        """Store ``search``; raises ``DuplicateNameError`` or ``SearchLimitError``."""
        _require_owner(search.owner)
        await self._run(self._add, search)

    async def list(self, owner: str) -> list[SavedSearch]:
        """Return the owner's searches, oldest first (then by name)."""
        _require_owner(owner)
        return await self._run(self._list, owner)

    async def get(self, owner: str, name: str) -> SavedSearch:
        """Return one search; raises ``SearchNotFoundError``."""
        _require_owner(owner)
        return await self._run(self._get, owner, name)

    async def names(self, owner: str) -> set[str]:
        """Return the names the owner has taken."""
        _require_owner(owner)
        return await self._run(self._names, owner)

    async def replace(self, owner: str, name: str, search: SavedSearch) -> None:
        """Replace the row ``(owner, name)`` by ``search``, whose name may differ (a rename).

        Raises ``ValueError`` if ``search`` belongs to another owner, ``SearchNotFoundError``
        if the row does not exist and ``DuplicateNameError`` if the new name is taken.
        """
        _require_owner(owner)
        if search.owner != owner:
            raise ValueError("a search cannot move to another owner")
        await self._run(self._replace, owner, name, search)

    async def delete(self, owner: str, name: str) -> None:
        """Delete one search; raises ``SearchNotFoundError``."""
        _require_owner(owner)
        await self._run(self._delete, owner, name)

    async def close(self) -> None:
        """Close the database; the store must not be used afterwards."""
        await self._run(self._db.close)

    async def _run(self, function: Callable[..., _T], *args: object) -> _T:
        """Run ``function`` in a worker thread under the lock."""

        def locked() -> _T:
            with self._lock:
                return function(*args)

        return await asyncio.to_thread(locked)

    # -- synchronous operations (always called under the lock) --------------------------------

    def _transaction(self, work: Callable[[], _T]) -> _T:
        """Run ``work`` in one write transaction; roll back if it raises."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            result = work()
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return result

    def _count(self, owner: str) -> int:
        """Number of rows of ``owner``."""
        (count,) = self._db.execute(
            "SELECT COUNT(*) FROM saved_searches WHERE owner = ?", (owner,)
        ).fetchone()
        return int(count)

    def _exists(self, owner: str, name: str) -> bool:
        """Whether the row ``(owner, name)`` exists."""
        row = self._db.execute(
            "SELECT 1 FROM saved_searches WHERE owner = ? AND name = ?", (owner, name)
        ).fetchone()
        return row is not None

    def _add(self, search: SavedSearch) -> None:
        """Insert ``search`` after checking name and limit, in one transaction."""

        def work() -> None:
            if self._exists(search.owner, search.name):
                raise DuplicateNameError(f"a saved search named '{search.name}' already exists")
            if self._count(search.owner) >= self._max:
                raise SearchLimitError(f"the limit of {self._max} saved searches is reached")
            self._db.execute(
                f"INSERT INTO saved_searches ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                _to_row(search),
            )

        self._transaction(work)

    def _list(self, owner: str) -> list[SavedSearch]:
        """The owner's rows ordered by creation time, then name."""
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM saved_searches WHERE owner = ? ORDER BY created_at, name",
            (owner,),
        ).fetchall()
        return [_from_row(row) for row in rows]

    def _get(self, owner: str, name: str) -> SavedSearch:
        """One row, or ``SearchNotFoundError``."""
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM saved_searches WHERE owner = ? AND name = ?", (owner, name)
        ).fetchone()
        if row is None:
            raise SearchNotFoundError(f"no saved search named '{name}'")
        return _from_row(row)

    def _names(self, owner: str) -> set[str]:
        """The owner's names."""
        rows = self._db.execute("SELECT name FROM saved_searches WHERE owner = ?", (owner,))
        return {row[0] for row in rows}

    def _replace(self, owner: str, name: str, search: SavedSearch) -> None:
        """Overwrite the row ``(owner, name)`` in one transaction."""

        def work() -> None:
            if not self._exists(owner, name):
                raise SearchNotFoundError(f"no saved search named '{name}'")
            if search.name != name and self._exists(owner, search.name):
                raise DuplicateNameError(f"a saved search named '{search.name}' already exists")
            self._db.execute(
                "UPDATE saved_searches SET name=?, kind=?, words=?, section=?, "
                "department_code=?, law_id=?, law_title=?, seen=?, checked_through=?, "
                "created_at=?, last_checked_at=? WHERE owner = ? AND name = ?",
                (*_to_row(search)[1:], owner, name),
            )

        self._transaction(work)

    def _delete(self, owner: str, name: str) -> None:
        """Delete one row, or raise ``SearchNotFoundError``."""
        cursor = self._db.execute(
            "DELETE FROM saved_searches WHERE owner = ? AND name = ?", (owner, name)
        )
        if cursor.rowcount == 0:
            raise SearchNotFoundError(f"no saved search named '{name}'")


def _require_owner(owner: str) -> None:
    """Refuse an empty owner: it would be a row nobody can claim."""
    if not owner:
        raise ValueError("owner must not be empty")


def _to_row(search: SavedSearch) -> tuple[object, ...]:
    """Column values of ``search`` in ``_COLUMNS`` order (dates as ISO text, ``seen`` as JSON)."""
    return (
        search.owner,
        search.name,
        search.kind,
        search.words,
        search.section,
        search.department_code,
        search.law_id,
        search.law_title,
        json.dumps(sorted(search.seen)),
        search.checked_through.isoformat() if search.checked_through else None,
        search.created_at.isoformat(),
        search.last_checked_at.isoformat() if search.last_checked_at else None,
    )


def _from_row(row: tuple[object, ...]) -> SavedSearch:
    """Build a ``SavedSearch`` from a row in ``_COLUMNS`` order."""
    (
        owner,
        name,
        kind,
        words,
        section,
        department_code,
        law_id,
        law_title,
        seen,
        checked_through,
        created_at,
        last_checked_at,
    ) = row
    return SavedSearch(
        owner=str(owner),
        name=str(name),
        kind="law" if kind == "law" else "gazette",
        words=_text(words),
        section=_text(section),
        department_code=_text(department_code),
        law_id=_text(law_id),
        law_title=_text(law_title),
        seen=frozenset(json.loads(str(seen))),
        checked_through=date.fromisoformat(str(checked_through)) if checked_through else None,
        created_at=datetime.fromisoformat(str(created_at)),
        last_checked_at=datetime.fromisoformat(str(last_checked_at)) if last_checked_at else None,
    )


def _text(value: object) -> str | None:
    """A nullable text column as ``str | None``."""
    return None if value is None else str(value)
