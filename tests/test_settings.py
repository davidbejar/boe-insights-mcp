"""Tests for ``boe_insights_mcp.settings``."""

from __future__ import annotations

from pathlib import Path

import pytest

from boe_insights_mcp.cache import default_cache_dir
from boe_insights_mcp.settings import Settings, platform_data_dir


def test_defaults_without_environment() -> None:
    settings = Settings.from_env({})

    assert settings.cache_backend == "disk"
    # The empty mapping given to from_env, not the process environment, decides the default.
    assert settings.cache_dir == default_cache_dir({})
    assert settings.min_interval == 1.0
    assert settings.max_concurrency == 2


def test_variables_override_every_setting(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "BOE_INSIGHTS_CACHE": "memory",
            "BOE_INSIGHTS_CACHE_DIR": str(tmp_path),
            "BOE_INSIGHTS_DATA_DIR": str(tmp_path / "data"),
            "BOE_INSIGHTS_MIN_INTERVAL": "0.25",
            "BOE_INSIGHTS_MAX_CONCURRENCY": "4",
        }
    )

    assert settings == Settings(
        cache_backend="memory",
        cache_dir=tmp_path,
        data_dir=tmp_path / "data",
        min_interval=0.25,
        max_concurrency=4,
    )


def test_empty_variables_keep_the_defaults() -> None:
    env = {
        "BOE_INSIGHTS_CACHE": "",
        "BOE_INSIGHTS_CACHE_DIR": "",
        "BOE_INSIGHTS_MIN_INTERVAL": "",
        "BOE_INSIGHTS_MAX_CONCURRENCY": "",
    }

    assert Settings.from_env(env) == Settings.from_env({})


def test_backend_is_case_insensitive() -> None:
    assert Settings.from_env({"BOE_INSIGHTS_CACHE": " Memory "}).cache_backend == "memory"


def test_zero_interval_is_allowed() -> None:
    assert Settings.from_env({"BOE_INSIGHTS_MIN_INTERVAL": "0"}).min_interval == 0.0


def test_default_cache_dir_follows_the_environment_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOE_INSIGHTS_CACHE_DIR", str(tmp_path))

    assert Settings().cache_dir == tmp_path


def test_from_env_with_a_mapping_ignores_the_process_environment_for_the_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOE_INSIGHTS_CACHE_DIR", str(tmp_path / "from-process-env"))

    settings = Settings.from_env({})

    assert settings.cache_dir == default_cache_dir({})
    assert "from-process-env" not in str(settings.cache_dir)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BOE_INSIGHTS_CACHE", "redis"),
        ("BOE_INSIGHTS_MIN_INTERVAL", "soon"),
        ("BOE_INSIGHTS_MIN_INTERVAL", "-1"),
        ("BOE_INSIGHTS_MIN_INTERVAL", "nan"),
        ("BOE_INSIGHTS_MIN_INTERVAL", "inf"),
        ("BOE_INSIGHTS_MAX_CONCURRENCY", "two"),
        ("BOE_INSIGHTS_MAX_CONCURRENCY", "0"),
        ("BOE_INSIGHTS_MAX_CONCURRENCY", "1.5"),
    ],
)
def test_invalid_value_raises_naming_the_variable(variable: str, value: str) -> None:
    with pytest.raises(ValueError, match=variable):
        Settings.from_env({variable: value})


def test_settings_are_immutable() -> None:
    with pytest.raises(AttributeError):
        Settings().min_interval = 5.0  # type: ignore[misc]


def test_data_dir_defaults_to_the_platform_data_dir() -> None:
    settings = Settings.from_env({})

    assert settings.data_dir is None
    assert settings.saved_searches_dir == platform_data_dir()
    assert settings.saved_searches_dir != settings.cache_dir


def test_data_dir_variable_overrides_it(tmp_path: Path) -> None:
    settings = Settings.from_env({"BOE_INSIGHTS_DATA_DIR": str(tmp_path)})

    assert settings.data_dir == tmp_path
    assert settings.saved_searches_dir == tmp_path


