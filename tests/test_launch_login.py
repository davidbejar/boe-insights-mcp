"""Tests of the launcher around login: warnings, one-line exits and where secrets must not go.

``launch.run`` is driven with a recording stand-in for the server, as in ``tests/test_launch.py``;
the log lines are read with ``caplog``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from boe_insights_mcp import launch

CLIENT_ID = "Iv1.0123456789abcdef"
CLIENT_SECRET = "gh-client-secret-5f2c9a7e1d"
SIGNING_KEY = "signing-key-" + "k" * 32
LOGIN_ENV = {
    "BOE_INSIGHTS_GITHUB_CLIENT_ID": CLIENT_ID,
    "BOE_INSIGHTS_GITHUB_CLIENT_SECRET": CLIENT_SECRET,
    "BOE_INSIGHTS_AUTH_SIGNING_KEY": SIGNING_KEY,
    "BOE_INSIGHTS_BASE_URL": "https://boe.example.org",
}
USERS_VAR = "BOE_INSIGHTS_ALLOWED_USERS"


class _Recorder:
    """Stands in for the server and records how ``launch.run`` started it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, transport: str | None = None, **kwargs: Any) -> None:
        self.calls.append((transport or "stdio", kwargs))


def run(
    argv: list[str], env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> tuple[_Recorder, list[logging.LogRecord]]:
    """Run the launcher; return the recorder and the WARNING records of our own loggers."""
    recorder = _Recorder()
    with caplog.at_level(logging.DEBUG):
        launch.run(argv, env, lambda settings: recorder)  # type: ignore[arg-type,return-value]
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name.startswith("boe_insights_mcp")
    ]
    return recorder, warnings


def assert_no_secret_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Neither the client secret nor the signing key is in any captured record."""
    assert CLIENT_SECRET not in caplog.text
    assert SIGNING_KEY not in caplog.text
    for record in caplog.records:
        assert CLIENT_SECRET not in record.getMessage()
        assert SIGNING_KEY not in record.getMessage()


# --- Login variables with stdio ------------------------------------------------------------------


def test_login_variables_with_stdio_are_ignored_with_one_warning_and_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder, warnings = run(["--transport", "stdio"], LOGIN_ENV, caplog)

    assert recorder.calls == [("stdio", {"show_banner": False})]
    [warning] = warnings
    text = warning.getMessage().lower()
    assert "ignored" in text
    assert "stdio" in text
    assert_no_secret_in_logs(caplog)


def test_login_variables_with_the_default_transport_are_ignored_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder, warnings = run([], LOGIN_ENV, caplog)

    assert [call[0] for call in recorder.calls] == ["stdio"]
    assert len(warnings) == 1
    assert_no_secret_in_logs(caplog)


def test_stdio_without_login_variables_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    _, warnings = run([], {}, caplog)

    assert warnings == []


def test_an_allow_list_alone_with_stdio_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    _, warnings = run([], {USERS_VAR: "alice"}, caplog)

    assert warnings == []


# --- Login over HTTP -----------------------------------------------------------------------------


def test_login_with_an_allow_list_over_http_starts_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {**LOGIN_ENV, USERS_VAR: "alice,id:12"}

    recorder, warnings = run(["--transport", "http"], env, caplog)

    assert [call[0] for call in recorder.calls] == ["http"]
    assert warnings == []
    assert_no_secret_in_logs(caplog)


def test_login_with_an_empty_allow_list_warns_that_nobody_can_sign_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder, warnings = run(["--transport", "http"], LOGIN_ENV, caplog)

    # The server still starts: the warning is not an error.
    assert [call[0] for call in recorder.calls] == ["http"]
    [warning] = warnings
    assert USERS_VAR in warning.getMessage()
    assert_no_secret_in_logs(caplog)


def test_a_blank_allow_list_counts_as_empty(caplog: pytest.LogCaptureFixture) -> None:
    _, warnings = run(["--transport", "http"], {**LOGIN_ENV, USERS_VAR: " , "}, caplog)

    assert [USERS_VAR in w.getMessage() for w in warnings] == [True]


def test_open_http_without_login_does_not_warn_about_the_allow_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, warnings = run(["--transport", "http"], {}, caplog)

    assert warnings == []


# --- A half-configured login ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("missing", "names"),
    [
        (["BOE_INSIGHTS_GITHUB_CLIENT_SECRET"], ["BOE_INSIGHTS_GITHUB_CLIENT_SECRET"]),
        (
            ["BOE_INSIGHTS_AUTH_SIGNING_KEY", "BOE_INSIGHTS_BASE_URL"],
            ["BOE_INSIGHTS_AUTH_SIGNING_KEY", "BOE_INSIGHTS_BASE_URL"],
        ),
    ],
)
def test_a_half_configured_login_exits_with_a_one_line_message_naming_what_is_missing(
    missing: list[str],
    names: list[str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {name: value for name, value in LOGIN_ENV.items() if name not in missing}
    recorder = _Recorder()

    with pytest.raises(SystemExit) as exit_info, caplog.at_level(logging.DEBUG):
        launch.run(["--transport", "http"], env, lambda settings: recorder)  # type: ignore[arg-type,return-value]

    assert exit_info.value.code == 2
    assert recorder.calls == []
    err = capsys.readouterr().err
    message = err.strip().splitlines()[-1]
    for name in names:
        assert name in message
    assert "Traceback" not in err
    assert CLIENT_SECRET not in err
    assert SIGNING_KEY not in err
    assert_no_secret_in_logs(caplog)


def test_a_short_signing_key_exits_with_a_one_line_message_without_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = "q9z-weak-77"
    env = {**LOGIN_ENV, "BOE_INSIGHTS_AUTH_SIGNING_KEY": key}

    with pytest.raises(SystemExit) as exit_info:
        launch.resolve_settings(["--transport", "http"], env)

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "BOE_INSIGHTS_AUTH_SIGNING_KEY" in err.strip().splitlines()[-1]
    assert key not in err


def test_a_bad_allowed_users_entry_exits_with_a_one_line_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        launch.resolve_settings([], {USERS_VAR: "alice,bad name"})

    assert exit_info.value.code == 2
    message = capsys.readouterr().err.strip().splitlines()[-1]
    assert USERS_VAR in message
    assert "bad name" in message


def test_resolve_settings_keeps_the_login_values_for_the_factory() -> None:
    settings = launch.resolve_settings(["--transport", "http"], LOGIN_ENV)

    assert settings.login_enabled is True
    assert settings.github_client_id == CLIENT_ID
    assert settings.base_url == "https://boe.example.org"
