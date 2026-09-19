"""Routing and response shaping.

Deliberately socket-free: :func:`route` takes a query source and headers and returns
a :class:`Response`, so the whole surface is testable without binding a port or
standing up a database.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from . import datasets, openapi, sql
from .config import Settings
from .db import DatabaseError

DEFAULT_MIN_BARS = 1000
DEFAULT_LIMIT = 1500
DEFAULT_LOOKBACK_DAYS = 2200
#: Row cap for a dataset export. Rows, not symbols -- unlike the panel, where
#: `limit` counts names. A dataset export is a time series per ticker, so the
#: same number means something very different in the two places.
DEFAULT_DATASET_LIMIT = 5000
#: Upper bound on a `tickers` filter, so one request cannot ask the database to
#: build an enormous `= ANY(...)` array.
MAX_TICKERS = 500
#: Deliberately permissive on shape and strict on rejection. Equibles' tickers are
#: exact, not case-folded, and include dotted (`BRK.B`), suffixed (`BARC.L`) and
#: caret-prefixed (`^VIX`) forms -- so this rejects whitespace, quotes, commas and
#: semicolons (the characters that matter for injection into a log or a filter)
#: without pretending to know every venue's grammar.
_TICKER_RE = re.compile(r"[A-Za-z0-9.^/_=-]{1,32}")

#: The documented API surface. The OpenAPI document must cover exactly these, and a
#: test asserts it -- a documented path that 404s is worse than no documentation.
#:
#: The dataset paths are generated from the registry rather than listed, so adding
#: a dataset cannot leave the documentation behind: `API_PATHS` grows with it and
#: the OpenAPI check fails until the document covers it.
API_PATHS: tuple[str, ...] = (
    "/healthz",
    "/v1/catalogue",
    "/v1/coverage",
    "/v1/holdings/summary",
    "/v1/panel.csv",
    *(f"/v1/{dataset.name}.csv" for dataset in datasets.DATASETS),
)

#: Reachable without a credential. ``/healthz`` so container healthchecks work;
#: the docs so the API can be inspected before anyone holds the key.
PUBLIC_PATHS: tuple[str, ...] = ("/", "/docs", "/openapi.json", "/healthz")


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


#: Stands in for "no upper bound". A sentinel rather than a NULL keeps the export
#: SQL uniform -- `date <= %(until)s` holds whether or not the caller supplied one
#: -- so there is no second query shape to keep correct.
MAX_DATE = date(9999, 12, 31)


def _resolve_until(query: Mapping[str, list[str]]) -> date:
    raw = (query.get("until") or [""])[0].strip()
    if not raw:
        return MAX_DATE
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BadRequest(f"until must be an ISO date like 2026-01-02, got {raw!r}") from exc


def _bool_param(query: Mapping[str, list[str]], name: str, default: bool) -> bool:
    values = query.get(name)
    if not values or not values[0].strip():
        return default
    raw = values[0].strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise BadRequest(f"{name} must be a boolean, got {raw!r}")


def _ticker_param(query: Mapping[str, list[str]]) -> tuple[str, ...] | None:
    """A comma-separated ticker filter, or None meaning "every ticker".

    None is bound as SQL NULL and the clause short-circuits, so an unfiltered
    export does not build a huge array of every ticker in the table.
    """
    values = query.get("tickers")
    if not values:
        return None
    tickers = tuple(
        part.strip() for value in values for part in value.split(",") if part.strip()
    )
    if not tickers:
        return None
    if len(tickers) > MAX_TICKERS:
        raise BadRequest(f"tickers is capped at {MAX_TICKERS} symbols, got {len(tickers)}")
    for ticker in tickers:
        if not _TICKER_RE.fullmatch(ticker):
            raise BadRequest(f"tickers contains an invalid symbol {ticker!r}")
    return tickers


def _with_headers(response: Response, extra: Mapping[str, str]) -> Response:
    """Copy ``response`` with ``extra`` headers merged in (or return it unchanged)."""
    if not extra:
        return response
    return Response(
        response.status,
        response.content_type,
        response.body,
        {**response.headers, **extra},
    )


def cors_headers(origin: str, settings: Settings) -> dict[str, str]:
    """CORS headers for a browser request, when the origin is allowed.

    Empty allowlist means no CORS headers at all -- the default, because this
    service is built for server-to-server use and enabling it for browsers should be
    a deliberate act rather than an accident.

    ``Vary: Origin`` is not optional: without it a shared cache can replay one
    origin's `Access-Control-Allow-Origin` to a different origin.
    """
    origin = origin.strip()
    if not origin or origin not in settings.allowed_origins:
        return {}
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}


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
    cors = cors_headers(headers.get("origin", ""), settings)

    if method == "OPTIONS":
        # Preflight, and deliberately incomplete: it advertises GET/HEAD and no
        # request headers. The browser probe we support is an unauthenticated GET of
        # /healthz, which needs no preflight at all; withholding
        # `Access-Control-Allow-Headers` is what keeps an API key from being sendable
        # from a page -- and a key inlined in a browser bundle is not a secret
        # anyway. So `/v1/*` stays unreachable from JavaScript on purpose.
        return _with_headers(
            Response(
                204,
                "text/plain; charset=utf-8",
                [b""],
                {"Access-Control-Allow-Methods": "GET, HEAD", "Access-Control-Max-Age": "600"},
            ),
            cors,
        )

    if path in PUBLIC_PATHS:
        return _with_headers(_public(path=path, db=db, settings=settings), cors)
    if not authorized(headers, settings):
        return _with_headers(_json(401, {"error": "unauthorized"}), cors)
    if method not in ("GET", "HEAD"):
        return _with_headers(_json(405, {"error": f"method {method} is not allowed"}), cors)
    try:
        return _with_headers(_dispatch(path=path, query=query, db=db, settings=settings), cors)
    except BadRequest as exc:
        return _with_headers(_json(400, {"error": str(exc)}), cors)
    except DatabaseError as exc:
        # 503, not 500: the caller's request was fine, the dependency is not.
        return _with_headers(_json(503, {"error": str(exc)}), cors)


def _public(*, path: str, db: QuerySource, settings: Settings) -> Response:
    """Routes that work without a key. See :data:`PUBLIC_PATHS`."""
    if path == "/":
        # A browser landing on the root should not meet a 401 and conclude the
        # service is broken.
        return Response(302, "text/plain; charset=utf-8", [b""], {"Location": "/docs"})
    if path == "/healthz":
        # 200 even when the database is down: a healthcheck that fails here is
        # indistinguishable from a dead process, which is the distinction it exists
        # to draw. `ok` carries the answer instead.
        return _json(200, {"ok": db.healthy()})
    if path == "/openapi.json":
        # Not routed through _json: that sorts keys, which reorders the paths into
        # alphabetical noise. Indented also keeps it diffable.
        body = (json.dumps(openapi.document(settings), indent=2) + "\n").encode("utf-8")
        return Response(200, "application/json", [body])
    if path == "/docs":
        return Response(
            200, "text/html; charset=utf-8", [openapi.DOCS_HTML.encode("utf-8")]
        )
    # Explicit rather than a catch-all: adding a name to PUBLIC_PATHS should not
    # silently start serving the docs page for it.
    return _json(404, {"error": f"no route for {path}"})


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

    if path == "/v1/catalogue":
        return _catalogue(query=query, db=db)

    # Placed after the explicit routes above so `/v1/panel.csv` -- which shares the
    # shape but has different `limit` semantics -- keeps its own handling.
    prefix, _, filename = path.rpartition("/")
    if prefix == "/v1" and filename.endswith(".csv"):
        return _dataset_csv(filename[: -len(".csv")], query=query, db=db, settings=settings)

    return _json(404, {"error": f"no route for {path}"})


def _catalogue(*, query: Mapping[str, list[str]], db: QuerySource) -> Response:
    """What is available, and optionally how far back it goes.

    Coverage is opt-in because it is one query per dataset. The default call is
    served from the registry with no database round trip at all, so a consumer can
    discover the surface even when the database is down.
    """
    with_coverage = _bool_param(query, "coverage", False)
    entries: list[dict[str, Any]] = []
    for dataset in datasets.DATASETS:
        entry: dict[str, Any] = {
            "name": dataset.name,
            "path": f"/v1/{dataset.name}.csv",
            "summary": dataset.summary,
            "description": dataset.description,
            "date_column": dataset.date_column,
            "columns": [column.name for column in dataset.columns],
        }
        if with_coverage:
            entry["coverage"] = db.query_one(sql.dataset_coverage_sql(dataset))
        entries.append(entry)
    return _json(200, {"datasets": entries})


def _dataset_csv(
    name: str,
    *,
    query: Mapping[str, list[str]],
    db: QuerySource,
    settings: Settings,
) -> Response:
    dataset = datasets.BY_NAME.get(name)
    if dataset is None:
        return _json(404, {"error": f"unknown dataset {name!r}"})
    since = _resolve_since(query)
    until = _resolve_until(query)
    if until < since:
        raise BadRequest(f"until {until} is before since {since}")
    tickers = _ticker_param(query)
    limit = _int_param(query, "limit", DEFAULT_DATASET_LIMIT, 1, settings.max_rows)
    response = Response(
        200,
        "text/csv; charset=utf-8",
        db.copy_csv(
            sql.dataset_export_sql(dataset),
            {"since": since, "until": until, "tickers": tickers, "limit": limit},
        ),
        {
            "X-Dataset": dataset.name,
            "X-Dataset-Since": since.isoformat(),
            "X-Dataset-Until": ("" if until == MAX_DATE else until.isoformat()),
            "X-Dataset-Limit": str(limit),
        },
    )
    return response