def test_empty_data_dir_variable_keeps_the_default() -> None:
    settings = Settings.from_env({"BOE_INSIGHTS_DATA_DIR": ""})

    assert settings.saved_searches_dir == platform_data_dir()


def test_disk_backend_stores_searches_in_the_data_dir() -> None:
    settings = Settings.from_env({})

    assert settings.saved_searches_dir == platform_data_dir()


def test_default_data_dir_follows_the_environment_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOE_INSIGHTS_DATA_DIR", str(tmp_path))

    assert Settings().saved_searches_dir == tmp_path


def test_memory_backend_without_a_data_dir_keeps_searches_in_memory() -> None:
    assert Settings.from_env({"BOE_INSIGHTS_CACHE": "memory"}).saved_searches_dir is None


def test_memory_backend_with_a_data_dir_still_persists_searches(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"BOE_INSIGHTS_CACHE": "memory", "BOE_INSIGHTS_DATA_DIR": str(tmp_path)}
    )

    assert settings.saved_searches_dir == tmp_path


def test_a_settings_built_directly_and_from_env_agree_about_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BOE_INSIGHTS_DATA_DIR", raising=False)
    assert Settings().data_dir is None
    assert Settings.from_env().data_dir is None

    monkeypatch.setenv("BOE_INSIGHTS_DATA_DIR", str(tmp_path))
    assert Settings().data_dir is not None
    assert Settings().data_dir == Settings.from_env().data_dir == tmp_path


def test_an_explicit_data_dir_is_set_whatever_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BOE_INSIGHTS_DATA_DIR", raising=False)

    settings = Settings(cache_backend="memory", data_dir=tmp_path)

    assert settings.saved_searches_dir == tmp_path


def test_the_memory_backend_without_any_data_dir_keeps_searches_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOE_INSIGHTS_DATA_DIR", raising=False)

    assert Settings(cache_backend="memory").saved_searches_dir is None


# --- Transport settings -----------------------------------------------------------------------


def test_transport_defaults_are_stdio_on_loopback() -> None:
    settings = Settings.from_env({})

    assert settings.transport == "stdio"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.allowed_hosts == ()
    assert settings.allowed_origins == ()


def test_transport_variables_are_read() -> None:
    settings = Settings.from_env(
        {
            "BOE_INSIGHTS_TRANSPORT": " HTTP ",
            "BOE_INSIGHTS_HOST": "0.0.0.0",
            "BOE_INSIGHTS_PORT": "9001",
            "BOE_INSIGHTS_ALLOWED_HOSTS": "boe.example.org, ,other.example.org,",
            "BOE_INSIGHTS_ALLOWED_ORIGINS": "https://boe.example.org",
        }
    )

    assert settings.transport == "http"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9001
    assert settings.allowed_hosts == ("boe.example.org", "other.example.org")
    assert settings.allowed_origins == ("https://boe.example.org",)


def test_empty_transport_variables_keep_the_defaults() -> None:
    env = {
        "BOE_INSIGHTS_TRANSPORT": "",
        "BOE_INSIGHTS_HOST": "",
        "BOE_INSIGHTS_PORT": "",
        "BOE_INSIGHTS_ALLOWED_HOSTS": "",
        "BOE_INSIGHTS_ALLOWED_ORIGINS": " , ",
    }

    assert Settings.from_env(env) == Settings.from_env({})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BOE_INSIGHTS_TRANSPORT", "sse"),
        ("BOE_INSIGHTS_PORT", "0"),
        ("BOE_INSIGHTS_PORT", "65536"),
        ("BOE_INSIGHTS_PORT", "http"),
        ("BOE_INSIGHTS_PORT", "80.5"),
    ],
)
def test_invalid_transport_values_name_the_variable(variable: str, value: str) -> None:
    with pytest.raises(ValueError, match=variable):
        Settings.from_env({variable: value})


def test_port_bounds_are_accepted() -> None:
    assert Settings.from_env({"BOE_INSIGHTS_PORT": "1"}).port == 1
    assert Settings.from_env({"BOE_INSIGHTS_PORT": "65535"}).port == 65535
