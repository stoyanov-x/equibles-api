"""The exportable datasets, described once.

Each entry is a *declaration*, not a query: the SQL is built from it by
:mod:`equibles_api.sql`. That keeps three things in one place per dataset -- the
table, the join that resolves a plain ticker, and the columns a consumer may rely
on -- instead of scattering them across a hand-written query per endpoint.

**On interpolation.** :mod:`equibles_api.sql` says every value is bound as a
parameter, and that still holds: the only strings that reach the SQL text are the
constants in this module, which are written here by hand. Everything a caller can
influence -- the date bounds, the ticker list, the row cap -- is a bound
parameter. A dataset *name* from a request is looked up in :data:`BY_NAME` and a
miss is a 404, so it can never reach the query text either.

**On the `ticker` expression.** Equibles' schema does not carry a ticker on every
table. ``DailyShortVolume`` and ``FailToDeliver`` denormalise ``ListedTicker``;
``CashDividend`` and ``InsiderTransaction`` require a join back to it. Consumers
should not have to know which is which, so each dataset declares the expression
that yields a plain ticker and the joins it needs are applied here.

The upstream identity model, for whoever extends this: ``EquityListing`` holds
ticker + market, ``CommonStock`` holds the issuer's primary ticker, and
``EquityIssuer`` links a filing to a ``CommonStock`` (issuers are per-CIK, while
listings are per-venue -- one issuer can have several).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    """One output column: the CSV header, and the SQL that produces it."""

    name: str
    expression: str


@dataclass(frozen=True)
class Dataset:
    """Everything needed to export one table as CSV, and to document it."""

    name: str
    summary: str
    description: str
    table: str
    #: The column callers filter and order on. Every dataset has exactly one.
    date_column: str
    #: An expression yielding the plain ticker, for the `tickers` filter.
    ticker: str
    columns: tuple[Column, ...]
    #: Joins needed to make `ticker` resolvable. The main table is always `p`.
    joins: str = ""


DATASETS: tuple[Dataset, ...] = (
    Dataset(
        name="short-volume",
        summary="Daily short sale volume, per listing",
        description=(
            "FINRA-reported daily short volume per venue. `ShortVolume` is the "
            "shares sold short that day and `TotalVolume` the day's total, so "
            "`ShortVolume / TotalVolume` is the ratio most consumers want; both "
            "are summed across venues for a given ticker and date, and `Market` "
            "identifies the venue if you need to disaggregate."
        ),
        table='"DailyShortVolume"',
        date_column='p."Date"',
        ticker='p."ListedTicker"',
        columns=(
            Column("Date", 'p."Date"'),
            Column("Ticker", 'p."ListedTicker"'),
            Column("Market", 'p."Market"'),
            Column("ShortVolume", 'p."ShortVolume"'),
            Column("ShortExemptVolume", 'p."ShortExemptVolume"'),
            Column("TotalVolume", 'p."TotalVolume"'),
        ),
    ),
    Dataset(
        name="dividends",
        summary="Cash dividends, by ex-date",
        description=(
            "Cash dividends keyed by **ex-date**, which is the date that matters "
            "for a return series: the holder on the day before the ex-date "
            "receives the payment. Use it to convert a price series into a total "
            "return series, or to check whether an 'adjusted close' already has "
            "the dividend applied.\n\n"
            "`AmountPerShare` is in `Currency`. "
            "`PriceAdjustmentAppliedAmountPerShare` records the amount actually "
            "used in the vendor's own price adjustment, which can differ from the "
            "declared amount."
        ),
        table='"CashDividend"',
        date_column='p."ExDate"',
        ticker='el."Ticker"',
        joins='JOIN "EquityListing" el ON el."Id" = p."EquityListingId"',
        columns=(
            Column("ExDate", 'p."ExDate"'),
            Column("Ticker", 'el."Ticker"'),
            Column("AmountPerShare", 'p."AmountPerShare"'),
            Column("Currency", 'p."Currency"'),
            Column(
                "PriceAdjustmentAppliedAmountPerShare",
                'p."PriceAdjustmentAppliedAmountPerShare"',
            ),
        ),
    ),
    Dataset(
        name="insider-transactions",
        summary="Form 4 insider transactions",
        description=(
            "Insider dealings from Form 4 filings, the classic informed-trader "
            "signal. Keyed by `TransactionDate` -- when the trade happened -- "
            "while `FilingDate` is when it became public. **The two are days or "
            "weeks apart and using the earlier as a signal date is look-ahead**: "
            "a consumer must gate on `FilingDate`.\n\n"
            "`TransactionCode` is the SEC code (P = open-market purchase, S = "
            "sale); `AcquiredDisposed` is 1 for acquired and 0 for disposed. "
            "`IsAmendment` flags restatements -- an amended filing supersedes the "
            "original, so filtering them out double-counts and filtering them in "
            "without care double-counts the other way. `IsRule10b5One` marks "
            "pre-scheduled trades, which carry much less information than "
            "discretionary ones."
        ),
        table='"InsiderTransaction"',
        date_column='p."TransactionDate"',
        ticker='cs."Ticker"',
        joins=(
            'JOIN "EquityIssuer" ei ON ei."Id" = p."EquityIssuerId" '
            'JOIN "CommonStock" cs ON cs."Id" = ei."CommonStockId"'
        ),
        columns=(
            Column("TransactionDate", 'p."TransactionDate"'),
            Column("FilingDate", 'p."FilingDate"'),
            Column("Ticker", 'cs."Ticker"'),
            Column("TransactionCode", 'p."TransactionCode"'),
            Column("AcquiredDisposed", 'p."AcquiredDisposed"'),
            Column("Shares", 'p."Shares"'),
            Column("PricePerShare", 'p."PricePerShare"'),
            Column("SharesOwnedAfter", 'p."SharesOwnedAfter"'),
            Column("IsAmendment", 'p."IsAmendment"'),
            Column("IsRule10b5One", 'p."IsRule10b5One"'),
            Column("AccessionNumber", 'p."AccessionNumber"'),
        ),
    ),
    Dataset(
        name="fail-to-deliver",
        summary="Failures to deliver, by settlement date",
        description=(
            "SEC failures-to-deliver. A failure means shares were not delivered "
            "by settlement, which is a read on settlement stress and on the "
            "difficulty of borrowing a name. `Quantity` is shares and `Price` the "
            "day's price, so their product is the notional."
        ),
        table='"FailToDeliver"',
        date_column='p."SettlementDate"',
        ticker='p."ListedTicker"',
        columns=(
            Column("SettlementDate", 'p."SettlementDate"'),
            Column("Ticker", 'p."ListedTicker"'),
            Column("Quantity", 'p."Quantity"'),
            Column("Price", 'p."Price"'),
        ),
    ),
    Dataset(
        name="splits",
        summary="Stock splits",
        description=(
            "Split events as a ratio: `Numerator / Denominator` is the factor the "
            "number of shares multiplies by, so a 4-for-1 split is 4/1. "
            "`PriceSeriesTicker` is the ticker of the *price series* the split "
            "applies to, which is what a price consumer can join on. Useful for "
            "auditing whether an adjusted series really is adjusted."
        ),
        table='"StockSplit"',
        date_column='p."EffectiveDate"',
        ticker='p."PriceSeriesTicker"',
        columns=(
            Column("EffectiveDate", 'p."EffectiveDate"'),
            Column("Ticker", 'p."PriceSeriesTicker"'),
            Column("Numerator", 'p."Numerator"'),
            Column("Denominator", 'p."Denominator"'),
        ),
    ),
)

#: Lookup for the routing layer. A request names a dataset by string, and a miss
#: must be a 404 rather than a query.
BY_NAME: dict[str, Dataset] = {dataset.name: dataset for dataset in DATASETS}
