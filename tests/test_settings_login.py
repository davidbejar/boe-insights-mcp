"""Tests of the login settings: the four variables, the allow-list variable and their rules.

Contract (spec "Settings"): login is on iff at least one of the four login variables is set; then
all four are required; secrets never appear in ``repr`` or in an error.
"""

from __future__ import annotations

import itertools

import pytest

from boe_insights_mcp.settings import Settings

CLIENT_ID = "Iv1.0123456789abcdef"
CLIENT_SECRET = "gh-client-secret-5f2c9a7e1d"
SIGNING_KEY = "signing-key-" + "k" * 32
BASE_URL = "https://boe.example.org"

ID_VAR = "BOE_INSIGHTS_GITHUB_CLIENT_ID"
SECRET_VAR = "BOE_INSIGHTS_GITHUB_CLIENT_SECRET"
KEY_VAR = "BOE_INSIGHTS_AUTH_SIGNING_KEY"
URL_VAR = "BOE_INSIGHTS_BASE_URL"
USERS_VAR = "BOE_INSIGHTS_ALLOWED_USERS"

LOGIN_ENV = {
    ID_VAR: CLIENT_ID,
    SECRET_VAR: CLIENT_SECRET,
    KEY_VAR: SIGNING_KEY,
    URL_VAR: BASE_URL,
}
SECRETS = (CLIENT_SECRET, SIGNING_KEY)


def _without(*names: str) -> dict[str, str]:
    """``LOGIN_ENV`` minus the variables ``names``."""
    return {name: value for name, value in LOGIN_ENV.items() if name not in names}


# --- Login on and off ---------------------------------------------------------------------------


def test_without_login_variables_login_is_off_and_nothing_is_required() -> None:
    settings = Settings.from_env({})

    assert settings.login_enabled is False
    assert settings.github_client_id is None
    assert settings.github_client_secret is None
    assert settings.auth_signing_key is None
    assert settings.base_url is None
    assert settings.allowed_users == ()


def test_all_four_variables_turn_login_on_and_are_kept() -> None:
    settings = Settings.from_env(LOGIN_ENV)

    assert settings.login_enabled is True
    assert settings.github_client_id == CLIENT_ID
    assert settings.github_client_secret == CLIENT_SECRET
    assert settings.auth_signing_key == SIGNING_KEY
    assert settings.base_url == BASE_URL


def test_login_enabled_does_not_depend_on_the_transport() -> None:
    # The launcher needs to see login configuration under stdio to warn that it is ignored.
    for transport in ("stdio", "http"):
        settings = Settings.from_env({**LOGIN_ENV, "BOE_INSIGHTS_TRANSPORT": transport})

        assert settings.login_enabled is True


def test_empty_login_variables_count_as_unset() -> None:
    env = dict.fromkeys(LOGIN_ENV, "")

    settings = Settings.from_env(env)

    assert settings.login_enabled is False
    assert settings.github_client_id is None


def test_allowed_users_alone_does_not_turn_login_on() -> None:
    settings = Settings.from_env({USERS_VAR: "alice"})

    assert settings.login_enabled is False
    assert settings.allowed_users == ("alice",)


def _names(*variables: str) -> str:
    return ", ".join(variables)


_ALL = (ID_VAR, SECRET_VAR, KEY_VAR, URL_VAR)
_PROPER_SUBSETS = [subset for size in range(1, 4) for subset in itertools.combinations(_ALL, size)]


@pytest.mark.parametrize("present", _PROPER_SUBSETS, ids=lambda subset: "+".join(subset))
def test_a_partly_set_login_names_every_missing_variable(present: tuple[str, ...]) -> None:
    env = {name: LOGIN_ENV[name] for name in present}
    missing = [name for name in _ALL if name not in present]

    with pytest.raises(ValueError) as caught:
        Settings.from_env({**env, "BOE_INSIGHTS_TRANSPORT": "http"})

    message = str(caught.value)
    for name in missing:
        assert name in message


@pytest.mark.parametrize("present", _PROPER_SUBSETS, ids=lambda subset: "+".join(subset))
def test_the_error_of_a_partly_set_login_carries_no_secret(present: tuple[str, ...]) -> None:
    env = {name: LOGIN_ENV[name] for name in present}

    with pytest.raises(ValueError) as caught:
        Settings.from_env({**env, "BOE_INSIGHTS_TRANSPORT": "http"})

    for secret in SECRETS:
        assert secret not in str(caught.value)


def test_an_empty_value_among_the_four_counts_as_missing() -> None:
    env = {**LOGIN_ENV, ID_VAR: ""}

    with pytest.raises(ValueError, match=ID_VAR):
        Settings.from_env({**env, "BOE_INSIGHTS_TRANSPORT": "http"})


