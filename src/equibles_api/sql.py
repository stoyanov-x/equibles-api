"""The queries this service is willing to run.

Every value is bound as a parameter -- nothing is interpolated. That matters more
here than usual: this process holds a database credential, so a formatting mistake
is not a bad response, it is the whole point of the service failing.

The table and column names are quoted because Equibles' schema is PascalCase
(`"ListedDailyStockPrice"`, `"AdjustedClose"`), and Postgres folds unquoted
identifiers to lowercase.
"""

from __future__ import annotations

from .datasets import Dataset

#: The panel the Foundry's cross-sectional family consumes: a liquidity-ranked set
#: of tickers, split/dividend-adjusted closes, one row per (date, ticker).
#:
#: Ranking by average dollar volume over the *whole* window, then filtering rows to
#: the same window, is deliberate: ranking on a shorter window than we export would
#: quietly let in names that were liquid only recently.
#:
#: ``Volume`` rides along with the close because the consumer gates on capacity, and
#: capacity is dollar volume. Without it a consumer that reads prices from here still
#: has to fetch volume per symbol from a separate provider -- which fails for exactly
#: the long tail of tickers this panel exists to add, and fails as *unknown* capacity,
#: which the gate treats as a rejection. The column is free: it is already in the
#: source table and already used by the liquidity ranking below.
PANEL_SQL = """
COPY (
  WITH liquid AS (
    SELECT
      "ListedTicker" AS ticker,
      count(*) AS bars,
      avg("Close" * "Volume") AS adusd
    FROM "ListedDailyStockPrice"
    WHERE "Date" >= %(since)s
    GROUP BY 1
    HAVING count(*) >= %(min_bars)s
    ORDER BY adusd DESC NULLS LAST
    LIMIT %(limit)s
  )
  SELECT p."Date", p."ListedTicker", p."AdjustedClose", p."Volume"
  FROM "ListedDailyStockPrice" p
  JOIN liquid l ON l.ticker = p."ListedTicker"
  WHERE p."Date" >= %(since)s
  ORDER BY p."ListedTicker", p."Date"
) TO STDOUT WITH CSV HEADER
"""

#: How much price history actually exists, and over what span. This is the cheap
#: call a consumer makes before asking for the panel.
COVERAGE_SQL = """
SELECT
  count(*) AS rows,
  count(DISTINCT "ListedTicker") AS symbols,
  count(DISTINCT "Date") AS dates,
  min("Date") AS first_date,
  max("Date") AS last_date
FROM "ListedDailyStockPrice"
WHERE "Date" >= %(since)s
"""

#: 13F state. Deliberately includes the intermediate counters: "holdings are low"
#: is unactionable without knowing whether CUSIP coverage or the data sets are the
#: constraint, and that distinction cost real debugging time once already.
HOLDINGS_SQL = """
SELECT
  (SELECT count(*) FROM "InstitutionalHolding") AS holdings,
  (SELECT count(*) FROM "InstitutionalFiling") AS filings,
  (SELECT count(*) FROM "ProcessedDataSet") AS datasets,
  (SELECT count(*) FROM "ProcessedFiling") AS processed_filings,
  (SELECT count(*) FROM "EquitySecurity" WHERE "Cusip" IS NOT NULL) AS securities_with_cusip
"""

HEALTH_SQL = "SELECT 1 AS ok"


# --- dataset exports ---------------------------------------------------------
#
# The three functions below BUILD query text from a :class:`~equibles_api.datasets.Dataset`.
# Interpolation is confined to that declaration: table, column and join text all
# come from the registry, which is written by hand in this package. Every value a
# caller can influence -- `since`, `until`, `tickers`, `limit` -- is passed to
# psycopg as a bound parameter and never appears in the string. `dataset.name` is
# never interpolated: the router resolves it through `datasets.BY_NAME` and 404s
# on a miss, so no caller-controlled text reaches here at all.


def _select_columns(dataset: Dataset) -> str:
    body = ",\n        ".join(
        f'{column.expression} AS "{column.name}"' for column in dataset.columns
    )
    return f"SELECT\n        {body}"


def dataset_export_sql(dataset: Dataset) -> str:
    """A streaming CSV export of one dataset, newest-last and bounded.

    The `limit` is not optional in spirit: some of these tables hold millions of
    rows, and `COPY` streams, so an unbounded request would hold a database
    connection open for as long as the client felt like reading. The cap is
    applied inside the subquery so Postgres stops producing rows rather than
    truncating a completed result.
    """
    return f"""
COPY (
  {_select_columns(dataset)}
  FROM {dataset.table} p
  {dataset.joins}
  WHERE {dataset.date_column} >= %(since)s
    AND {dataset.date_column} <= %(until)s
    AND (%(tickers)s::text[] IS NULL OR {dataset.ticker} = ANY(%(tickers)s::text[]))
  ORDER BY {dataset.date_column}, {dataset.ticker}
  LIMIT %(limit)s
) TO STDOUT WITH CSV HEADER
"""


def dataset_coverage_sql(dataset: Dataset) -> str:
    """Span and volume of one dataset, for the catalogue.

    `min`/`max` on the date column rather than `count(*)`: on the largest table
    here a count is a multi-second sequential scan, while the extremes come off
    the date index. The count is deliberately absent -- it is the expensive part
    and the least actionable, since a consumer choosing a dataset cares whether
    the history reaches back far enough, not how many rows it holds.
    """
    return f"""
SELECT
  count(DISTINCT {dataset.ticker}) AS tickers,
  min({dataset.date_column}) AS first_date,
  max({dataset.date_column}) AS last_date
FROM {dataset.table} p
{dataset.joins}
"""

