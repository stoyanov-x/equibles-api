"""Database access: one shared connection for small queries, one per stream."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import Settings


class DatabaseError(RuntimeError):
    """The database is unreachable, or the query failed."""


class Database:
    """A lazily-opened connection, reopened after any failure.

    Small queries share one connection guarded by a lock -- a pool would be
    ceremony for a single consumer. Streaming (``copy_csv``) deliberately does NOT
    take that lock: a 50 MB export can occupy a connection for seconds, and holding
    the lock would block ``/healthz`` behind it, so a slow export would look like a
    dead service.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._conn: psycopg.Connection[Any] | None = None

    # -- internals ---------------------------------------------------------------
    def _open(self) -> psycopg.Connection[Any]:
        try:
            conn = psycopg.connect(**self._settings.connect_kwargs(), autocommit=True)
        except psycopg.Error as exc:
            raise DatabaseError(f"cannot connect to {self._settings.db_host}: {exc}") from exc
        # set_config rather than "SET statement_timeout = {}". SET does not accept
        # bind parameters, and building the string is exactly the mistake this
        # module avoids everywhere else.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(int(self._settings.statement_timeout_ms)),),
            )
        return conn

    def _reset(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None and not conn.closed:
            conn.close()

    def connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = self._open()
        return self._conn

    # -- api ---------------------------------------------------------------------
    def query_one(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run one query and return its single row as a dict."""
        with self._lock:
            try:
                with self.connection().cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    row: dict[str, Any] | None = cur.fetchone()
            except psycopg.Error as exc:
                self._reset()
                raise DatabaseError(str(exc)) from exc
        if row is None:
            raise DatabaseError("query returned no rows")
        return row

    def copy_csv(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> Iterator[bytes]:
        """Stream a ``COPY ... TO STDOUT`` query as raw CSV bytes.

        Note for callers: because this is a generator, a failure raised after the
        first chunk cannot be turned into an HTTP error status -- the response has
        already started. Consumers must treat a truncated stream as a failure.
        """
        conn = self._open()
        try:
            with conn.cursor() as cur, cur.copy(sql, params) as copy:
                for block in copy:
                    yield bytes(block)
        except psycopg.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            conn.close()

    def healthy(self) -> bool:
        """True when a trivial query succeeds. Never raises."""
        try:
            row = self.query_one("SELECT 1 AS ok")
        except DatabaseError:
            return False
        return row.get("ok") == 1