# --- Signing key --------------------------------------------------------------------------------


def test_a_signing_key_of_32_characters_is_accepted() -> None:
    key = "k" * 32

    assert Settings.from_env({**LOGIN_ENV, KEY_VAR: key}).auth_signing_key == key


def test_a_signing_key_of_31_characters_is_refused_by_naming_the_variable() -> None:
    key = "short-key-" + "k" * 21
    assert len(key) == 31

    with pytest.raises(ValueError) as caught:
        Settings.from_env({**LOGIN_ENV, KEY_VAR: key})

    assert KEY_VAR in str(caught.value)
    assert key not in str(caught.value)


# --- Base URL -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://boe.example.org", "https://boe.example.org"),
        ("https://boe.example.org/", "https://boe.example.org"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("https://boe.example.org:8443/", "https://boe.example.org:8443"),
    ],
)
def test_a_valid_base_url_is_kept_without_its_trailing_slash(given: str, kept: str) -> None:
    assert Settings.from_env({**LOGIN_ENV, URL_VAR: given}).base_url == kept


@pytest.mark.parametrize(
    "given",
    [
        "boe.example.org",
        "ftp://boe.example.org",
        "ws://boe.example.org",
        "file:///etc/passwd",
        "https://",
        "https:///only/a/path",
        "http://",
        "https://boe.example.org/?a=1",
        "https://boe.example.org/#section",
        "https://boe.example.org/path?x=1#y",
        "//boe.example.org",
        "not a url",
    ],
)
def test_an_invalid_base_url_is_refused_by_naming_the_variable(given: str) -> None:
    with pytest.raises(ValueError, match=URL_VAR):
        Settings.from_env({**LOGIN_ENV, URL_VAR: given})


# --- Allowed users ------------------------------------------------------------------------------


def test_allowed_users_default_to_nobody() -> None:
    assert Settings.from_env({}).allowed_users == ()
    assert Settings.from_env({USERS_VAR: ""}).allowed_users == ()
    assert Settings.from_env({USERS_VAR: " , ,, "}).allowed_users == ()


def test_allowed_users_are_split_on_commas_trimmed_and_blanks_dropped() -> None:
    settings = Settings.from_env({USERS_VAR: " alice , bob,, id:12 ,*, "})

    assert settings.allowed_users == ("alice", "bob", "id:12", "*")


@pytest.mark.parametrize(
    "bad",
    ["al_ice", "al ice", "alice!", "@alice", "id:", "id:abc", "id:1.5", "**", "a--b", "x" * 40],
)
def test_a_bad_allowed_user_is_refused_naming_the_variable_and_the_entry(bad: str) -> None:
    with pytest.raises(ValueError) as caught:
        Settings.from_env({USERS_VAR: f"alice,{bad},bob"})

    assert USERS_VAR in str(caught.value)
    assert bad in str(caught.value)


def test_a_bad_allowed_user_is_refused_even_when_login_is_off() -> None:
    # A typo found at startup, not when login is switched on later.
    with pytest.raises(ValueError, match=USERS_VAR):
        Settings.from_env({USERS_VAR: "al_ice"})


# --- Secrets stay out of repr and errors --------------------------------------------------------


def test_the_secrets_are_absent_from_the_repr() -> None:
    settings = Settings.from_env(LOGIN_ENV)

    for text in (repr(settings), str(settings), f"{settings!r}"):
        for secret in SECRETS:
            assert secret not in text


def test_the_secrets_are_absent_from_the_repr_of_a_directly_built_settings() -> None:
    settings = Settings(
        transport="http",
        github_client_id=CLIENT_ID,
        github_client_secret=CLIENT_SECRET,
        auth_signing_key=SIGNING_KEY,
        base_url=BASE_URL,
    )

    for secret in SECRETS:
        assert secret not in repr(settings)


@pytest.mark.parametrize(
    "env",
    [
        {**_without(KEY_VAR), URL_VAR: "ftp://x"},
        {**LOGIN_ENV, URL_VAR: "ftp://boe.example.org"},
        {**LOGIN_ENV, USERS_VAR: "al_ice"},
        {**LOGIN_ENV, KEY_VAR: "q9z-weak-77"},
        {SECRET_VAR: CLIENT_SECRET},
        {KEY_VAR: SIGNING_KEY},
        {**LOGIN_ENV, "BOE_INSIGHTS_PORT": "nope"},
    ],
    ids=["bad-url", "bad-url-full", "bad-user", "short-key", "secret-only", "key-only", "port"],
)
def test_no_error_text_contains_a_secret(env: dict[str, str]) -> None:
    with pytest.raises(ValueError) as caught:
        Settings.from_env(env)

    for secret in (*SECRETS, env.get(KEY_VAR, "")):
        if secret:
            assert secret not in str(caught.value)
