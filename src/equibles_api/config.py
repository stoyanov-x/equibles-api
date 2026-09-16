"""Environment-driven configuration.

No secret has a default. The database password is required, so a misconfigured
service fails to start rather than silently connecting as something else.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict


class ConfigError(ValueError):
    """Required configuration is missing or malformed."""


class ConnectKwargs(TypedDict):
    """Typed so ``psycopg.connect(**kwargs)`` is actually checked.

    A plain ``dict[str, object]`` type-erases the expansion and mypy (strict)
    rejects it -- which is the useful outcome, but the fix is a concrete shape,
    not a cast.
    """

    host: str
    port: int
    dbname: str
    user: str
    password: str


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one process."""

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    api_key: str | None
    host: str
    port: int
    #: Hard ceiling on rows any single panel request may ask for. A client cannot
    #: make the service materialise an unbounded export by passing a big `limit`.
    max_rows: int
    statement_timeout_ms: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        src = os.environ if env is None else env
        # Two accepted names for one value. ``EQUIBLES_RO_PASSWORD`` is what is set
        # in Coolify (naming the read-only role, which is the point), and
        # ``EQUIBLES_DB_PASSWORD`` is the neutral name used elsewhere. Accepting
        # both is deliberate: it keeps the deployment working whether the value is
        # injected by Coolify or passed directly.
        password = src.get("EQUIBLES_DB_PASSWORD") or src.get("EQUIBLES_RO_PASSWORD") or ""
        if not password:
            raise ConfigError(
                "no database password: set EQUIBLES_DB_PASSWORD "
                "(or EQUIBLES_RO_PASSWORD), using the read-only role"
            )
        return cls(
            db_host=src.get("EQUIBLES_DB_HOST", "db"),
            db_port=_int(src, "EQUIBLES_DB_PORT", 5432),
            db_name=src.get("EQUIBLES_DB_NAME", "equibles"),
            db_user=src.get("EQUIBLES_DB_USER", "meridian_ro"),
            db_password=password,
            # Empty/absent means "no auth": the service is expected to be reachable
            # only from the app network. Set it when that assumption stops holding.
            api_key=src.get("EQUIBLES_API_KEY") or None,
            host=src.get("EQUIBLES_API_HOST", "0.0.0.0"),
            port=_int(src, "EQUIBLES_API_PORT", 8080),
            max_rows=_int(src, "EQUIBLES_API_MAX_ROWS", 5000),
            statement_timeout_ms=_int(src, "EQUIBLES_API_STATEMENT_TIMEOUT_MS", 600_000),
        )

    def connect_kwargs(self) -> ConnectKwargs:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
        }
