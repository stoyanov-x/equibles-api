"""The OpenAPI document, and the page that renders it.

Hand-written, because the server is stdlib-only: there is no framework to
introspect, and pulling one in purely to generate a spec would cost more than the
spec is worth.

The obvious risk with a hand-written document is drift, so ``router.PATHS`` is the
single list of paths this service serves and a test asserts the document covers
*exactly* those. A documented endpoint that 404s is worse than no documentation.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .config import Settings

#: Pinned, not floating. A CDN URL with a major-only tag (`@5`) means the JavaScript
#: executing in an operator's browser can change without any commit here.
#: Verified to exist: unpkg returns 200 for both swagger-ui.css and
#: swagger-ui-bundle.js at this exact version. Check before bumping -- a typo'd or
#: unpublished version renders a blank page rather than failing loudly.
SWAGGER_UI_VERSION = "5.32.15"
_SWAGGER_CDN = f"https://unpkg.com/swagger-ui-dist@{SWAGGER_UI_VERSION}"

DOCS_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>equibles-api - API docs</title>
<link rel="stylesheet" href="{_SWAGGER_CDN}/swagger-ui.css">
</head>
<body style="margin:0">
<div id="swagger-ui"></div>
<script src="{_SWAGGER_CDN}/swagger-ui-bundle.js" crossorigin="anonymous"></script>
<script>
SwaggerUIBundle({{
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  // Keeps the pasted token across reloads, which matters because every /v1/*
  // route needs it and re-pasting it on every page refresh is how people end up
  // putting it in a query string.
  persistAuthorization: true,
  tryItOutEnabled: true,
}});
</script>
</body>
</html>
"""


