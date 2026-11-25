"""Tests of the login rules the spec left open: half-configured login, blanks, names, base URL."""

from __future__ import annotations

import logging

import pytest

from boe_insights_mcp import launch
from boe_insights_mcp.access import AllowList
from boe_insights_mcp.settings import Settings

from .test_settings_login import CLIENT_ID, ID_VAR, LOGIN_ENV, SECRET_VAR, URL_VAR


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_a_half_configured_login_is_an_error_whatever_the_transport(transport: str) -> None:
    with pytest.raises(ValueError, match=SECRET_VAR):
        Settings.from_env({ID_VAR: CLIENT_ID, "BOE_INSIGHTS_TRANSPORT": transport})


def test_a_half_configured_login_exits_under_stdio_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        launch.resolve_settings([], {ID_VAR: CLIENT_ID})

    assert exit_info.value.code == 2
    assert SECRET_VAR in capsys.readouterr().err


def test_a_fully_configured_login_under_stdio_is_ignored_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Recorder:
        def run(self, *args: object, **kwargs: object) -> None:
            pass

    with caplog.at_level(logging.WARNING):
        launch.run([], LOGIN_ENV, lambda settings: Recorder())  # type: ignore[arg-type,return-value]

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.parametrize("blank", [" ", "\t", "  \n "])
def test_a_variable_of_only_whitespace_counts_as_unset(blank: str) -> None:
    assert Settings.from_env(dict.fromkeys(LOGIN_ENV, blank)).login_enabled is False
    with pytest.raises(ValueError, match=ID_VAR):
        Settings.from_env({**LOGIN_ENV, ID_VAR: blank})


@pytest.mark.parametrize("entry", ["-alice", "alice-", "-", "a-b-", "id:5 5", "ID:5", "Id:5"])
def test_a_username_may_not_start_or_end_with_a_hyphen_and_id_is_lowercase(entry: str) -> None:
    with pytest.raises(ValueError):
        AllowList.parse([entry])


@pytest.mark.parametrize(
    "given",
    [
        "https://boe.example.org/mcp",
        "https://boe.example.org/a/",
        "https://user@boe.example.org",
        "https://user:pass@boe.example.org",
        "https://boe.example.org?",
        "https://boe.example.org#",
        "https://boe.example.org:99999",
        "https://boe.example.org:abc",
        "https://boe.example.org//",
    ],
)
def test_a_base_url_with_a_path_credentials_or_a_bad_port_is_refused(given: str) -> None:
    with pytest.raises(ValueError) as caught:
        Settings.from_env({**LOGIN_ENV, URL_VAR: given})

    assert URL_VAR in str(caught.value)
    assert "pass" not in str(caught.value)
