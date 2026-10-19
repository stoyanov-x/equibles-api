"""Entry point: ``python -m equibles_api``."""

from __future__ import annotations

import logging
import os
import sys

from . import schema
from .app import serve
from .config import ConfigError, Settings
from .db import Database, DatabaseError

logger = logging.getLogger("equibles_api")


def _schema_check_disabled() -> bool:
    """Escape hatch for a shell that has no database (docs work, ad-hoc queries).

    Deliberately not a default: a service that starts against a database it cannot
    query answers ``/healthz`` and fails every real request, which is the failure
    this check exists to make impossible.
    """
    return os.environ.get("EQUIBLES_API_SKIP_SCHEMA_CHECK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # Fail loudly at boot: a service that starts with no database password
        # would answer /healthz and fail every real request.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    db = Database(settings)
    if not _schema_check_disabled():
        # Boot-time drift check. Equibles renames things (the 2026-10 identity
        # cutover renamed "ListedDailyStockPrice" and its "ListedTicker" column),
        # and a renamed table turns every price route into a 503 that reads like a
        # data problem. Refusing to start makes it a failed deploy instead: a
        # crash-looping container cannot be missed, an `ok:false` in a JSON body can.
        try:
            problems = schema.probe(db)
        except DatabaseError as exc:
            problems = [f"database unreachable: {exc}"]
        if problems:
            for problem in problems:
                logger.error("schema drift: %s", problem)
            logger.error(
                "refusing to start: %d of %d statements reference something the "
                "database does not have. Fix the SQL, or set "
                "EQUIBLES_API_SKIP_SCHEMA_CHECK=1 to start anyway.",
                len(problems),
                len(schema.queries()),
            )
            return 3
        logger.info("schema ok: %d statements verified", len(schema.queries()))

    serve(settings, db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
