"""Tests for ``boe_insights_mcp.settings``."""

from __future__ import annotations

from pathlib import Path

import pytest

from boe_insights_mcp.cache import default_cache_dir
from boe_insights_mcp.settings import Settings, default_data_dir


def test_defaults_without_environment() -> None:
    settings = Settings.from_env({})

    assert settings.cache_backend == "disk"
    assert settings.cache_dir == default_cache_dir()
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
        data_dir_set=True,
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

    assert settings.data_dir == default_data_dir({})
    assert settings.data_dir != settings.cache_dir
    assert settings.data_dir_set is False


def test_data_dir_variable_overrides_it(tmp_path: Path) -> None:
    settings = Settings.from_env({"BOE_INSIGHTS_DATA_DIR": str(tmp_path)})

    assert settings.data_dir == tmp_path
    assert settings.data_dir_set is True
    assert settings.saved_searches_path == tmp_path / "saved_searches.sqlite3"


def test_empty_data_dir_variable_keeps_the_default() -> None:
    settings = Settings.from_env({"BOE_INSIGHTS_DATA_DIR": ""})

    assert settings.data_dir == default_data_dir({})
    assert settings.data_dir_set is False


def test_disk_backend_stores_searches_in_the_data_dir() -> None:
    settings = Settings.from_env({})

    assert settings.saved_searches_path == default_data_dir({}) / "saved_searches.sqlite3"


def test_default_data_dir_follows_the_environment_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOE_INSIGHTS_DATA_DIR", str(tmp_path))

    assert Settings().data_dir == tmp_path


def test_memory_backend_without_a_data_dir_keeps_searches_in_memory() -> None:
    assert Settings.from_env({"BOE_INSIGHTS_CACHE": "memory"}).saved_searches_path is None


def test_memory_backend_with_a_data_dir_still_persists_searches(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"BOE_INSIGHTS_CACHE": "memory", "BOE_INSIGHTS_DATA_DIR": str(tmp_path)}
    )

    assert settings.saved_searches_path == tmp_path / "saved_searches.sqlite3"
