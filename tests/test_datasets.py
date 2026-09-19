"""Tests for the dataset catalogue and the CSV exports it advertises.

The point of these is that the export path is the one place caller-controlled text
could reach a SQL string, so several of them assert on *absence*: that a ticker, a
bound or a dataset name never appears in the query text, only in the bound
parameters.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from equibles_api import datasets, sql
from equibles_api.config import Settings
from equibles_api.router import API_PATHS, MAX_DATE, MAX_TICKERS, Response, route


class RecordingDB:
    """Records what the router asked for. Never touches a database."""

    def __init__(self, *, row: Mapping[str, Any] | None = None) -> None:
        self.row = dict(row or {})
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def query_one(
        self, sql_text: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("query_one", sql_text, params))
        return dict(self.row)

    def copy_csv(
        self, sql_text: str, params: Mapping[str, Any] | None = None
    ) -> Iterable[bytes]:
        self.calls.append(("copy_csv", sql_text, params))
        return iter([b"Date,Ticker\n2026-01-02,AAPL\n"])

    def healthy(self) -> bool:
        return True


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(
        {"EQUIBLES_DB_PASSWORD": "secret", "EQUIBLES_API_MAX_ROWS": "5000"}
    )


def call(
    db: RecordingDB,
    settings: Settings,
    *,
    path: str,
    query: Mapping[str, list[str]] | None = None,
) -> Response:
    return route(
        method="GET",
        path=path,
        query=dict(query or {}),
        headers={},
        db=db,
        settings=settings,
    )


def payload(resp: Response) -> dict[str, Any]:
    return json.loads(b"".join(resp.body))


# -- catalogue ------------------------------------------------------------------


def test_catalogue_serves_from_the_registry_without_touching_the_database(
    settings: Settings,
) -> None:
    """A consumer must be able to discover the surface even when the database is down."""
    db = RecordingDB()
    resp = call(db, settings, path="/v1/catalogue")

    assert resp.status == 200
    assert db.calls == [], "the default catalogue call must not query"


def test_catalogue_lists_every_dataset_with_a_routable_path(settings: Settings) -> None:
    resp = call(RecordingDB(), settings, path="/v1/catalogue")
    entries = payload(resp)["datasets"]

    assert {e["name"] for e in entries} == {d.name for d in datasets.DATASETS}
    for entry in entries:
        assert entry["path"] == f"/v1/{entry['name']}.csv"
        assert entry["path"] in API_PATHS, "advertised but not routable"
        assert entry["columns"], "a dataset with no columns is not exportable"
        assert entry["date_column"]


def test_catalogue_coverage_costs_exactly_one_query_per_dataset(
    settings: Settings,
) -> None:
    db = RecordingDB(row={"tickers": 3, "first_date": "2020-01-02", "last_date": "2026-09-18"})
    resp = call(db, settings, path="/v1/catalogue", query={"coverage": ["1"]})

    assert resp.status == 200
    assert len(db.calls) == len(datasets.DATASETS)
    assert all(kind == "query_one" for kind, _, _ in db.calls)
    entry = payload(resp)["datasets"][0]
    assert entry["coverage"]["first_date"] == "2020-01-02"


def test_catalogue_rejects_a_non_boolean_coverage(settings: Settings) -> None:
    resp = call(RecordingDB(), settings, path="/v1/catalogue", query={"coverage": ["maybe"]})
    assert resp.status == 400


# -- export routing -------------------------------------------------------------


def test_every_dataset_is_routable(settings: Settings) -> None:
    for dataset in datasets.DATASETS:
        resp = call(RecordingDB(), settings, path=f"/v1/{dataset.name}.csv")
        assert resp.status == 200, dataset.name
        assert resp.content_type.startswith("text/csv")
        assert resp.headers["X-Dataset"] == dataset.name


def test_an_unknown_dataset_is_a_404_and_runs_no_query(settings: Settings) -> None:
    db = RecordingDB()
    resp = call(db, settings, path="/v1/not-a-dataset.csv")

    assert resp.status == 404
    assert db.calls == [], "an unknown name must not reach the database"


def test_a_dataset_name_can_never_reach_the_sql(settings: Settings) -> None:
    """The name is only ever a dictionary key. A miss is a 404, not a query."""
    db = RecordingDB()
    hostile = "short-volume;DROP TABLE"
    resp = call(db, settings, path=f"/v1/{hostile}.csv")

    assert resp.status == 404
    assert db.calls == []
    # The name is echoed back as a JSON error message and nothing else.
    assert "unknown dataset" in payload(resp)["error"]


# -- caller values are bound, never interpolated --------------------------------


def test_the_export_binds_every_caller_supplied_value(settings: Settings) -> None:
    db = RecordingDB()
    resp = call(
        db,
        settings,
        path="/v1/short-volume.csv",
        query={
            "since": ["2021-01-04"],
            "until": ["2022-01-04"],
            "tickers": ["AAPL,MSFT"],
            "limit": ["10"],
        },
    )

    assert resp.status == 200
    kind, text, params = db.calls[0]
    assert kind == "copy_csv"
    assert params is not None
    assert params["tickers"] == ("AAPL", "MSFT")
    assert params["limit"] == 10
    assert params["since"].isoformat() == "2021-01-04"
    assert params["until"].isoformat() == "2022-01-04"
    # The caller's values appear ONLY as placeholders.
    assert "AAPL" not in text
    assert "MSFT" not in text
    assert "2021-01-04" not in text
    assert "2022-01-04" not in text
    for placeholder in ("%(since)s", "%(until)s", "%(tickers)s", "%(limit)s"):
        assert placeholder in text


def test_the_export_is_bounded(settings: Settings) -> None:
    """These tables hold millions of rows; an unbounded COPY holds a connection."""
    for dataset in datasets.DATASETS:
        assert "LIMIT %(limit)s" in sql.dataset_export_sql(dataset)


# -- parameter validation -------------------------------------------------------


def test_tickers_are_split_trimmed_and_preserved(settings: Settings) -> None:
    db = RecordingDB()
    call(
        db,
        settings,
        path="/v1/dividends.csv",
        query={"tickers": [" AAPL , BRK.B ,^VIX "]},
    )
    assert db.calls[0][2]["tickers"] == ("AAPL", "BRK.B", "^VIX")


def test_an_empty_tickers_filter_means_every_ticker(settings: Settings) -> None:
    """None is bound as NULL, so the clause short-circuits instead of matching nothing."""
    db = RecordingDB()
    call(db, settings, path="/v1/dividends.csv", query={"tickers": ["", "  "]})
    assert db.calls[0][2]["tickers"] is None


@pytest.mark.parametrize("bad", ["AAPL;DROP", "AAPL MSFT", "AAPL'", 'AAPL"', "a,b;c"])
def test_a_malformed_ticker_is_rejected(settings: Settings, bad: str) -> None:
    resp = call(RecordingDB(), settings, path="/v1/dividends.csv", query={"tickers": [bad]})
    assert resp.status == 400


def test_too_many_tickers_is_rejected(settings: Settings) -> None:
    many = ",".join(f"T{i}" for i in range(MAX_TICKERS + 1))
    resp = call(RecordingDB(), settings, path="/v1/dividends.csv", query={"tickers": [many]})
    assert resp.status == 400


def test_until_before_since_is_rejected(settings: Settings) -> None:
    resp = call(
        RecordingDB(),
        settings,
        path="/v1/dividends.csv",
        query={"since": ["2024-01-01"], "until": ["2020-01-01"]},
    )
    assert resp.status == 400


def test_limit_is_capped_by_the_configured_ceiling(settings: Settings) -> None:
    resp = call(
        RecordingDB(), settings, path="/v1/dividends.csv", query={"limit": ["5001"]}
    )
    assert resp.status == 400


def test_an_omitted_until_has_no_upper_bound(settings: Settings) -> None:
    """The sentinel keeps one query shape rather than two."""
    db = RecordingDB()
    resp = call(db, settings, path="/v1/dividends.csv")

    assert resp.status == 200
    assert db.calls[0][2]["until"] == MAX_DATE
    assert resp.headers["X-Dataset-Until"] == ""


# -- the registry itself --------------------------------------------------------


def test_every_dataset_resolves_a_ticker_expression(settings: Settings) -> None:
    """A dataset that forgets its join would silently filter on nothing."""
    for dataset in datasets.DATASETS:
        text = sql.dataset_export_sql(dataset)
        assert dataset.ticker in text, dataset.name
        assert f"FROM {dataset.table} p" in text, dataset.name
        # The filter is on the declared ticker, not on a hardcoded column.
        assert f"{dataset.ticker} = ANY(%(tickers)s::text[])" in text, dataset.name


def test_joins_are_scoped_to_tables_that_need_them() -> None:
    """Denormalised tickers need no join; normalised ones must declare one."""
    no_join = {"short-volume", "fail-to-deliver", "splits"}
    for dataset in datasets.DATASETS:
        if dataset.name in no_join:
            assert dataset.joins == "", f"{dataset.name} should not join"
        else:
            assert dataset.joins, f"{dataset.name} resolves a ticker and needs a join"


def test_dataset_names_are_unique_and_filename_safe() -> None:
    names = [d.name for d in datasets.DATASETS]
    assert len(names) == len(set(names))
    for name in names:
        assert name.replace("-", "").isalnum(), name
        assert name.islower(), name
