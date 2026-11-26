"""The sign-in token store is made private (0700) before FastMCP's OAuth proxy creates or uses it.

FastMCP keeps its encrypted OAuth records under ``<FastMCP home>/oauth-proxy/<fingerprint>/`` and
creates those folders with the default mode. ``login.make_token_store_private`` runs before the
proxy is built: it creates ``oauth-proxy`` (and missing parents) with mode 0700, tightens an
existing one and its sub-folders, refuses a symlink or a non-directory there, and leaves an
existing FastMCP home alone. These tests drive the helper directly (for the umask cases), the
real provider through ``server._build`` (for where it runs and where it does not), and
``launch.run`` (for the one-line refusal). Everything lives under ``tmp_path``; nothing reaches
the network.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
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


def test_a_missing_store_and_its_missing_parents_are_created_0700_even_under_a_hostile_umask(
    tmp_path: Path, umask: Any
) -> None:
    home = tmp_path / "a" / "b" / "home"
    # 0o277 strips the owner's write and execute bits: mkdir alone would give 0o500.
    umask(0o277)
    login.make_token_store_private(home)
    umask(0o022)

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


def test_a_store_that_is_already_private_is_not_changed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    login.make_token_store_private(home)
    (home / "oauth-proxy" / "0123456789ab").mkdir(mode=0o700)

    def refuse(*args: Any, **kwargs: Any) -> None:
        """A chmod that always fails, as for a folder someone else owns."""
        raise PermissionError("not the owner")

    monkeypatch.setattr(os, "chmod", refuse)
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)

    assert our_warnings(caplog) == []


def test_a_store_that_cannot_be_tightened_logs_one_warning_naming_it_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = loose_directory(tmp_path / "home")
    store = loose_directory(home / "oauth-proxy")
    loose_directory(store / "0123456789ab")
    real_chmod = os.chmod

    def chmod(path: Any, *args: Any, **kwargs: Any) -> None:
        """Fail like a folder owned by someone else, inside the store only."""
        if Path(path) == store or Path(path).parent == store:
            raise PermissionError(1, "Operation not permitted", str(path))
        real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)
    with caplog.at_level(logging.DEBUG):
        login.make_token_store_private(home)

    [warning] = our_warnings(caplog)
    assert str(store) in warning.getMessage()
    assert "\n" not in warning.getMessage()
    assert mode(store) == 0o755


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


def test_a_symlinked_fingerprint_folder_is_skipped_not_followed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = loose_directory(home / "oauth-proxy")
    target = loose_directory(tmp_path / "elsewhere")
    (store / "0123456789ab").symlink_to(target, target_is_directory=True)

    login.make_token_store_private(home)

    assert mode(store) == 0o700
    assert mode(target) == 0o755


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
