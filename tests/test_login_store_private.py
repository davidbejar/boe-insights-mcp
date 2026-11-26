"""The sign-in token store is made private (0700) before FastMCP's OAuth proxy creates or uses it.

FastMCP keeps its encrypted OAuth records under ``<FastMCP home>/oauth-proxy/<fingerprint>/`` and
creates those folders with the default mode. ``login.make_token_store_private`` runs before the
proxy is built: it creates ``oauth-proxy`` (and missing parents) with mode 0700, tightens an
existing one and its sub-folders, refuses a symlink or a non-directory there, and leaves an
existing FastMCP home alone. What cannot be made private (not the owner, a filesystem that
refuses or ignores the change, a folder that vanishes) is one WARNING, never a traceback; a race
with a second server, or a link swapped in after the checks, is simulated by patching ``os``
calls. These tests drive the helper directly (for the umask cases), the
real provider through ``server._build`` (for where it runs and where it does not), and
``launch.run`` (for the one-line refusal). Everything lives under ``tmp_path``; nothing reaches
the network.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp.server.auth.oauth_proxy.proxy import OAuthProxy
from key_value.aio.stores.memory import MemoryStore

from boe_insights_mcp import launch, login, server
from boe_insights_mcp.access import AllowList
from boe_insights_mcp.settings import Settings

from .login_support import build, login_env, private_fastmcp_home

__all__ = ["private_fastmcp_home"]  # fixture, imported for pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits are not meaningful on Windows"
)

SIGNING_KEY = "k" * 32
CLIENT_SECRET = "client-secret-VALUE-1234"


def mode(path: Path) -> int:
    """The permission bits of ``path`` (not following a symlink at ``path``)."""
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def umask() -> Iterator[Any]:
    """Set the process umask for one test and restore it afterwards."""
    previous = os.umask(0o022)
    os.umask(previous)

    def set_umask(value: int) -> None:
        """Apply ``value`` as the process umask."""
        os.umask(value)

    yield set_umask
    os.umask(previous)


def our_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The WARNING records logged by this package."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name.startswith("boe_insights_mcp")
    ]


def loose_directory(path: Path) -> Path:
    """Create ``path`` (and parents) and set it to 0755 whatever the umask; return it."""
    path.mkdir(parents=True)
    path.chmod(0o755)
    return path


# --- The helper: creating ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        0o277,  # strips the owner's write and execute bits: mkdir alone would give 0o500
        0o677,  # strips read too (0o100), so the folder cannot even be opened to change it
    ],
)
def test_a_missing_store_and_its_missing_parents_are_created_0700_even_under_a_hostile_umask(
    hostile: int, tmp_path: Path, umask: Any, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "a" / "b" / "home"
    umask(hostile)
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)
    umask(0o022)

    assert our_warnings(caplog) == []

    for created in (tmp_path / "a", tmp_path / "a" / "b", home, home / "oauth-proxy"):
        assert created.is_dir()
        assert mode(created) == 0o700, created


def test_a_missing_store_is_created_0700_under_the_usual_umask(tmp_path: Path, umask: Any) -> None:
    home = tmp_path / "home"
    umask(0o022)

    login.make_token_store_private(home)

    assert mode(home / "oauth-proxy") == 0o700
    assert mode(home) == 0o700  # created by us, so private too


def test_an_existing_home_keeps_its_mode_and_only_the_store_is_created_private(
    tmp_path: Path, umask: Any
) -> None:
    home = loose_directory(tmp_path / "shared-home")
    umask(0o022)

    login.make_token_store_private(home)

    assert mode(home) == 0o755
    assert mode(home / "oauth-proxy") == 0o700


def test_nothing_above_an_existing_home_is_touched(tmp_path: Path) -> None:
    above = loose_directory(tmp_path / "above")
    home = loose_directory(above / "home")

    login.make_token_store_private(home)

    assert mode(above) == 0o755
    assert mode(home) == 0o755


# --- The helper: tightening --------------------------------------------------------------------


def test_an_existing_loose_store_and_its_fingerprint_folders_are_tightened(
    tmp_path: Path,
) -> None:
    home = loose_directory(tmp_path / "home")
    store = loose_directory(home / "oauth-proxy")
    first = loose_directory(store / "0123456789ab")
    second = loose_directory(store / "ba9876543210")
    stray = store / "note.txt"
    stray.write_text("not a folder")
    stray.chmod(0o644)

    login.make_token_store_private(home)

    assert mode(store) == 0o700
    assert mode(first) == 0o700
    assert mode(second) == 0o700
    assert mode(stray) == 0o644  # only folders are FastMCP's fingerprint folders
    assert mode(home) == 0o755


def test_a_store_open_only_to_its_group_is_tightened(tmp_path: Path) -> None:
    store = tmp_path / "home" / "oauth-proxy"
    store.mkdir(parents=True)
    store.chmod(0o750)

    login.make_token_store_private(tmp_path / "home")

    assert mode(store) == 0o700


def fail_chmod_on(monkeypatch: pytest.MonkeyPatch, folders: list[Path], error: OSError) -> None:
    """Make every chmod of one of ``folders`` (by path or by descriptor) raise ``error``."""
    inodes = {folder.stat().st_ino for folder in folders}
    real_fchmod, real_chmod = os.fchmod, os.chmod

    def fchmod(fd: int, mode: int) -> None:
        """Refuse when the descriptor is one of ``folders``."""
        if os.fstat(fd).st_ino in inodes:
            raise error
        real_fchmod(fd, mode)

    def chmod(path: Any, mode: int, **kwargs: Any) -> None:
        """Refuse when the path is one of ``folders``."""
        if os.stat(path).st_ino in inodes:
            raise error
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "fchmod", fchmod)
    monkeypatch.setattr(os, "chmod", chmod)


def test_a_store_that_is_already_private_is_not_changed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    login.make_token_store_private(home)
    inside = home / "oauth-proxy" / "0123456789ab"
    inside.mkdir(mode=0o700)
    fail_chmod_on(monkeypatch, [home / "oauth-proxy", inside], PermissionError(1, "no"))

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)

    assert our_warnings(caplog) == []
    assert our_infos(caplog) == []


def test_a_store_that_cannot_be_tightened_logs_one_warning_naming_it_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = loose_directory(tmp_path / "home")
    store = loose_directory(home / "oauth-proxy")
    inside = loose_directory(store / "0123456789ab")
    fail_chmod_on(
        monkeypatch, [store, inside], PermissionError(1, "Operation not permitted", str(store))
    )

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)

    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()
    assert "\n" not in warning.getMessage()
    assert mode(store) == 0o755


def test_the_warning_names_a_sub_folder_that_cannot_be_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    inside = loose_directory(store / "0123456789ab")
    fail_chmod_on(monkeypatch, [inside], PermissionError(1, "Operation not permitted"))

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    [warning] = our_warnings(caplog)
    assert str(inside) in warning.getMessage()
    assert mode(store) == 0o700
    assert mode(inside) == 0o755


@pytest.mark.parametrize("number", [errno.EROFS, errno.ENOTSUP, errno.EIO])
def test_any_other_chmod_error_on_an_existing_store_is_a_warning_not_a_traceback(
    number: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    fail_chmod_on(monkeypatch, [store], OSError(number, os.strerror(number)))

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()
    assert os.strerror(number) in warning.getMessage()


def test_a_link_swapped_in_during_the_path_fallback_stops_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask: Any
) -> None:
    home = loose_directory(tmp_path / "home")
    store = home / "oauth-proxy"
    target = loose_directory(tmp_path / "elsewhere")
    real_chmod = os.chmod

    def chmod(path: Any, mode: int, **kwargs: Any) -> None:
        """Swap the new store for a link just before it is changed by path."""
        if Path(path) == store and not store.is_symlink():
            store.rmdir()
            store.symlink_to(target, target_is_directory=True)
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)
    umask(0o677)  # the new store cannot be opened, so the fallback runs
    try:
        with pytest.raises(login.TokenStoreError, match="symbolic link"):
            login.make_token_store_private(home)
    finally:
        umask(0o022)


def test_an_existing_store_that_cannot_be_opened_is_reported_not_changed_by_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = tmp_path / "home" / "oauth-proxy"
    store.mkdir(parents=True)
    store.chmod(0o355)  # open to others, but not readable by its owner: no descriptor

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()
    assert mode(store) == 0o355
    store.chmod(0o700)  # so tmp_path can be cleaned up


def test_a_store_that_vanishes_midway_is_a_warning_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    real_lstat = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        """Report the store as removed by the time it is examined."""
        if Path(path) == store:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()


def open_descriptors() -> int:
    """How many file descriptors this process has open."""
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("fails", [False, True])
def test_every_descriptor_opened_to_change_a_mode_is_closed(
    fails: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    folders = [store, *(loose_directory(store / f"{n:012x}") for n in range(5))]
    if fails:
        fail_chmod_on(monkeypatch, folders, OSError(errno.EROFS, os.strerror(errno.EROFS)))
    before = open_descriptors()

    login.make_token_store_private(tmp_path / "home")

    assert open_descriptors() == before


def test_an_unreadable_store_still_starts_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    real_scandir = os.scandir

    def scandir(path: Any = ".") -> Any:
        """Refuse to list the store, as for one its reader may not read."""
        if Path(path) == store:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    assert mode(store) == 0o700
    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()


def test_a_sub_folder_that_vanishes_midway_is_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    gone = loose_directory(store / "0123456789ab")
    real_scandir = os.scandir

    def scandir(path: Any = ".") -> Any:
        """List the store, then remove the sub-folder before it is examined."""
        with real_scandir(path) as listing:
            entries = list(listing)
        if Path(path) == store:
            gone.rmdir()
        return _Listing(entries)

    monkeypatch.setattr(os, "scandir", scandir)

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    assert mode(store) == 0o700
    assert our_warnings(caplog) == []  # nothing left there to protect


def test_a_chmod_the_filesystem_ignores_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()
    assert "did not apply" in warning.getMessage()
    assert our_infos(caplog) == []


def test_a_created_store_the_filesystem_leaves_open_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = loose_directory(tmp_path / "home")
    real_mkdir = os.mkdir

    def mkdir(path: Any, mode: int = 0o777, **kwargs: Any) -> None:
        """A volume with a fixed directory mode: every new folder is 0755."""
        real_mkdir(path, 0o755, **kwargs)
        os.chmod(path, 0o755)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)

    [warning] = our_warnings(caplog)
    assert str(home / "oauth-proxy") in warning.getMessage()
    assert "did not apply" in warning.getMessage()


def our_infos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The INFO records logged by this package."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and record.name.startswith("boe_insights_mcp")
    ]


def test_tightening_an_existing_store_is_logged_once_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    inside = loose_directory(store / "0123456789ab")

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    [info] = our_infos(caplog)
    assert str(store) in info.getMessage()
    assert str(inside) in info.getMessage()
    assert "755" in info.getMessage()
    assert our_warnings(caplog) == []


def test_creating_a_store_logs_no_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(tmp_path / "home")

    assert our_infos(caplog) == []
    assert our_warnings(caplog) == []


# --- Two servers starting at once --------------------------------------------------------------


def race_mkdir(monkeypatch: pytest.MonkeyPatch, contested: Path, competitor: Any) -> None:
    """Make ``mkdir`` of ``contested`` lose a race: ``competitor(contested)`` runs, then EEXIST."""
    real_mkdir = os.mkdir

    def mkdir(path: Any, mode: int = 0o777, **kwargs: Any) -> None:
        """Let the competitor create the path first, as another process would."""
        if Path(path) == contested:
            # One race only: the competitor and everything after it use the real mkdir.
            monkeypatch.setattr(os, "mkdir", real_mkdir)
            competitor(contested)
            raise FileExistsError(17, "File exists", str(path))
        real_mkdir(path, mode, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)


def test_a_store_another_server_creates_first_ends_private_without_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    store = home / "oauth-proxy"
    race_mkdir(monkeypatch, store, lambda path: loose_directory(path))

    login.make_token_store_private(home)

    assert mode(store) == 0o700


def test_a_parent_another_server_creates_first_is_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    race_mkdir(monkeypatch, home, lambda path: loose_directory(path))

    login.make_token_store_private(home)

    assert mode(home / "oauth-proxy") == 0o700
    assert mode(home) == 0o755  # not ours: created by the other process


def test_a_parent_that_turns_out_to_be_a_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    race_mkdir(monkeypatch, home, lambda path: path.write_text(""))

    with pytest.raises(login.TokenStoreError, match="cannot create"):
        login.make_token_store_private(home)


def appear_before_creation(monkeypatch: pytest.MonkeyPatch, store: Path, competitor: Any) -> None:
    """Make ``competitor(store)`` run after the first lookup found no store, before creation."""
    real_create = login._create_private  # pyright: ignore[reportPrivateUsage]

    def create(path: Path, findings: Any) -> bool:
        """Let another process put something at the store first, then create as usual."""
        competitor(store)
        return real_create(path, findings)

    monkeypatch.setattr(login, "_create_private", create)


def test_a_loose_store_appearing_after_the_first_lookup_ends_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = loose_directory(tmp_path / "home")
    store = home / "oauth-proxy"
    appear_before_creation(monkeypatch, store, loose_directory)

    login.make_token_store_private(home)

    assert mode(store) == 0o700


def test_a_symlink_appearing_after_the_first_lookup_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = loose_directory(tmp_path / "home")
    store = home / "oauth-proxy"
    target = loose_directory(tmp_path / "elsewhere")
    appear_before_creation(
        monkeypatch, store, lambda path: path.symlink_to(target, target_is_directory=True)
    )

    with pytest.raises(login.TokenStoreError, match="symbolic link"):
        login.make_token_store_private(home)

    assert mode(target) == 0o755


def test_a_symlink_planted_in_the_creation_window_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = loose_directory(tmp_path / "home")
    store = home / "oauth-proxy"
    target = loose_directory(tmp_path / "elsewhere")
    race_mkdir(monkeypatch, store, lambda path: path.symlink_to(target, target_is_directory=True))

    with pytest.raises(login.TokenStoreError, match="symbolic link"):
        login.make_token_store_private(home)

    assert mode(target) == 0o755


def test_a_symlink_swapped_in_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = loose_directory(tmp_path / "home")
    store = home / "oauth-proxy"
    target = loose_directory(tmp_path / "elsewhere")
    store.symlink_to(target, target_is_directory=True)
    # The up-front check saw a real folder; the link is swapped in before the chmod.
    monkeypatch.setattr(login, "_refuse_untrusted", lambda path: None)

    with pytest.raises(login.TokenStoreError, match="symbolic link"):
        login.make_token_store_private(home)

    assert mode(target) == 0o755


def test_a_file_swapped_in_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = loose_directory(tmp_path / "home")
    store = home / "oauth-proxy"
    store.write_text("")
    monkeypatch.setattr(login, "_refuse_untrusted", lambda path: None)

    with pytest.raises(login.TokenStoreError, match="not a directory"):
        login.make_token_store_private(home)


class _Listing:
    """A finished ``os.scandir`` result that can still be used as a context manager."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> list[os.DirEntry[str]]:
        """Hand out the entries read earlier."""
        return self._entries

    def __exit__(self, *exc: object) -> None:
        """Nothing to close: the real listing is already closed."""

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        """Iterate over the entries read earlier."""
        return iter(self._entries)


