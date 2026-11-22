"""Tests of ``searchstore``: the sqlite-backed store of saved searches.

Every test that needs a file uses ``tmp_path``; the rest use the in-memory store. Nothing here
touches the user's data directory or the network.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from boe_insights_mcp.searchstore import (
    DuplicateNameError,
    SavedSearch,
    SavedSearchStore,
    SearchLimitError,
    SearchNotFoundError,
    SearchStoreError,
)

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
ALICE = "user:alice"
BOB = "user:bob"


def gazette(owner: str, name: str, *, minutes: int = 0, **extra: object) -> SavedSearch:
    """A gazette search created ``minutes`` after ``T0``."""
    fields: dict[str, object] = {
        "words": "vivienda",
        "checked_through": date(2026, 11, 1),
        **extra,
    }
    return SavedSearch(
        owner=owner,
        name=name,
        kind="gazette",
        created_at=T0 + timedelta(minutes=minutes),
        **fields,  # type: ignore[arg-type]
    )


def law(owner: str, name: str, *, minutes: int = 0, **extra: object) -> SavedSearch:
    """A law search created ``minutes`` after ``T0``."""
    fields: dict[str, object] = {
        "law_id": "BOE-A-2015-10566",
        "law_title": "Ley 39/2015",
        "seen": frozenset({"BOE-A-2020-1|270|art. 1"}),
        **extra,
    }
    return SavedSearch(
        owner=owner,
        name=name,
        kind="law",
        created_at=T0 + timedelta(minutes=minutes),
        **fields,  # type: ignore[arg-type]
    )


@pytest.fixture
async def store() -> AsyncIterator[SavedSearchStore]:
    """An in-memory store with the default limit."""
    opened = SavedSearchStore(None)
    yield opened
    await opened.close()


# --- basic operations --------------------------------------------------------------------------


async def test_an_added_search_is_read_back_whole(store: SavedSearchStore) -> None:
    saved = gazette(
        ALICE,
        "vivienda",
        section="3",
        department_code="8161",
        last_checked_at=T0 + timedelta(days=2),
    )
    await store.add(saved)

    assert await store.get(ALICE, "vivienda") == saved


async def test_a_law_search_round_trips_its_seen_set_and_no_gazette_fields(
    store: SavedSearchStore,
) -> None:
    saved = law(ALICE, "ley-39-2015", seen=frozenset({"a|270|x", "b|210|y"}))
    await store.add(saved)

    found = await store.get(ALICE, "ley-39-2015")

    assert found == saved
    assert found.seen == frozenset({"a|270|x", "b|210|y"})
    assert isinstance(found.seen, frozenset)
    assert found.words is None
    assert found.checked_through is None
    assert found.created_at.tzinfo is not None


async def test_get_of_an_unknown_name_raises(store: SavedSearchStore) -> None:
    with pytest.raises(SearchNotFoundError):
        await store.get(ALICE, "nada")


def test_the_error_classes_share_a_base() -> None:
    for error in (DuplicateNameError, SearchLimitError, SearchNotFoundError):
        assert issubclass(error, SearchStoreError)
    assert issubclass(SearchStoreError, Exception)


async def test_list_is_oldest_first_then_by_name(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "late", minutes=30))
    await store.add(gazette(ALICE, "b-tie", minutes=10))
    await store.add(gazette(ALICE, "a-tie", minutes=10))
    await store.add(gazette(ALICE, "first", minutes=0))

    assert [s.name for s in await store.list(ALICE)] == ["first", "a-tie", "b-tie", "late"]


async def test_list_of_an_owner_without_searches_is_empty(store: SavedSearchStore) -> None:
    assert await store.list(ALICE) == []


async def test_names_are_a_set(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "uno"))
    await store.add(law(ALICE, "dos"))

    assert await store.names(ALICE) == {"uno", "dos"}


async def test_a_duplicate_name_is_refused_and_leaves_the_original(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno", words="original")
    await store.add(original)

    with pytest.raises(DuplicateNameError):
        await store.add(gazette(ALICE, "uno", words="otro"))

    assert await store.list(ALICE) == [original]


async def test_the_default_limit_is_twenty(store: SavedSearchStore) -> None:
    for number in range(20):
        await store.add(gazette(ALICE, f"s{number}"))

    with pytest.raises(SearchLimitError):
        await store.add(gazette(ALICE, "one-too-many"))

    assert len(await store.list(ALICE)) == 20


async def test_the_limit_is_per_owner_and_configurable() -> None:
    store = SavedSearchStore(None, max_per_owner=2)
    await store.add(gazette(ALICE, "a1"))
    await store.add(gazette(ALICE, "a2"))

    with pytest.raises(SearchLimitError):
        await store.add(gazette(ALICE, "a3"))
    # Another owner is not affected by Alice being full.
    await store.add(gazette(BOB, "b1"))
    await store.add(gazette(BOB, "b2"))
    with pytest.raises(SearchLimitError):
        await store.add(gazette(BOB, "b3"))
    await store.close()


async def test_deleting_frees_a_place_under_the_limit() -> None:
    store = SavedSearchStore(None, max_per_owner=1)
    await store.add(gazette(ALICE, "uno"))
    await store.delete(ALICE, "uno")

    await store.add(gazette(ALICE, "dos"))

    assert await store.names(ALICE) == {"dos"}
    await store.close()


async def test_a_full_owner_adding_an_existing_name_gets_a_store_error() -> None:
    # A full owner adding an existing name gets one of the two store errors, never a new row.
    store = SavedSearchStore(None, max_per_owner=1)
    await store.add(gazette(ALICE, "uno"))

    with pytest.raises(SearchStoreError):
        await store.add(gazette(ALICE, "uno"))

    assert len(await store.list(ALICE)) == 1
    await store.close()


# --- delete ------------------------------------------------------------------------------------


async def test_delete_removes_only_that_search(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "uno"))
    await store.add(gazette(ALICE, "dos"))

    await store.delete(ALICE, "uno")

    assert await store.names(ALICE) == {"dos"}


async def test_delete_of_an_unknown_name_raises(store: SavedSearchStore) -> None:
    with pytest.raises(SearchNotFoundError):
        await store.delete(ALICE, "nada")


# --- replace -----------------------------------------------------------------------------------


async def test_replace_swaps_the_row_keeping_its_name(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "uno", words="viejo"))
    changed = gazette(ALICE, "uno", words="nuevo", last_checked_at=T0)

    await store.replace(ALICE, "uno", changed)

    assert await store.get(ALICE, "uno") == changed
    assert len(await store.list(ALICE)) == 1


async def test_replace_can_rename(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "uno"))
    await store.add(gazette(ALICE, "otro"))

    await store.replace(ALICE, "uno", gazette(ALICE, "renamed"))

    assert await store.names(ALICE) == {"renamed", "otro"}
    with pytest.raises(SearchNotFoundError):
        await store.get(ALICE, "uno")


async def test_replace_onto_a_name_of_another_row_is_refused_and_changes_nothing(
    store: SavedSearchStore,
) -> None:
    first = gazette(ALICE, "uno", words="primero")
    second = gazette(ALICE, "dos", words="segundo")
    await store.add(first)
    await store.add(second)

    with pytest.raises(DuplicateNameError):
        await store.replace(ALICE, "uno", gazette(ALICE, "dos", words="intruso"))

    assert await store.get(ALICE, "uno") == first
    assert await store.get(ALICE, "dos") == second


async def test_replace_of_an_unknown_name_raises_and_adds_nothing(
    store: SavedSearchStore,
) -> None:
    with pytest.raises(SearchNotFoundError):
        await store.replace(ALICE, "nada", gazette(ALICE, "nada"))

    assert await store.list(ALICE) == []


async def test_replace_with_another_owner_inside_the_search_is_a_value_error(
    store: SavedSearchStore,
) -> None:
    await store.add(gazette(ALICE, "uno"))

    with pytest.raises(ValueError):
        await store.replace(ALICE, "uno", gazette(BOB, "uno"))

    assert (await store.get(ALICE, "uno")).owner == ALICE
    assert await store.list(BOB) == []


# --- owner isolation ---------------------------------------------------------------------------


async def test_one_owner_cannot_see_the_rows_of_another(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "privada"))

    assert await store.list(BOB) == []
    assert await store.names(BOB) == set()
    with pytest.raises(SearchNotFoundError):
        await store.get(BOB, "privada")


async def test_one_owner_cannot_delete_the_rows_of_another(store: SavedSearchStore) -> None:
    original = gazette(ALICE, "privada")
    await store.add(original)

    with pytest.raises(SearchNotFoundError):
        await store.delete(BOB, "privada")

    assert await store.get(ALICE, "privada") == original


async def test_one_owner_cannot_replace_the_rows_of_another(store: SavedSearchStore) -> None:
    original = gazette(ALICE, "privada")
    await store.add(original)

    with pytest.raises(SearchNotFoundError):
        await store.replace(BOB, "privada", gazette(BOB, "privada", words="robada"))

    assert await store.get(ALICE, "privada") == original
    assert await store.list(BOB) == []


async def test_owners_may_use_the_same_name(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "vivienda", words="alice"))
    await store.add(gazette(BOB, "vivienda", words="bob"))

    assert (await store.get(ALICE, "vivienda")).words == "alice"
    assert (await store.get(BOB, "vivienda")).words == "bob"


async def test_a_rename_may_take_a_name_that_only_another_owner_uses(
    store: SavedSearchStore,
) -> None:
    await store.add(gazette(ALICE, "uno"))
    await store.add(gazette(BOB, "dos"))

    await store.replace(ALICE, "uno", gazette(ALICE, "dos"))

    assert await store.names(ALICE) == {"dos"}
    assert await store.names(BOB) == {"dos"}


async def test_deleting_one_owners_row_leaves_the_same_name_of_another(
    store: SavedSearchStore,
) -> None:
    await store.add(gazette(ALICE, "vivienda"))
    await store.add(gazette(BOB, "vivienda"))

    await store.delete(ALICE, "vivienda")

    assert await store.names(BOB) == {"vivienda"}


async def test_an_empty_owner_is_refused_by_every_method(store: SavedSearchStore) -> None:
    await store.add(gazette(ALICE, "uno"))

    with pytest.raises(ValueError):
        await store.add(gazette("", "uno"))
    with pytest.raises(ValueError):
        await store.list("")
    with pytest.raises(ValueError):
        await store.get("", "uno")
    with pytest.raises(ValueError):
        await store.names("")
    with pytest.raises(ValueError):
        await store.replace("", "uno", gazette("", "uno"))
    with pytest.raises(ValueError):
        await store.delete("", "uno")


# --- concurrency -------------------------------------------------------------------------------


async def test_concurrent_adds_do_not_exceed_the_limit() -> None:
    store = SavedSearchStore(None, max_per_owner=3)

    outcomes = await asyncio.gather(
        *(store.add(gazette(ALICE, f"s{number}", minutes=number)) for number in range(10)),
        return_exceptions=True,
    )

    assert sum(outcome is None for outcome in outcomes) == 3
    assert all(isinstance(o, SearchLimitError) for o in outcomes if o is not None)
    assert len(await store.list(ALICE)) == 3
    await store.close()


async def test_concurrent_adds_of_one_name_keep_exactly_one() -> None:
    store = SavedSearchStore(None)

    outcomes = await asyncio.gather(
        *(store.add(gazette(ALICE, "same", words=f"w{number}")) for number in range(6)),
        return_exceptions=True,
    )

    assert sum(outcome is None for outcome in outcomes) == 1
    assert all(isinstance(o, DuplicateNameError) for o in outcomes if o is not None)
    assert len(await store.list(ALICE)) == 1
    await store.close()


# --- persistence and the file ------------------------------------------------------------------


async def test_data_survives_closing_and_reopening(tmp_path: Path) -> None:
    path = tmp_path / "data" / "saved_searches.sqlite3"
    first = SavedSearchStore(path)
    one = gazette(ALICE, "uno", section="1", last_checked_at=T0)
    two = law(BOB, "dos")
    await first.add(one)
    await first.add(two)
    await first.close()

    second = SavedSearchStore(path)

    assert await second.list(ALICE) == [one]
    assert await second.get(BOB, "dos") == two
    await second.close()


async def test_a_change_persists_after_reopening(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    first = SavedSearchStore(path)
    await first.add(gazette(ALICE, "uno"))
    await first.add(gazette(ALICE, "dos"))
    await first.replace(ALICE, "uno", gazette(ALICE, "renamed", words="nuevo"))
    await first.delete(ALICE, "dos")
    await first.close()

    second = SavedSearchStore(path)

    assert [s.name for s in await second.list(ALICE)] == ["renamed"]
    assert (await second.get(ALICE, "renamed")).words == "nuevo"
    await second.close()


async def test_the_parent_directory_is_created_private_and_the_file_too(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data" / "saved_searches.sqlite3"
    opened = SavedSearchStore(path)
    await opened.add(gazette(ALICE, "uno"))
    await opened.close()

    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_a_new_file_gets_the_current_user_version(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    opened = SavedSearchStore(path)
    await opened.add(gazette(ALICE, "uno"))
    await opened.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
    finally:
        connection.close()


async def test_a_file_written_by_a_newer_program_is_refused_and_left_unmodified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future (x TEXT)")
    connection.execute("INSERT INTO future VALUES ('keep me')")
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    # The refusal may come from the constructor or from the first operation.
    with pytest.raises(SearchStoreError):
        refused = SavedSearchStore(path)
        await refused.list(ALICE)

    assert path.read_bytes() == before
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone() == (3,)
        assert check.execute("SELECT x FROM future").fetchall() == [("keep me",)]
        tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master")}
        assert tables == {"future"}
    finally:
        check.close()


async def test_in_memory_stores_are_independent() -> None:
    first = SavedSearchStore(None)
    second = SavedSearchStore(None)
    await first.add(gazette(ALICE, "uno"))

    assert await second.list(ALICE) == []
    await first.close()
    await second.close()


# --- items already delivered (schema version 2) ------------------------------------------------


async def test_seen_items_round_trip_per_day(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    saved = gazette(
        ALICE,
        "uno",
        seen_items={date(2026, 11, 3): frozenset({"A", "B"}), date(2026, 11, 4): frozenset()},
    )
    first = SavedSearchStore(path)
    await first.add(saved)
    await first.close()

    second = SavedSearchStore(path)

    assert (await second.get(ALICE, "uno")).seen_items == saved.seen_items
    await second.close()


_V1_SCHEMA = """
CREATE TABLE saved_searches (
    owner TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, words TEXT, section TEXT,
    department_code TEXT, law_id TEXT, law_title TEXT, seen TEXT NOT NULL, checked_through TEXT,
    created_at TEXT NOT NULL, last_checked_at TEXT, PRIMARY KEY (owner, name)
)
"""


def write_version_one(path: Path) -> None:
    """A file as the previous program wrote it: user_version 1, no seen_items column."""
    connection = sqlite3.connect(path)
    connection.execute(_V1_SCHEMA)
    connection.execute(
        "INSERT INTO saved_searches VALUES ('user:alice','old','gazette','vivienda',NULL,NULL,"
        "NULL,NULL,'[]','2026-11-01','2026-10-01T09:00:00+00:00',NULL)"
    )
    connection.execute(
        "INSERT INTO saved_searches VALUES ('user:alice','ley','law',NULL,NULL,NULL,"
        "'BOE-A-2015-10566','Ley 39/2015','[\"k1\"]',NULL,'2026-10-02T09:00:00+00:00',NULL)"
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()


async def test_a_version_one_file_is_migrated_in_place_keeping_every_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    write_version_one(path)

    opened = SavedSearchStore(path)
    rows = await opened.list(ALICE)
    await opened.close()

    assert [row.name for row in rows] == ["old", "ley"]
    assert rows[0].checked_through == date(2026, 11, 1)
    assert rows[0].seen_items == {}
    assert rows[1].seen == frozenset({"k1"})
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone() == (2,)
    finally:
        check.close()


async def test_a_migrated_file_accepts_progress_and_reopens_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    write_version_one(path)
    first = SavedSearchStore(path)
    row = await first.get(ALICE, "old")
    assert await first.update_progress(
        ALICE,
        "old",
        expected=row,
        checked_through=date(2026, 11, 5),
        seen=frozenset(),
        seen_items={date(2026, 11, 6): frozenset({"X"})},
        last_checked_at=T0,
    )
    await first.close()

    second = SavedSearchStore(path)

    updated = await second.get(ALICE, "old")
    assert updated.seen_items == {date(2026, 11, 6): frozenset({"X"})}
    assert updated.checked_through == date(2026, 11, 5)
    await second.close()


async def test_migration_moves_a_bookmark_that_marked_the_day_of_saving_one_day_back(
    tmp_path: Path,
) -> None:
    # Version 1 marked today as finished when saving or checking; version 2 never does.
    path = tmp_path / "saved_searches.sqlite3"
    write_version_one(path)
    connection = sqlite3.connect(path)
    rows = [
        # (name, checked_through, created_at, last_checked_at)
        ("saved-today", "2026-11-21", "2026-11-21T09:00:00+00:00", None),
        ("checked-today", "2026-11-21", "2026-11-01T09:00:00+00:00", "2026-11-21T10:00:00+00:00"),
        # 23:30 UTC on the 20th is 00:30 on the 21st in Madrid: the Madrid day counts.
        ("late-utc", "2026-11-21", "2026-11-20T23:30:00+00:00", None),
        ("earlier", "2026-11-19", "2026-11-21T09:00:00+00:00", "2026-11-20T10:00:00+00:00"),
        ("never", None, "2026-11-21T09:00:00+00:00", None),
    ]
    for name, through, created, checked in rows:
        connection.execute(
            "INSERT INTO saved_searches VALUES ('user:alice',?,'gazette','x',NULL,NULL,NULL,NULL,"
            "'[]',?,?,?)",
            (name, through, created, checked),
        )
    connection.execute(
        "INSERT INTO saved_searches VALUES ('user:alice','law-today','law',NULL,NULL,NULL,'L',"
        "'T','[]','2026-11-21','2026-11-21T09:00:00+00:00',NULL)"
    )
    connection.commit()
    connection.close()

    opened = SavedSearchStore(path)
    through = {row.name: row.checked_through for row in await opened.list(ALICE)}
    await opened.close()

    assert through["saved-today"] == date(2026, 11, 20)
    assert through["checked-today"] == date(2026, 11, 20)
    assert through["late-utc"] == date(2026, 11, 20)
    assert through["earlier"] == date(2026, 11, 19)
    assert through["never"] is None
    assert through["old"] == date(2026, 11, 1)
    assert through["law-today"] == date(2026, 11, 21)  # not a gazette search: left alone


async def test_update_progress_compares_decoded_values_not_stored_text(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    store = SavedSearchStore(path)
    original = gazette(ALICE, "uno", seen=frozenset({"k"}))
    await store.add(original)
    await store.close()
    connection = sqlite3.connect(path)
    # Valid JSON in another spelling than this program writes (another writer, a hand edit).
    connection.execute("UPDATE saved_searches SET seen = '[ \"k\" ]', seen_items = '{ }'")
    connection.commit()
    connection.close()
    store = SavedSearchStore(path)

    assert await progress(store, original) is True
    assert (await store.get(ALICE, "uno")).checked_through == date(2026, 11, 9)
    await store.close()


async def test_update_progress_is_false_for_a_row_deleted_and_saved_again_meanwhile(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno")
    await store.add(original)
    await store.delete(ALICE, "uno")
    # Same name, same progress, saved a moment later: not the row the check was reading.
    await store.add(gazette(ALICE, "uno", minutes=1))

    assert await progress(store, original) is False
    assert (await store.get(ALICE, "uno")).checked_through == date(2026, 11, 1)


# --- update_progress: a check writes only progress, and only if nothing moved --------------------


async def progress(store: SavedSearchStore, expected: SavedSearch, **change: object) -> bool:
    """``update_progress`` from ``expected`` with a new bookmark unless ``change`` says more."""
    values: dict[str, object] = {
        "checked_through": date(2026, 11, 9),
        "seen": expected.seen,
        "seen_items": expected.seen_items,
        "last_checked_at": T0 + timedelta(hours=1),
        **change,
    }
    return await store.update_progress(expected.owner, expected.name, expected=expected, **values)  # type: ignore[arg-type]


async def test_update_progress_changes_only_the_progress_columns(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno", section="3")
    await store.add(original)

    assert await progress(store, original) is True

    row = await store.get(ALICE, "uno")
    assert row.checked_through == date(2026, 11, 9)
    assert row.last_checked_at == T0 + timedelta(hours=1)
    assert (row.words, row.section, row.name, row.created_at) == ("vivienda", "3", "uno", T0)


async def test_update_progress_keeps_a_filter_edited_after_the_check_started(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno")
    await store.add(original)
    await store.replace(ALICE, "uno", gazette(ALICE, "uno", words="alquiler"))

    assert await progress(store, original) is True

    assert (await store.get(ALICE, "uno")).words == "alquiler"


async def test_update_progress_of_a_renamed_or_deleted_row_is_false_and_harmless(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno")
    await store.add(original)
    await store.replace(ALICE, "uno", gazette(ALICE, "dos"))

    assert await progress(store, original) is False
    assert [row.name for row in await store.list(ALICE)] == ["dos"]
    await store.delete(ALICE, "dos")
    assert await progress(store, gazette(ALICE, "dos")) is False
    assert await store.list(ALICE) == []


async def test_update_progress_is_false_when_the_progress_moved_meanwhile(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno")
    await store.add(original)

    assert await progress(store, original, checked_through=date(2026, 11, 5)) is True
    assert await progress(store, original, checked_through=date(2026, 11, 7)) is False

    assert (await store.get(ALICE, "uno")).checked_through == date(2026, 11, 5)


async def test_update_progress_compares_the_seen_items_too(store: SavedSearchStore) -> None:
    original = gazette(ALICE, "uno")
    await store.add(original)
    day = date(2026, 11, 10)

    assert await progress(store, original, seen_items={day: frozenset({"A"})}) is True
    assert await progress(store, original, seen_items={day: frozenset({"B"})}) is False

    assert (await store.get(ALICE, "uno")).seen_items == {day: frozenset({"A"})}


async def test_update_progress_is_false_when_only_the_seen_items_moved(
    store: SavedSearchStore,
) -> None:
    original = gazette(ALICE, "uno")
    await store.add(original)
    day = date(2026, 11, 10)
    same = {"checked_through": original.checked_through, "last_checked_at": None}
    # Everything but the seen items stays as it was, so only they can tell the row has moved.
    assert await progress(store, original, seen_items={day: frozenset({"A"})}, **same) is True

    assert await progress(store, original, seen_items={day: frozenset({"B"})}, **same) is False
    assert (await store.get(ALICE, "uno")).seen_items == {day: frozenset({"A"})}


async def test_update_progress_touches_only_the_owner_given(store: SavedSearchStore) -> None:
    mine = gazette(ALICE, "uno")
    await store.add(mine)
    await store.add(gazette(BOB, "uno"))

    assert await progress(store, mine) is True

    assert (await store.get(BOB, "uno")).checked_through == date(2026, 11, 1)


# --- a bad file never breaks the server: every failure is a SearchStoreError --------------------


def make_bad_file(path: Path, kind: str) -> None:
    """Write a file the store cannot use, of the given ``kind``."""
    if kind == "corrupt":
        path.write_bytes(b"this is not a database" * 100)
        return
    connection = sqlite3.connect(path)
    if kind == "foreign":
        connection.execute("CREATE TABLE saved_searches (id INTEGER PRIMARY KEY, query TEXT)")
        connection.execute("PRAGMA user_version = 1")
    elif kind == "foreign-v2":
        connection.execute("CREATE TABLE saved_searches (id INTEGER PRIMARY KEY, query TEXT)")
        connection.execute("PRAGMA user_version = 2")
    elif kind == "foreign-v0":
        connection.execute("CREATE TABLE saved_searches (id INTEGER PRIMARY KEY, query TEXT)")
    elif kind == "bad-date":
        connection.execute(
            _V1_SCHEMA.replace("seen TEXT", "seen_items TEXT NOT NULL DEFAULT '{}', seen TEXT")
        )
        connection.execute(
            "INSERT INTO saved_searches VALUES ('user:alice','bad','gazette','y',NULL,NULL,NULL,"
            "NULL,'{}','[]','not-a-date','2026-01-02',NULL)"
        )
        connection.execute("PRAGMA user_version = 2")
    elif kind == "bad-json":
        connection.execute(
            _V1_SCHEMA.replace("seen TEXT", "seen_items TEXT NOT NULL DEFAULT '{}', seen TEXT")
        )
        connection.execute(
            "INSERT INTO saved_searches VALUES ('user:alice','bad','gazette','y',NULL,NULL,NULL,"
            "NULL,'{not json','[]','2026-11-01','2026-01-02',NULL)"
        )
        connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    "kind", ["corrupt", "foreign", "foreign-v2", "foreign-v0", "bad-date", "bad-json"]
)
async def test_an_unusable_file_raises_search_store_error_on_use_not_on_creation(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    make_bad_file(path, kind)

    unused = SavedSearchStore(path)  # lazy: building it never fails
    with pytest.raises(SearchStoreError):
        await unused.list(ALICE)
    await unused.close()


async def test_an_unwritable_directory_raises_search_store_error_on_use(
    tmp_path: Path,
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        unused = SavedSearchStore(locked / "sub" / "saved_searches.sqlite3")
        with pytest.raises(SearchStoreError):
            await unused.names(ALICE)
        await unused.close()
    finally:
        locked.chmod(stat.S_IRWXU)


async def test_a_path_that_is_a_directory_raises_search_store_error(tmp_path: Path) -> None:
    unused = SavedSearchStore(tmp_path)
    with pytest.raises(SearchStoreError):
        await unused.names(ALICE)
    await unused.close()


async def test_a_failure_to_open_is_retried_on_the_next_use(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    path.write_bytes(b"garbage" * 50)
    unused = SavedSearchStore(path)
    with pytest.raises(SearchStoreError):
        await unused.names(ALICE)

    path.unlink()

    assert await unused.names(ALICE) == set()
    await unused.close()


async def test_the_store_does_not_touch_the_disk_until_it_is_used(tmp_path: Path) -> None:
    path = tmp_path / "data" / "saved_searches.sqlite3"

    unused = SavedSearchStore(path)
    await unused.close()

    assert not path.parent.exists()


# --- modes -------------------------------------------------------------------------------------


async def test_every_directory_the_store_creates_is_private(tmp_path: Path) -> None:
    old_umask = os.umask(0o022)
    try:
        path = tmp_path / "a" / "b" / "c" / "saved_searches.sqlite3"
        opened = SavedSearchStore(path)
        await opened.names(ALICE)
        await opened.close()
    finally:
        os.umask(old_umask)

    for directory in (path.parent, path.parent.parent, path.parent.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_an_existing_file_with_loose_permissions_is_tightened(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    first = SavedSearchStore(path)
    await first.names(ALICE)
    await first.close()
    path.chmod(0o644)

    second = SavedSearchStore(path)
    await second.names(ALICE)
    await second.close()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- two processes sharing a file ------------------------------------------------------------


async def test_two_stores_on_one_file_wait_for_each_other_instead_of_failing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    one, two = SavedSearchStore(path), SavedSearchStore(path)

    await asyncio.gather(
        *(one.add(gazette(ALICE, f"a{n}", minutes=n)) for n in range(5)),
        *(two.add(gazette(BOB, f"b{n}", minutes=n)) for n in range(5)),
    )

    assert len(await one.names(ALICE)) == 5
    assert len(await two.names(BOB)) == 5
    await one.close()
    await two.close()


def test_the_store_says_whether_it_is_in_memory() -> None:
    assert SavedSearchStore(None).in_memory is True
    assert SavedSearchStore(Path("somewhere.sqlite3")).in_memory is False
