"""Tests for the routing surface.

These exercise :func:`route` directly with a fake query source, so the whole HTTP
surface is covered without a port or a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

import pytest

from equibles_api import sql
from equibles_api.config import Settings
from equibles_api.db import DatabaseError
from equibles_api.router import Response, route


class FakeDB:
    """Records what the router asked for, and returns whatever the test wants."""

    def __init__(
        self,
        *,
        row: Mapping[str, Any] | None = None,
        chunks: Iterable[bytes] = (),
        healthy: bool = True,
        error: str | None = None,
    ) -> None:
        self.row = dict(row or {})
        self.chunks = list(chunks)
        self._healthy = healthy
        self.error = error
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def query_one(
        self, sql_text: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("query_one", sql_text, params))
        if self.error:
            raise DatabaseError(self.error)
        return dict(self.row)

    def copy_csv(
        self, sql_text: str, params: Mapping[str, Any] | None = None
    ) -> Iterable[bytes]:
        self.calls.append(("copy_csv", sql_text, params))
        if self.error:
            raise DatabaseError(self.error)
        return iter(self.chunks)

    def healthy(self) -> bool:
        return self._healthy


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(
        {
            "EQUIBLES_DB_PASSWORD": "secret",
            "EQUIBLES_API_MAX_ROWS": "5000",
        }
    )


def body(resp: Response) -> bytes:
    return b"".join(resp.body)


def call(
    db: FakeDB,
    settings: Settings,
    *,
    path: str,
    method: str = "GET",
    query: Mapping[str, list[str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return route(
        method=method,
        path=path,
        query=dict(query or {}),
        headers=dict(headers or {}),
        db=db,
        settings=settings,
    )


# -- health ---------------------------------------------------------------------


def test_healthz_reports_database_reachability(settings: Settings) -> None:
    assert call(FakeDB(healthy=True), settings, path="/healthz").status == 200
    assert b'"ok": true' in body(call(FakeDB(healthy=True), settings, path="/healthz"))


def test_healthz_stays_200_when_the_database_is_down(settings: Settings) -> None:
    """A healthcheck that 500s when the dependency is down cannot be distinguished
    from a dead process, and both look the same to an orchestrator."""
    resp = call(FakeDB(healthy=False), settings, path="/healthz")
    assert resp.status == 200
    assert b'"ok": false' in body(resp)


def test_healthz_needs_no_api_key(settings: Settings) -> None:
    keyed = Settings.from_env(
        {"EQUIBLES_DB_PASSWORD": "secret", "EQUIBLES_API_KEY": "k"}
    )
    assert call(FakeDB(), keyed, path="/healthz").status == 200
    assert call(FakeDB(), keyed, path="/v1/coverage").status == 401


# -- auth -----------------------------------------------------------------------


def test_auth_rejects_wrong_or_missing_key(settings: Settings) -> None:
    keyed = Settings.from_env(
        {"EQUIBLES_DB_PASSWORD": "secret", "EQUIBLES_API_KEY": "k"}
    )
    assert call(FakeDB(), keyed, path="/v1/coverage", headers={}).status == 401
    assert (
        call(FakeDB(), keyed, path="/v1/coverage", headers={"authorization": "Bearer nope"}).status
        == 401
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer k"},
        {"x-api-key": "k"},
    ],
)
def test_auth_accepts_both_bearer_and_header(
    settings: Settings, headers: Mapping[str, str]
) -> None:
    keyed = Settings.from_env(
        {"EQUIBLES_DB_PASSWORD": "secret", "EQUIBLES_API_KEY": "k"}
    )
    resp = call(FakeDB(row={"rows": 1}), keyed, path="/v1/coverage", headers=headers)
    assert resp.status == 200


# -- routing --------------------------------------------------------------------


def test_unknown_route_is_404(settings: Settings) -> None:
    assert call(FakeDB(), settings, path="/v1/nope").status == 404


def test_non_get_methods_are_405(settings: Settings) -> None:
    resp = call(FakeDB(), settings, path="/v1/coverage", method="POST")
    assert resp.status == 405


def test_database_failure_is_503_not_500(settings: Settings) -> None:
    resp = call(FakeDB(error="connection refused"), settings, path="/v1/coverage")
    assert resp.status == 503
    assert b"connection refused" in body(resp)


# -- coverage -------------------------------------------------------------------


def test_coverage_returns_the_row_and_echoes_the_window(settings: Settings) -> None:
    db = FakeDB(row={"rows": 12, "symbols": 3, "dates": 4})
    resp = call(db, settings, path="/v1/coverage", query={"since": ["2020-01-02"]})
    assert resp.status == 200
    assert b'"rows": 12' in body(resp)
    assert b'"since": "2020-01-02"' in body(resp)
    assert db.calls[0][2] == {"since": date(2020, 1, 2)}


# -- panel ----------------------------------------------------------------------


def test_panel_streams_csv_with_the_documented_columns(settings: Settings) -> None:
    db = FakeDB(chunks=[b"Date,ListedTicker,AdjustedClose\n", b"2024-01-02,AAA,1.0\n"])
    resp = call(db, settings, path="/v1/panel.csv")
    assert resp.status == 200
    assert resp.content_type.startswith("text/csv")
    text = body(resp).decode()
    assert text.splitlines()[0] == "Date,ListedTicker,AdjustedClose"
    assert db.calls[0][1] == sql.PANEL_SQL


def test_panel_defaults_match_the_export_script(settings: Settings) -> None:
    """The API and scripts/equibles-price-export.sh must agree on what a panel is."""
    db = FakeDB()
    call(db, settings, path="/v1/panel.csv")
    _, _, params = db.calls[0]
    assert params is not None
    assert params["min_bars"] == 1000
    assert params["limit"] == 1500
    assert params["since"] == date.today() - timedelta(days=2200)


def test_panel_honours_explicit_params(settings: Settings) -> None:
    db = FakeDB()
    call(
        db,
        settings,
        path="/v1/panel.csv",
        query={"min_bars": ["250"], "limit": ["10"], "since": ["2021-06-01"]},
    )
    _, _, params = db.calls[0]
    assert params == {"since": date(2021, 6, 1), "min_bars": 250, "limit": 10}


def test_panel_echoes_the_window_in_headers(settings: Settings) -> None:
    resp = call(FakeDB(), settings, path="/v1/panel.csv", query={"since": ["2021-06-01"]})
    assert resp.headers["X-Panel-Since"] == "2021-06-01"


@pytest.mark.parametrize(
    "query",
    [
        {"min_bars": ["0"]},
        {"min_bars": ["-5"]},
        {"min_bars": ["lots"]},
        {"limit": ["0"]},
        {"lookback_days": ["0"]},
        {"since": ["yesterday"]},
        {"since": ["2021-13-45"]},
    ],
)
def test_bad_parameters_are_400_and_never_reach_the_database(
    settings: Settings, query: Mapping[str, list[str]]
) -> None:
    db = FakeDB()
    resp = call(db, settings, path="/v1/panel.csv", query=query)
    assert resp.status == 400
    assert db.calls == [], "an invalid request must not touch the database"


def test_limit_cannot_exceed_the_configured_ceiling(settings: Settings) -> None:
    """max_rows is what stops a caller materialising an unbounded export."""
    resp = call(FakeDB(), settings, path="/v1/panel.csv", query={"limit": ["5001"]})
    assert resp.status == 400
    assert b"5000" in body(resp)


def test_lookback_days_produces_a_concrete_date_not_a_sql_expression(
    settings: Settings,
) -> None:
    """The window is computed in Python, so the query stays fully parameterised."""
    db = FakeDB()
    call(db, settings, path="/v1/panel.csv", query={"lookback_days": ["100"]})
    _, _, params = db.calls[0]
    assert params is not None
    assert params["since"] == date.today() - timedelta(days=100)


# -- holdings -------------------------------------------------------------------


def test_holdings_summary_exposes_the_intermediate_counters(settings: Settings) -> None:
    """"Holdings are low" is unactionable without CUSIP coverage and data-set counts."""
    db = FakeDB(row={"holdings": 112, "securities_with_cusip": 284, "datasets": 3})
    resp = call(db, settings, path="/v1/holdings/summary")
    assert resp.status == 200
    payload = body(resp).decode()
    assert '"securities_with_cusip": 284' in payload
    assert '"datasets": 3' in payload


# -- sql ------------------------------------------------------------------------


def test_sql_uses_named_parameters_only() -> None:
    """No query may interpolate a caller value; every one is a named placeholder."""
    for statement in (sql.PANEL_SQL, sql.COVERAGE_SQL):
        assert "%(since)s" in statement
        assert "{" not in statement, f"f-string braces found in {statement[:40]!r}"