def test_a_fingerprint_folder_swapped_for_a_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = loose_directory(tmp_path / "home" / "oauth-proxy")
    inside = loose_directory(store / "0123456789ab")
    target = loose_directory(tmp_path / "elsewhere")
    real_scandir = os.scandir

    def scandir(path: Any = ".") -> Any:
        """List the store, then swap the sub-folder for a link before it is changed."""
        with real_scandir(path) as listing:
            entries = list(listing)
        if Path(path) == store:
            inside.rmdir()
            inside.symlink_to(target, target_is_directory=True)
        return _Listing(entries)

    monkeypatch.setattr(os, "scandir", scandir)

    login.make_token_store_private(tmp_path / "home")

    assert mode(target) == 0o755


# --- The helper: what it refuses ---------------------------------------------------------------


def test_a_symlinked_store_is_refused_and_its_target_is_not_changed(tmp_path: Path) -> None:
    home = loose_directory(tmp_path / "home")
    target = loose_directory(tmp_path / "elsewhere")
    (home / "oauth-proxy").symlink_to(target, target_is_directory=True)

    with pytest.raises(login.TokenStoreError) as refusal:
        login.make_token_store_private(home)

    message = str(refusal.value)
    assert str(home / "oauth-proxy") in message
    assert "symbolic link" in message
    assert "\n" not in message
    assert mode(target) == 0o755


