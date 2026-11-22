"""Persistent store of saved searches (standard-library ``sqlite3``).

A saved search is a filter plus a bookmark (see ``saved_searches``). This module is only the
storage: it knows nothing about the BOE and does not interpret filters. It is deliberately
separate from the response cache: the cache may be deleted at any time, a person's saved
searches may not. Every operation takes the owner and touches only that owner's rows; there is
no method that reads across owners.

sqlite3 blocks, so each operation runs in a worker thread (``asyncio.to_thread``) on a single
connection, serialised by a lock so two operations never interleave. The file is opened lazily,
on the first operation, and every failure of the file (corrupt, foreign, newer, unreadable
stored values, unwritable directory) is a ``SearchStoreError``: a bad file must disable saved
searches, never the server that carries the other tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar

from boe_insights_mcp.clock import madrid_today

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

_SCHEMA_VERSION = 2
# Seconds a statement waits for a lock held by another process on the same file.
_BUSY_TIMEOUT = 5.0
_T = TypeVar("_T")

_BASE_COLUMNS = (
    "owner, name, kind, words, section, department_code, law_id, law_title, seen, "
    "checked_through, created_at, last_checked_at"
)
_COLUMNS = f"{_BASE_COLUMNS}, seen_items"
_COLUMN_NAMES = frozenset(_COLUMNS.replace(" ", "").split(","))
_V1_COLUMN_NAMES = frozenset(_BASE_COLUMNS.replace(" ", "").split(","))

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
    seen_items TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (owner, name)
)
"""

_UNAVAILABLE = "the saved-searches file cannot be used"


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
    """Gazette: every day up to this one is finished for this search."""
    seen_items: Mapping[date, frozenset[str]] = field(default_factory=dict)
    """Gazette: identifiers already delivered, per day, for days after ``checked_through``."""
    last_checked_at: datetime | None = None
    """When a check last moved the bookmark; ``None`` if never checked."""


class SearchStoreError(Exception):
    """Base class of the store's errors; also raised when the file itself cannot be used."""


class DuplicateNameError(SearchStoreError):
    """The owner already has a search with that name."""


class SearchLimitError(SearchStoreError):
    """The owner already has the maximum number of searches."""


class SearchNotFoundError(SearchStoreError):
    """The owner has no search with that name."""