def document(settings: Settings) -> dict[str, Any]:
    """Build the OpenAPI 3.1 document."""
    # Imported inside the function, not at module scope: `router` imports this
    # module, so a top-level import would be circular.
    from .router import DEFAULT_LIMIT, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_BARS
    lookback = {
        "name": "lookback_days",
        "in": "query",
        "required": False,
        "description": "Window length in calendar days. Ignored when `since` is given.",
        "schema": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20000,
            "default": DEFAULT_LOOKBACK_DAYS,
        },
    }
    since = {
        "name": "since",
        "in": "query",
        "required": False,
        "description": "Inclusive ISO date lower bound. Overrides `lookback_days`.",
        "schema": {"type": "string", "format": "date", "example": "2020-01-02"},
    }
    until = {
        "name": "until",
        "in": "query",
        "required": False,
        "description": "Inclusive ISO date upper bound. Omitted means no upper bound.",
        "schema": {"type": "string", "format": "date", "example": "2026-01-02"},
    }
    tickers = {
        "name": "tickers",
        "in": "query",
        "required": False,
        "description": (
            "Comma-separated symbols to restrict the export. Symbols are matched "
            "exactly and are NOT case-folded. Omitted means every ticker."
        ),
        "schema": {"type": "string", "example": "AAPL,MSFT"},
    }
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "equibles-api",
            "version": __version__,
            "description": (
                "Read-only HTTP access to the Equibles Postgres database.\n\n"
                "Every `/v1/*` route requires the API key. `/healthz`, `/docs` and "
                "`/openapi.json` are intentionally open so container healthchecks and "
                "this page work without it.\n\n"
                "**Note on the panel:** its calendar is a union of every symbol's "
                "dates, and the newest date typically belongs to a handful of early "
                "reporters. Ranking the last row is therefore not ranking the "
                "universe -- pick the last date with adequate coverage."
            ),
        },
        # No `servers` entry on purpose: this page is served from the same origin as
        # the API, so Swagger UI's "Try it out" targets the right host by default,
        # whatever domain it was reached on.
        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
        "paths": {
            "/healthz": {
                "get": {
                    "summary": "Liveness and database reachability",
                    "description": (
                        "Always 200 while the process is alive; `ok` reports "
                        "the database."
                    ),
                    "security": [],
                    "responses": {
                        "200": {
                            "description": "Process is up",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                        "required": ["ok"],
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/v1/catalogue": {
                "get": {
                    "summary": "What datasets exist, and optionally their coverage",
                    "description": (
                        "The discovery surface. Returns every exportable dataset with "
                        "its path, columns and the date column callers filter on.\n\n"
                        "`coverage=1` adds the first and last date (and ticker count) "
                        "per dataset, which is the cheap way to answer 'is there "
                        "enough history here for my backtest' before pulling anything. "
                        "It costs one query per dataset, so it is opt-in; the default "
                        "call needs no database at all."
                    ),
                    "parameters": [
                        {
                            "name": "coverage",
                            "in": "query",
                            "required": False,
                            "description": "Include per-dataset date coverage.",
                            "schema": {"type": "boolean", "default": False},
                        }
                    ],
                    "responses": {
                        "200": {"description": "The dataset catalogue"},
                        "400": {"description": "A parameter was missing or out of range"},
                        "401": {"description": "Missing or wrong API key"},
                        "503": {"description": "The database is unreachable"},
                    },
                }
            },
            "/v1/coverage": {
                "get": {
                    "summary": "How much price history exists",
                    "description": "The cheap call to make before asking for a panel.",
                    "parameters": [since, lookback],
                    "responses": {
                        "200": {"description": "Row, symbol and date counts over the window"},
                        "400": {"description": "A parameter was missing or out of range"},
                        "401": {"description": "Missing or wrong API key"},
                        "503": {"description": "The database is unreachable"},
                    },
                }
            },
            "/v1/panel.csv": {
                "get": {
                    "summary": "The liquidity-ranked price panel",
                    "description": (
                        "Streams CSV with the header `Date,ListedTicker,AdjustedClose`. "
                        "Symbols are ranked by average dollar volume over the window, "
                        "then truncated to `limit`.\n\n"
                        "A 200 means the stream **started**. A failure mid-export just "
                        "closes the connection, so a truncated body must be treated as "
                        "a failed request."
                    ),
                    "parameters": [
                        since,
                        lookback,
                        {
                            "name": "min_bars",
                            "in": "query",
                            "required": False,
                            "description": "Minimum bars a symbol needs to be considered.",
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100000,
                                "default": DEFAULT_MIN_BARS,
                            },
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "description": (
                                "Maximum number of symbols. Capped by the "
                                "service's max_rows."
                            ),
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": settings.max_rows,
                                "default": DEFAULT_LIMIT,
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "The panel as CSV",
                            "headers": {
                                "X-Panel-Since": {
                                    "description": "The resolved lower bound",
                                    "schema": {"type": "string", "format": "date"},
                                },
                                "X-Panel-Limit": {
                                    "description": "The resolved symbol cap",
                                    "schema": {"type": "integer"},
                                },
                            },
                            "content": {
                                "text/csv": {
                                    "schema": {"type": "string"},
                                    "example": (
                                        "Date,ListedTicker,AdjustedClose\n"
                                        "2020-09-08,AAPL,109.3381\n"
                                    ),
                                }
                            },
                        },
                        "400": {"description": "A parameter was missing or out of range"},
                        "401": {"description": "Missing or wrong API key"},
                        "503": {"description": "The database is unreachable"},
                    },
                }
            },
            "/v1/holdings/summary": {
                "get": {
                    "summary": "13F ingest counters",
                    "description": (
                        "Includes the intermediate counters on purpose: 'holdings are low' "
                        "is unactionable without knowing whether CUSIP coverage or the "
                        "processed data sets are the constraint."
                    ),
                    "responses": {
                        "200": {"description": "Current counters"},
                        "401": {"description": "Missing or wrong API key"},
                        "503": {"description": "The database is unreachable"},
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            }
        },
    }
    document["paths"].update(
        _dataset_paths(settings, since=since, lookback=lookback, until=until, tickers=tickers)
    )
    return document


def _dataset_paths(
    settings: Settings,
    *,
    since: dict[str, Any],
    lookback: dict[str, Any],
    until: dict[str, Any],
    tickers: dict[str, Any],
) -> dict[str, Any]:
    """The `/v1/<dataset>.csv` entries, generated from the registry.

    Generated rather than hand-written: `router.API_PATHS` is derived from the same
    registry, and the test that compares the two is what keeps a new dataset from
    being served-but-undocumented or documented-but-404.
    """
    from . import datasets as registry
    from .router import DEFAULT_DATASET_LIMIT

    limit = {
        "name": "limit",
        "in": "query",
        "required": False,
        "description": (
            "Maximum number of ROWS. Note this differs from `/v1/panel.csv`, where "
            "`limit` caps symbols instead.\n\n"
            "Rows are ordered by date then ticker and then truncated, so hitting "
            "this cap silently drops the tail rather than sampling the window. "
            "Narrow `since` or `tickers` instead of relying on `limit` to bound a "
            "large export."
        ),
        "schema": {
            "type": "integer",
            "minimum": 1,
            "maximum": settings.max_rows,
            "default": DEFAULT_DATASET_LIMIT,
        },
    }
    responses = {
        "200": {"description": "The dataset as CSV"},
        "400": {"description": "A parameter was missing or out of range"},
        "401": {"description": "Missing or wrong API key"},
        "404": {"description": "Unknown dataset"},
        "503": {"description": "The database is unreachable"},
    }
    return {
        f"/v1/{dataset.name}.csv": {
            "get": {
                "summary": dataset.summary,
                "description": (
                    f"{dataset.description}\n\n"
                    "Streams CSV ordered by date then ticker. **A 200 means the "
                    "stream started** -- a mid-export failure closes the connection, "
                    "so a truncated body must be treated as a failed request."
                ),
                "parameters": [since, lookback, until, tickers, limit],
                "responses": responses,
            }
        }
        for dataset in registry.DATASETS
    }
