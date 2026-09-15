"""Tests for configuration resolution."""

from __future__ import annotations

import pytest

from equibles_api.config import ConfigError, Settings


def test_password_is_required() -> None:
    """A service that boots without credentials answers /healthz and fails every
    real request, which is worse than not booting."""
    with pytest.raises(ConfigError, match="EQUIBLES_DB_PASSWORD"):
        Settings.from_env({})


def test_defaults_target_the_read_only_role() -> None:
    settings = Settings.from_env({"EQUIBLES_DB_PASSWORD": "secret"})
    assert settings.db_host == "db"
    assert settings.db_port == 5432
    assert settings.db_name == "equibles"
    assert settings.db_user == "meridian_ro", "must never default to the superuser"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080


def test_api_key_is_optional_and_empty_means_unset() -> None:
    assert Settings.from_env({"EQUIBLES_DB_PASSWORD": "s"}).api_key is None
    assert Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_KEY": ""}).api_key is None
    assert Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_KEY": "k"}).api_key == "k"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"EQUIBLES_DB_PORT": "abc"}, "EQUIBLES_DB_PORT must be an integer"),
        ({"EQUIBLES_API_PORT": "0"}, "EQUIBLES_API_PORT must be > 0"),
        # -1 parses as an integer and is then rejected by the positivity check, so
        # the message names the range rather than the type.
        ({"EQUIBLES_API_MAX_ROWS": "-1"}, "EQUIBLES_API_MAX_ROWS must be > 0"),
    ],
)
def test_malformed_numbers_fail_at_boot(env: dict[str, str], expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        Settings.from_env({"EQUIBLES_DB_PASSWORD": "s", **env})


def test_connect_kwargs_never_include_the_api_key() -> None:
    settings = Settings.from_env(
        {"EQUIBLES_DB_PASSWORD": "s", "EQUIBLES_API_KEY": "k"}
    )
    assert set(settings.connect_kwargs()) == {"host", "port", "dbname", "user", "password"}