class SavedSearchStore:
    """SQLite-backed store of ``SavedSearch`` rows, per owner.

    ``path=None`` keeps everything in memory (tests, and the memory cache backend). With a
    path, every directory the store creates is private (0o700) and the file is private (0o600).
    Nothing is opened until the first operation.
    """

    def __init__(self, path: Path | None, *, max_per_owner: int = 20) -> None:
        """Remember where the store lives; the file is opened by the first operation."""
        self._path = path
        self._max = max_per_owner
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    @property
    def in_memory(self) -> bool:
        """Whether the searches live only in memory and are lost when the server stops."""
        return self._path is None

    # -- opening ------------------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        """The open connection, opening (and migrating) the file on first use.

        Called under the lock. A failure leaves the store closed, so the next operation tries
        again (the person may have fixed the file).
        """
        if self._connection is not None:
            return self._connection
        try:
            connection = self._open()
        except (sqlite3.Error, OSError) as error:
            raise SearchStoreError(_UNAVAILABLE) from error
        self._connection = connection
        return connection

    def _open(self) -> sqlite3.Connection:
        """Open the database and bring its schema to the current version.

        Raises ``SearchStoreError`` for a file from a newer program or with a foreign table, and
        sqlite3 or OS errors for the rest (``_db`` translates those).
        """
        if self._path is None:
            target = ":memory:"
        else:
            _make_private_directories(self._path.parent)
            # Create the file ourselves so its mode is 0o600 from the first byte (sqlite would
            # use the umask), and tighten one an older version or a person left looser.
            os.close(os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600))
            os.chmod(self._path, 0o600)
            target = str(self._path)
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE) so a check and its
        # write are one atomic step. check_same_thread=False: to_thread uses several threads.
        # The timeout is sqlite's busy timeout: two server processes may share one file.
        connection = sqlite3.connect(
            target, isolation_level=None, check_same_thread=False, timeout=_BUSY_TIMEOUT
        )
        try:
            self._initialise(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _initialise(connection: sqlite3.Connection) -> None:
        """Create, validate or migrate the schema in one transaction."""
        # BEGIN IMMEDIATE first so two processes opening one old file cannot both migrate it:
        # the second one waits, then finds version 2.
        connection.execute("BEGIN IMMEDIATE")
        try:
            (version,) = connection.execute("PRAGMA user_version").fetchone()
            if version > _SCHEMA_VERSION:
                raise SearchStoreError(
                    f"the saved-searches file has version {version}, newer than this program "
                    f"understands ({_SCHEMA_VERSION}); update boe-insights-mcp"
                )
            if version == 0:
                connection.execute(_SCHEMA)
            elif version == 1:
                if _columns(connection) != _V1_COLUMN_NAMES:
                    raise SearchStoreError("the saved-searches file has an unexpected layout")
                connection.execute(
                    "ALTER TABLE saved_searches ADD COLUMN seen_items TEXT NOT NULL DEFAULT '{}'"
                )
                _reopen_the_day_of_saving(connection)
            # The version number alone is not trusted: a foreign file can carry any number.
            if _columns(connection) != _COLUMN_NAMES:
                raise SearchStoreError("the saved-searches file has an unexpected layout")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    # -- public operations ----------------------------------------------------------------------

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

    async def update_progress(
        self,
        owner: str,
        name: str,
        *,
        expected: SavedSearch,
        checked_through: date | None,
        seen: frozenset[str],
        seen_items: Mapping[date, frozenset[str]],
        last_checked_at: datetime | None,
    ) -> bool:
        """Write the progress of a check, if the row still has the progress ``expected`` has.

        Only the four progress columns are written, never the name or the filters, so an edit,
        rename or delete made while a check was reading is never undone by it. Returns ``False``
        when the row is gone or its progress changed meanwhile (another check got there first);
        nothing is written then.
        """
        _require_owner(owner)
        return await self._run(
            self._update_progress,
            owner,
            name,
            expected,
            (checked_through, seen, seen_items, last_checked_at),
        )

    async def delete(self, owner: str, name: str) -> None:
        """Delete one search; raises ``SearchNotFoundError``."""
        _require_owner(owner)
        await self._run(self._delete, owner, name)

    async def close(self) -> None:
        """Close the database if it was opened; the store may be used again (it reopens)."""
        await self._run(self._close)

    async def _run(self, function: Callable[..., _T], *args: object) -> _T:
        """Run ``function`` in a worker thread under the lock.

        Any sqlite3 failure or unreadable stored value becomes a ``SearchStoreError`` here, the
        one place every operation goes through.
        """

        def locked() -> _T:
            """``function`` under the lock, with sqlite3 failures translated."""
            with self._lock:
                try:
                    return function(*args)
                except sqlite3.Error as error:
                    raise SearchStoreError(_UNAVAILABLE) from error

        return await asyncio.to_thread(locked)

    # -- synchronous operations (always called under the lock) --------------------------------

    def _close(self) -> None:
        """Close the connection, if there is one."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _transaction(self, work: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run ``work`` in one write transaction; roll back if it raises."""
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            result = work(db)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return result

    @staticmethod
    def _count(db: sqlite3.Connection, owner: str) -> int:
        """Number of rows of ``owner``."""
        (count,) = db.execute(
            "SELECT COUNT(*) FROM saved_searches WHERE owner = ?", (owner,)
        ).fetchone()
        return int(count)

    @staticmethod
    def _exists(db: sqlite3.Connection, owner: str, name: str) -> bool:
        """Whether the row ``(owner, name)`` exists."""
        row = db.execute(
            "SELECT 1 FROM saved_searches WHERE owner = ? AND name = ?", (owner, name)
        ).fetchone()
        return row is not None

    def _add(self, search: SavedSearch) -> None:
        """Insert ``search`` after checking name and limit, in one transaction."""

        def work(db: sqlite3.Connection) -> None:
            """Check, then insert."""
            if self._exists(db, search.owner, search.name):
                raise DuplicateNameError(f"a saved search named '{search.name}' already exists")
            if self._count(db, search.owner) >= self._max:
                raise SearchLimitError(f"the limit of {self._max} saved searches is reached")
            db.execute(
                f"INSERT INTO saved_searches ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _to_row(search),
            )

        self._transaction(work)

    def _list(self, owner: str) -> list[SavedSearch]:
        """The owner's rows ordered by creation time, then name."""
        rows = (
            self._db()
            .execute(
                f"SELECT {_COLUMNS} FROM saved_searches WHERE owner = ? ORDER BY created_at, name",
                (owner,),
            )
            .fetchall()
        )
        return [_from_row(row) for row in rows]

    def _get(self, owner: str, name: str) -> SavedSearch:
        """One row, or ``SearchNotFoundError``."""
        row = (
            self._db()
            .execute(
                f"SELECT {_COLUMNS} FROM saved_searches WHERE owner = ? AND name = ?",
                (owner, name),
            )
            .fetchone()
        )
        if row is None:
            raise SearchNotFoundError(f"no saved search named '{name}'")
        return _from_row(row)

    def _names(self, owner: str) -> set[str]:
        """The owner's names."""
        rows = self._db().execute("SELECT name FROM saved_searches WHERE owner = ?", (owner,))
        return {row[0] for row in rows}

    def _replace(self, owner: str, name: str, search: SavedSearch) -> None:
        """Overwrite the row ``(owner, name)`` in one transaction."""

        def work(db: sqlite3.Connection) -> None:
            """Check the row and the new name, then update."""
            if not self._exists(db, owner, name):
                raise SearchNotFoundError(f"no saved search named '{name}'")
            if search.name != name and self._exists(db, owner, search.name):
                raise DuplicateNameError(f"a saved search named '{search.name}' already exists")
            db.execute(
                "UPDATE saved_searches SET name=?, kind=?, words=?, section=?, "
                "department_code=?, law_id=?, law_title=?, seen=?, checked_through=?, "
                "created_at=?, last_checked_at=?, seen_items=? WHERE owner = ? AND name = ?",
                (*_to_row(search)[1:], owner, name),
            )

        self._transaction(work)

    def _update_progress(
        self,
        owner: str,
        name: str,
        expected: SavedSearch,
        new: tuple[date | None, frozenset[str], Mapping[date, frozenset[str]], datetime | None],
    ) -> bool:
        """Compare-and-set of the progress columns; see ``update_progress``."""
        checked_through, seen, seen_items, last_checked_at = new

        def work(db: sqlite3.Connection) -> bool:
            """Read the row, compare what it holds, then update it."""
            row = db.execute(
                f"SELECT {_COLUMNS} FROM saved_searches WHERE owner = ? AND name = ?",
                (owner, name),
            ).fetchone()
            if row is None:
                return False
            # Why decoded values and not stored text: a row whose JSON is valid but spelled
            # differently (another writer, a hand edit) must not fail the comparison forever.
            # ``created_at`` is part of it so that a row deleted and saved again under the same
            # name during a check is another row and never passes.
            stored = _from_row(row)
            if (
                stored.seen,
                stored.checked_through,
                stored.seen_items,
                stored.last_checked_at,
                stored.created_at,
            ) != (
                expected.seen,
                expected.checked_through,
                expected.seen_items,
                expected.last_checked_at,
                expected.created_at,
            ):
                return False
            db.execute(
                "UPDATE saved_searches SET seen=?, checked_through=?, seen_items=?, "
                "last_checked_at=? WHERE owner = ? AND name = ?",
                (
                    _dump_seen(seen),
                    _day_text(checked_through),
                    _dump_seen_items(seen_items),
                    _moment_text(last_checked_at),
                    owner,
                    name,
                ),
            )
            return True

        return self._transaction(work)

    def _delete(self, owner: str, name: str) -> None:
        """Delete one row, or raise ``SearchNotFoundError``."""
        cursor = self._db().execute(
            "DELETE FROM saved_searches WHERE owner = ? AND name = ?", (owner, name)
        )
        if cursor.rowcount == 0:
            raise SearchNotFoundError(f"no saved search named '{name}'")