def test_a_dangling_symlinked_store_is_refused_and_nothing_is_created(tmp_path: Path) -> None:
    home = loose_directory(tmp_path / "home")
    target = tmp_path / "missing"
    (home / "oauth-proxy").symlink_to(target, target_is_directory=True)

    with pytest.raises(login.TokenStoreError, match="symbolic link"):
        login.make_token_store_private(home)

    assert not target.exists()


def test_a_store_path_that_is_a_file_is_refused_and_left_alone(tmp_path: Path) -> None:
    home = loose_directory(tmp_path / "home")
    squatter = home / "oauth-proxy"
    squatter.write_text("someone's file")
    squatter.chmod(0o644)

    with pytest.raises(login.TokenStoreError) as refusal:
        login.make_token_store_private(home)

    message = str(refusal.value)
    assert str(squatter) in message
    assert "not a directory" in message
    assert squatter.read_text() == "someone's file"
    assert mode(squatter) == 0o644


def test_a_symlinked_fingerprint_folder_is_skipped_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    store = loose_directory(home / "oauth-proxy")
    target = loose_directory(tmp_path / "elsewhere")
    (store / "0123456789ab").symlink_to(target, target_is_directory=True)

    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)

    assert mode(store) == 0o700
    assert mode(target) == 0o755
    assert our_warnings(caplog) == []  # skipped, not tried and failed


