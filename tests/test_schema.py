"""Tests for the schema drift probe.

The probe exists because a rename upstream (``"ListedDailyStockPrice"`` became
``"EquityDailyStockPrice"``) turned every price route into a 503 that looked like
a data problem. These tests pin the two things that make it useful: it finds the
identifiers we actually depend on, and it does not mistake the output aliases we
choose for something the database must have.
"""

from __future__ import annotations

from typing import Any

import pytest

from equibles_api import schema
from equibles_api.db import DatabaseError


class FakeCatalogue:
    """Stands in for Database: only query_one is used, and only for the catalogue."""

    def __init__(self, relations: list[str], columns: list[str]) -> None:
        self.relations = relations
        self.columns = columns
        self.statements: list[str] = []

    def query_one(
        self, sql: str, params: Any = None
    ) -> dict[str, Any]:
        self.statements.append(sql)
        return {"relations": self.relations, "columns": self.columns}


def test_identifiers_ignore_output_aliases() -> None:
    """`AS "ListedTicker"` is a name we create, not one we require.

    The panel deliberately aliases `"SourceTicker"` back to `"ListedTicker"` so the
    CSV header contract consumers parse does not change. Treating that alias as a
    required column would report drift on a database that is perfectly fine.
    """
    statement = 'SELECT p."SourceTicker" AS "ListedTicker" FROM "EquityDailyStockPrice" p'
    assert schema.identifiers(statement) == {"SourceTicker", "EquityDailyStockPrice"}


def test_probe_is_clean_when_every_identifier_exists() -> None:
    required: set[str] = set()
    for statement in schema.queries().values():
        required |= schema.identifiers(statement)
    catalogue = FakeCatalogue(relations=sorted(required), columns=sorted(required))
    assert schema.probe(catalogue) == []  # type: ignore[arg-type]


def test_probe_reports_a_renamed_table() -> None:
    """The exact regression: the price table no longer has the name we query.

    Simulated by a catalogue that knows nothing -- the new name is in our SQL, the
    database would only have the old one, and the panel statement has to say so.
    """
    catalogue = FakeCatalogue(relations=[], columns=[])
    problems = schema.probe(catalogue)  # type: ignore[arg-type]
    assert problems, "a missing relation must be reported"
    panel = [problem for problem in problems if problem.startswith("panel:")]
    assert panel, f"the panel statement must be called out, got {problems}"
    assert "EquityDailyStockPrice" in panel[0]


def test_probe_raises_when_the_catalogue_is_unreadable() -> None:
    class Broken:
        def query_one(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise DatabaseError("cannot connect")

    with pytest.raises(DatabaseError):
        schema.probe(Broken())  # type: ignore[arg-type]