def _make_private_directories(directory: Path) -> None:
    """Create ``directory`` and the missing parents, each with mode 0o700."""
    # ``mkdir(parents=True, mode=...)`` applies the mode to the last directory only, and the
    # umask can narrow it further: so each missing one is created and its mode set explicitly.
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for created in reversed(missing):
        created.mkdir(mode=0o700)
        created.chmod(0o700)


def _reopen_the_day_of_saving(connection: sqlite3.Connection) -> None:
    """Move a version-1 gazette bookmark that marked the day of saving or checking one day back.

    Version 1 bookmarked today as finished, which loses what its issue gains later that day;
    version 2 never does, so such a bookmark is one day earlier. Version 1 was never released,
    so this stays small: a row whose dates cannot be read is left as it is.
    """
    rows = connection.execute(
        "SELECT owner, name, checked_through, created_at, last_checked_at FROM saved_searches "
        "WHERE kind = 'gazette' AND checked_through IS NOT NULL"
    ).fetchall()
    for owner, name, through, created, checked in rows:
        try:
            bookmark = date.fromisoformat(through)
            # The Madrid day, the one the BOE's calendar uses, not the UTC one the text holds.
            days = {
                madrid_today(datetime.fromisoformat(text)) for text in (created, checked) if text
            }
        except (ValueError, TypeError):
            continue
        if bookmark in days:
            connection.execute(
                "UPDATE saved_searches SET checked_through = ? WHERE owner = ? AND name = ?",
                ((bookmark - timedelta(days=1)).isoformat(), owner, name),
            )


