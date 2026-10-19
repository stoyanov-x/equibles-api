"""Schema drift detection: does the database still have what our SQL names?

Equibles' schema moves under this service. The 2026-10 equity-identity cutover
renamed ``"ListedDailyStockPrice"`` to ``"EquityDailyStockPrice"`` and its
``"ListedTicker"`` column to ``"SourceTicker"``; every price route then failed
with ``relation "ListedDailyStockPrice" does not exist``. Nobody noticed for
weeks because ``/healthz`` reported ``ok:false`` for a *different* reason (the
service could not reach the database at all after the host move), so the one
signal that existed was masked by another failure.

**What this checks, and what it does not.** Every double-quoted identifier in our
own SQL must exist as a relation or as a column. That is one catalogue query, it
touches no data, and it catches precisely the rename class of drift -- the one
that actually happened. It cannot catch a type change, a changed join key, or a
column that still exists but now means something else. Those need the row-level
checks a real integration test would do, which is a different (and much slower)
thing.

The point is that drift becomes a **boot failure**, not a 503 on the first query
a consumer happens to run. A crash-looping container is impossible to miss;
an `ok:false` in a JSON body is not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from . import datasets, sql
from .db import Database, DatabaseError

#: A double-quoted identifier, which in this codebase is always a table or column
#: name (the schema is PascalCase, so unquoted identifiers would be folded).
_IDENT = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')

#: Output aliases (`AS "ListedTicker"`) are names we *create*, not names we need,
#: so they must be stripped before the identifier sweep -- otherwise the panel's
#: deliberately-stable CSV header reads as a missing table.
_ALIAS = re.compile(r'\bAS\s+"[^"]+"', re.IGNORECASE)

#: One row: every relation and every column name the database currently has.
_CATALOGUE_SQL = """
SELECT
  (SELECT coalesce(json_agg(tablename), '[]'::json)
     FROM pg_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')) AS relations,
  (SELECT coalesce(json_agg(DISTINCT column_name), '[]'::json)
     FROM information_schema.columns) AS columns
"""


def queries() -> Mapping[str, str]:
    """Every SQL statement this service can run, keyed by a readable name."""
    known: dict[str, str] = {
        "panel": sql.PANEL_SQL,
        "coverage": sql.COVERAGE_SQL,
        "holdings": sql.HOLDINGS_SQL,
    }
    for dataset in datasets.DATASETS:
        known[f"dataset:{dataset.name}"] = sql.dataset_export_sql(dataset)
    return known


def identifiers(statement: str) -> set[str]:
    """The table and column names one statement depends on."""
    return set(_IDENT.findall(_ALIAS.sub("", statement)))


def probe(db: Database) -> list[str]:
    """Return one message per problem. An empty list means the schema matches.

    Raises :class:`~equibles_api.db.DatabaseError` if the catalogue itself cannot
    be read -- an unreachable database is not drift, and the caller decides what
    that means.
    """
    row = db.query_one(_CATALOGUE_SQL)
    relations = set(row.get("relations") or ())
    columns = set(row.get("columns") or ())
    known = relations | columns

    problems: list[str] = []
    for name, statement in queries().items():
        missing = sorted(identifiers(statement) - known)
        if missing:
            problems.append(f"{name}: missing {', '.join(missing)}")
    return problems


def describe(db: Database) -> str:
    """A one-line summary, for logs and for a healthcheck body."""
    try:
        problems = probe(db)
    except DatabaseError as exc:
        return f"schema unknown ({exc})"
    return "ok" if not problems else "; ".join(problems)
