"""Tests of ``searchstore``: the sqlite-backed store of saved searches.

Every test that needs a file uses ``tmp_path``; the rest use the in-memory store. Nothing here
touches the user's data directory or the network.
"""

from __future__ import annotations

import asyncio
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


async def test_a_new_file_gets_user_version_one(tmp_path: Path) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    opened = SavedSearchStore(path)
    await opened.add(gazette(ALICE, "uno"))
    await opened.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    finally:
        connection.close()


async def test_a_file_written_by_a_newer_program_is_refused_and_left_unmodified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "saved_searches.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future (x TEXT)")
    connection.execute("INSERT INTO future VALUES ('keep me')")
    connection.execute("PRAGMA user_version = 2")
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
        assert check.execute("PRAGMA user_version").fetchone() == (2,)
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