def test_on_windows_nothing_is_created_or_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "_POSIX_MODES", False)
    home = loose_directory(tmp_path / "home")
    (home / "oauth-proxy").symlink_to(tmp_path, target_is_directory=True)
    other = tmp_path / "other-home"

    login.make_token_store_private(home)
    login.make_token_store_private(other)

    assert not other.exists()


# --- Where it runs: the real provider ----------------------------------------------------------


def test_signing_in_over_http_creates_the_store_fastmcp_then_uses(
    tmp_path: Path, private_fastmcp_home: Path, umask: Any
) -> None:
    loose_directory(private_fastmcp_home)
    umask(0o022)

    built, _ = build(login_env(tmp_path))

    store = private_fastmcp_home / "oauth-proxy"
    assert mode(store) == 0o700
    assert mode(private_fastmcp_home) == 0o755
    # The folder FastMCP itself chose for its file store sits inside ours: same resolution.
    provider: Any = built.auth
    data_directory = Path(str(provider._default_file_store._data_directory))
    assert data_directory.parent.resolve() == store.resolve()
    # FastMCP's own ``mkdir(parents=True, exist_ok=True)`` leaves our mode alone.
    assert mode(store) == 0o700


def test_the_store_is_private_before_fastmcp_sets_up_its_storage(
    tmp_path: Path, private_fastmcp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = loose_directory(private_fastmcp_home / "oauth-proxy")
    seen: list[int | None] = []
    real_init = OAuthProxy.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        """Note the store's mode when FastMCP's proxy starts building, then build it."""
        seen.append(mode(store) if store.exists() else None)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(OAuthProxy, "__init__", init)

    build(login_env(tmp_path))

    assert seen == [0o700]


def test_signing_in_over_http_tightens_a_loose_existing_store(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    store = loose_directory(private_fastmcp_home / "oauth-proxy")
    old = loose_directory(store / "0123456789ab")
    private_fastmcp_home.chmod(0o755)

    build(login_env(tmp_path))

    assert mode(store) == 0o700
    assert mode(old) == 0o700
    assert mode(private_fastmcp_home) == 0o755


def test_over_stdio_the_store_is_not_created_even_with_login_variables(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    build(login_env(tmp_path, BOE_INSIGHTS_TRANSPORT="stdio"))

    assert not (private_fastmcp_home / "oauth-proxy").exists()


def test_over_stdio_a_loose_existing_store_is_left_alone(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    store = loose_directory(private_fastmcp_home / "oauth-proxy")

    build(login_env(tmp_path, BOE_INSIGHTS_TRANSPORT="stdio"))

    assert mode(store) == 0o755


def test_over_http_without_sign_in_the_store_is_not_created(
    tmp_path: Path, private_fastmcp_home: Path
) -> None:
    env = {
        "BOE_INSIGHTS_TRANSPORT": "http",
        "BOE_INSIGHTS_CACHE": "memory",
        "BOE_INSIGHTS_DATA_DIR": str(tmp_path / "data"),
        "BOE_INSIGHTS_CACHE_DIR": str(tmp_path / "cache"),
    }

    server._build(Settings.from_env(env))

    assert not (private_fastmcp_home / "oauth-proxy").exists()


def test_a_provider_with_its_own_storage_touches_no_folder(private_fastmcp_home: Path) -> None:
    login.AllowListedGitHubProvider(
        allow_list=AllowList.parse("alice"),
        advertised_scopes=[],
        client_id="Iv1.clientid",
        client_secret=CLIENT_SECRET,
        base_url="https://boe.example.com",
        jwt_signing_key=SIGNING_KEY,
        client_storage=MemoryStore(),
    )

    assert not (private_fastmcp_home / "oauth-proxy").exists()


# --- The refusal at start-up -------------------------------------------------------------------


class _Recorder:
    """Stands in for the server so a broken refusal cannot start a real HTTP server."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, transport: str | None = None, **kwargs: Any) -> None:
        """Record the start instead of serving."""
        self.calls.append(transport or "stdio")


def start(env: dict[str, str]) -> _Recorder:
    """Run the launcher with the real ``_build`` but a recording server; return the recorder."""
    recorder = _Recorder()

    def factory(settings: Settings) -> Any:
        """Build the real server (and provider), then hand back the recorder to be 'run'."""
        server._build(settings)
        return recorder

    launch.run([], env, factory)
    return recorder


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_an_untrustworthy_store_stops_the_server_with_a_one_line_message(
    kind: str,
    tmp_path: Path,
    private_fastmcp_home: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = private_fastmcp_home / "oauth-proxy"
    private_fastmcp_home.mkdir()
    if kind == "symlink":
        store.symlink_to(loose_directory(tmp_path / "elsewhere"), target_is_directory=True)
    else:
        store.write_text("")
    env = login_env(tmp_path, BOE_INSIGHTS_AUTH_SIGNING_KEY=SIGNING_KEY)

    with pytest.raises(SystemExit) as exit_info:
        start(env)

    assert exit_info.value.code not in (0, None)
    message = str(exit_info.value.code)
    assert "\n" not in message
    assert str(store) in message
    assert "Traceback" not in capsys.readouterr().err
    assert SIGNING_KEY not in message
    assert CLIENT_SECRET not in message
    assert SIGNING_KEY not in caplog.text
    assert CLIENT_SECRET not in caplog.text
