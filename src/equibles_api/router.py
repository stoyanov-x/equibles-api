"""Routing and response shaping.

Deliberately socket-free: :func:`route` takes a query source and headers and returns
a :class:`Response`, so the whole surface is testable without binding a port or
standing up a database.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from . import sql
from .config import Settings
from .db import DatabaseError

DEFAULT_MIN_BARS = 1000
DEFAULT_LIMIT = 1500
DEFAULT_LOOKBACK_DAYS = 2200


class BadRequest(ValueError):
    """A caller-supplied parameter was missing or out of range."""


class QuerySource(Protocol):
    """The slice of :class:`~equibles_api.db.Database` the router needs."""

    def query_one(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def copy_csv(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> Iterable[bytes]: ...

    def healthy(self) -> bool: ...


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: Iterable[bytes]
    headers: Mapping[str, str] = field(default_factory=dict)


def _json(status: int, payload: Mapping[str, Any]) -> Response:
    # default=str so a `date` from the coverage query serialises instead of raising
    # inside the response path.
    body = (json.dumps(payload, default=str, sort_keys=True) + "\n").encode("utf-8")
    return Response(status, "application/json", [body])


def authorized(headers: Mapping[str, str], settings: Settings) -> bool:
    """Check the bearer token, if one is configured.

    ``headers`` keys must be lowercased by the caller. With no key configured this
    is open by design -- the service is meant to be reachable only on the app
    network, and the key exists for when that stops being true.
    """
    if settings.api_key is None:
        return True
    presented = ""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        presented = auth[7:].strip()
    if not presented:
        presented = headers.get("x-api-key", "").strip()
    return hmac.compare_digest(presented, settings.api_key)


def _int_param(
    query: Mapping[str, list[str]], name: str, default: int, minimum: int, maximum: int
) -> int:
    values = query.get(name)
    if not values or not values[0].strip():
        return default
    raw = values[0].strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise BadRequest(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise BadRequest(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _resolve_since(query: Mapping[str, list[str]]) -> date:
    raw = (query.get("since") or [""])[0].strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise BadRequest(f"since must be an ISO date like 2020-01-02, got {raw!r}") from exc
    lookback = _int_param(query, "lookback_days", DEFAULT_LOOKBACK_DAYS, 1, 20_000)
    return date.today() - timedelta(days=lookback)


def route(
    *,
    method: str,
    path: str,
    query: Mapping[str, list[str]],
    headers: Mapping[str, str],
    db: QuerySource,
    settings: Settings,
) -> Response:
    """Map a request to a response. Never raises for bad input."""
    # /healthz is intentionally unauthenticated so container healthchecks and the
    # Coolify probe need no key. It reveals only whether the database answers, to a
    # caller that already has to be on the app network to reach it.
    if path == "/healthz":
        return _json(200, {"ok": db.healthy()})
    if not authorized(headers, settings):
        return _json(401, {"error": "unauthorized"})
    if method not in ("GET", "HEAD"):
        return _json(405, {"error": f"method {method} is not allowed"})
    try:
        return _dispatch(path=path, query=query, db=db, settings=settings)
    except BadRequest as exc:
        return _json(400, {"error": str(exc)})
    except DatabaseError as exc:
        # 503, not 500: the caller's request was fine, the dependency is not.
        return _json(503, {"error": str(exc)})


def _dispatch(
    *, path: str, query: Mapping[str, list[str]], db: QuerySource, settings: Settings
) -> Response:
    if path == "/v1/coverage":
        since = _resolve_since(query)
        row = db.query_one(sql.COVERAGE_SQL, {"since": since})
        return _json(200, {"since": since, **row})

    if path == "/v1/panel.csv":
        since = _resolve_since(query)
        min_bars = _int_param(query, "min_bars", DEFAULT_MIN_BARS, 1, 100_000)
        limit = _int_param(query, "limit", DEFAULT_LIMIT, 1, settings.max_rows)
        params = {"since": since, "min_bars": min_bars, "limit": limit}
        return Response(
            200,
            "text/csv; charset=utf-8",
            db.copy_csv(sql.PANEL_SQL, params),
            {"X-Panel-Since": since.isoformat(), "X-Panel-Limit": str(limit)},
        )

    if path == "/v1/holdings/summary":
        return _json(200, db.query_one(sql.HOLDINGS_SQL))

    return _json(404, {"error": f"no route for {path}"})