def _columns(connection: sqlite3.Connection) -> frozenset[str]:
    """The column names of the ``saved_searches`` table (empty when it does not exist)."""
    rows = connection.execute("PRAGMA table_info(saved_searches)").fetchall()
    return frozenset(str(row[1]) for row in rows)


def _require_owner(owner: str) -> None:
    """Refuse an empty owner: it would be a row nobody can claim."""
    if not owner:
        raise ValueError("owner must not be empty")


def _day_text(day: date | None) -> str | None:
    """A date as ISO text, or ``None``."""
    return day.isoformat() if day else None


def _moment_text(moment: datetime | None) -> str | None:
    """A datetime as ISO text, or ``None``."""
    return moment.isoformat() if moment else None


def _dump_seen(seen: frozenset[str]) -> str:
    """``seen`` as a sorted JSON list (deterministic, so it can be compared as text)."""
    return json.dumps(sorted(seen))


def _dump_seen_items(seen_items: Mapping[date, frozenset[str]]) -> str:
    """``seen_items`` as a JSON object of ISO day to sorted identifiers (deterministic)."""
    return json.dumps({day.isoformat(): sorted(items) for day, items in sorted(seen_items.items())})


def _to_row(search: SavedSearch) -> tuple[object, ...]:
    """Column values of ``search`` in ``_COLUMNS`` order (dates as ISO text, sets as JSON)."""
    return (
        search.owner,
        search.name,
        search.kind,
        search.words,
        search.section,
        search.department_code,
        search.law_id,
        search.law_title,
        _dump_seen(search.seen),
        _day_text(search.checked_through),
        search.created_at.isoformat(),
        _moment_text(search.last_checked_at),
        _dump_seen_items(search.seen_items),
    )


def _from_row(row: tuple[object, ...]) -> SavedSearch:
    """Build a ``SavedSearch`` from a row in ``_COLUMNS`` order.

    Raises ``SearchStoreError`` for a stored value that cannot be decoded (a hand-edited file):
    one bad row must not surface as an unexpected exception.
    """
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
        seen_items,
    ) = row
    try:
        return SavedSearch(
            owner=str(owner),
            name=str(name),
            kind="law" if kind == "law" else "gazette",
            words=_text(words),
            section=_text(section),
            department_code=_text(department_code),
            law_id=_text(law_id),
            law_title=_text(law_title),
            seen=frozenset(str(key) for key in json.loads(str(seen))),
            checked_through=date.fromisoformat(str(checked_through)) if checked_through else None,
            created_at=datetime.fromisoformat(str(created_at)),
            last_checked_at=(
                datetime.fromisoformat(str(last_checked_at)) if last_checked_at else None
            ),
            seen_items={
                date.fromisoformat(day): frozenset(str(item) for item in items)
                for day, items in json.loads(str(seen_items)).items()
            },
        )
    except (ValueError, TypeError, AttributeError) as error:
        # json.JSONDecodeError is a ValueError.
        raise SearchStoreError(
            "the saved-searches file holds a value that cannot be read"
        ) from error


def _text(value: object) -> str | None:
    """A nullable text column as ``str | None``."""
    return None if value is None else str(value)
