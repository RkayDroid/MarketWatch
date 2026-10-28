"""
Stock endpoints.

    GET /stock/{symbol}                         snapshot: name, sector, price, exchange...
    GET /stock/{symbol}/historic?period=        OHLC series (hourly / daily / fortnightly / monthly)
                              &referenceDate=   ...the window ends on that date (or on each of up to 5
                                                comma-separated dates) instead of today

The window is defined by one rule (`_resolve_window`):

    D' = the last trading day on or before `referenceDate` (default: today)

    DAY        the session on D'
    WEEK       the 5 sessions ending on D'
    MONTH      the 30 calendar days ending on D'
    HALF_YEAR  182 days         "
    YEAR       365 days         "
    FIVE_YEAR  5 years          "

Every record carries the bar's open / high / low / close, so the window's
traded extremes are `max(high)` / `min(low)` over the series, and the
response carries `referenceDate` (what was asked, or today) and `window`
(what was served, `start`..`end` inclusive, exchange-local dates).

There is NO caching in this version: every window is exactly one live
yfinance chart call (two for `.info`, plus one retry for dotted symbols),
so what a client asks for maps 1:1 onto Yahoo Finance traffic. A caching
layer is deliberately deferred until the use cases are settled. The only
protection is the sliding-window rate limiter in `rate_limit.py`, which
hard-caps upstream calls per hour and answers 429 beyond that.
"""

import re
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, NamedTuple, Optional

import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException, Query
from yfinance.exceptions import YFException, YFRateLimitError, YFTickerMissingError

from rate_limit import RateLimitExceeded, SlidingWindowRateLimiter

router = APIRouter(prefix="/stock", tags=["Stock Data"])

# --------------------------------------------------------------------------
# Upstream budget
#
# yfinance talks to Yahoo Finance's free, unauthenticated endpoints, which
# are informally capped around 2000 requests/hour per source IP. Every
# upstream call made by this module acquires a slot from the limiter first;
# when the hourly budget is gone the API answers 429 immediately instead
# of hammering Yahoo and risking an IP block.
# --------------------------------------------------------------------------
UPSTREAM_CALL_BUDGET_PER_HOUR = 1800  # 10% safety margin under the 2000/hr informal cap

_limiter = SlidingWindowRateLimiter(max_calls=UPSTREAM_CALL_BUDGET_PER_HOUR, window_seconds=3600)


class Period(str, Enum):
    """Look-back window accepted by /historic (case-insensitive)."""

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    HALF_YEAR = "HALF_YEAR"
    YEAR = "YEAR"
    FIVE_YEAR = "FIVE_YEAR"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            normalised = value.strip().upper()
            for member in cls:
                if member.value == normalised:
                    return member
        return None


# --------------------------------------------------------------------------
# The window rule
#
# Short periods count SESSIONS (what every "1D / 5D" chart button means),
# long periods count CALENDAR time ending on the last session ("1M / 6M /
# 1Y / 5Y"). Both kinds end on D', the last trading day on or before the
# reference date, so a weekend or holiday reference date resolves to the
# session before it instead of coming back empty.
# --------------------------------------------------------------------------
_SESSIONS = {
    Period.DAY: 1,
    Period.WEEK: 5,
}

_CALENDAR_DAYS = {
    Period.MONTH: 30,
    Period.HALF_YEAR: 182,
    Period.YEAR: 365,
    Period.FIVE_YEAR: 5 * 365 + 2,
}

# Calendar days the window nominally spans (a session-counted period's
# sessions fit in this many days when there are no holidays); used to size
# the upstream call and to check intraday retention.
_NOMINAL_SPAN_DAYS = {
    Period.DAY: 1,
    Period.WEEK: 7,
    **_CALENDAR_DAYS,
}

# D' is found by looking at the bars Yahoo returns, so the call reaches this
# many days further back than the nominal span. 14 days covers every known
# exchange closure (long weekend + holiday, Golden Week, the 2001 NYSE
# closure) and lets a WEEK that straddles holidays still find 5 sessions.
RESOLUTION_LOOKBACK_DAYS = 14

HOURLY_INTERVAL = "60m"
DAILY_INTERVAL = "1d"

# Yahoo keeps hourly bars for ~730 days only (yfinance encodes the same
# limit). A DAY window starting earlier than that can never be served, so
# it is rejected (422) before any upstream call; the resolution look-back
# is clamped to the horizon because Yahoo rejects a `start` beyond it
# outright.
HOURLY_RETENTION_DAYS = 730

# `referenceDate` accepts a comma-separated list; each date is one upstream
# call, so the list is capped.
MAX_REFERENCE_DATES = 5
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Marks a frame with the symbol Yahoo actually answered for (e.g. "BRK-B"
# when the client asked for "BRK.B"). pandas `attrs` survive slicing.
YAHOO_SYMBOL_ATTR = "yahoo_symbol"


# --------------------------------------------------------------------------
# Upstream access: one choke point for error mapping, one for chart calls
# --------------------------------------------------------------------------
def _upstream(fetch_fn: Callable[[], object]):
    """Run a live upstream fetch and normalise its failure modes (limiter
    exhaustion, yfinance errors, anything unexpected) into HTTP errors.
    `fetch_fn` must call `_limiter.try_acquire()` before each real HTTP
    request."""
    try:
        return fetch_fn()
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail=(
                "Local upstream call budget for yfinance/Yahoo Finance is "
                f"exhausted for this hour. Retry in ~{exc.retry_after_seconds:.0f}s."
            ),
        ) from exc
    except YFRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail="Yahoo Finance rate-limited this request. Please retry later.",
        ) from exc
    except YFException as exc:
        raise HTTPException(status_code=502, detail=f"yfinance error: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw 500 for upstream data issues
        raise HTTPException(status_code=502, detail=f"Upstream data error: {exc}") from exc


def _fetch_window(ticker: str, first_day: date, day_after_last: date, interval: str) -> pd.DataFrame:
    """One chart call for an explicit date window, charged to the limiter.
    yfinance treats naive dates as midnight in the exchange's time zone,
    `start` inclusive and `end` exclusive, so the call returns exactly the
    calendar days [first_day, day_after_last).

    Dotted symbols: Yahoo uses dots for exchange suffixes (SAP.DE, 0700.HK,
    BARC.L) - those work as-is - but expects a DASH for US share classes
    (BRK-B), which most people type with a dot (BRK.B). If a dotted symbol
    returns nothing, retry once with dots replaced by dashes; the retry is
    a second, separately charged upstream call."""
    return _fetch_chart(ticker, start=first_day, end=day_after_last, interval=interval)


def _fetch_chart(ticker: str, **history_kwargs) -> pd.DataFrame:
    """One `Ticker.history(...)` call plus the dotted->dashed retry. The
    returned frame carries the symbol that produced it in
    `df.attrs[YAHOO_SYMBOL_ATTR]`."""
    df = _history_or_missing(ticker, history_kwargs)
    if df is None or df.empty:
        if "." in ticker:
            dashed = ticker.replace(".", "-")
            df = _history_or_missing(dashed, history_kwargs)
            if df is not None and not df.empty:
                df.attrs[YAHOO_SYMBOL_ATTR] = dashed
                return df
        df = _empty_chart()
    df.attrs[YAHOO_SYMBOL_ATTR] = ticker
    return df


def _history_or_missing(symbol: str, history_kwargs: dict) -> Optional[pd.DataFrame]:
    """Charged `history()` call. Returns None when yfinance reports the
    symbol as unknown by RAISING rather than by returning an empty frame
    (it does that when `yf.config.debug.hide_exceptions` is False), so
    callers treat both signals the same way. Anything else propagates."""
    _limiter.try_acquire()
    try:
        return yf.Ticker(symbol).history(auto_adjust=True, **history_kwargs)
    except YFTickerMissingError:
        return None


def _empty_chart() -> pd.DataFrame:
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def _yahoo_symbol(df: pd.DataFrame, ticker: str) -> str:
    """The symbol Yahoo answered for (dash form for US share classes)."""
    return df.attrs.get(YAHOO_SYMBOL_ATTR, ticker)


# --------------------------------------------------------------------------
# Window resolution (shared by both endpoints)
# --------------------------------------------------------------------------
class Window(NamedTuple):
    """Exchange-local calendar dates the served bars span, both inclusive.
    For session-counted periods `start` is the first session's date; for
    calendar periods it is `end - (N - 1)` whether or not it traded."""

    start: date
    end: date  # D': the last trading day on or before the reference date

    def as_json(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


class Loaded(NamedTuple):
    """The bars of one resolved window plus what the response reports."""

    df: pd.DataFrame  # empty if nothing traded on or before the reference date
    window: Optional[Window]
    reference_date: date  # what was asked, or the exchange-local today
    symbol: str  # the symbol Yahoo answered for


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _today_local(tz) -> date:
    """Today's calendar date in the exchange's time zone."""
    return pd.Timestamp.now(tz=tz).date()


def _local_dates(df: pd.DataFrame) -> np.ndarray:
    """Exchange-local calendar date of every bar (the index is tz-aware in
    the exchange's zone, as yfinance returns it)."""
    return np.array(df.index.date, dtype=object)


def _resolve_window(df: pd.DataFrame, period: Period, reference_date: date):
    """Apply the window rule to a frame fetched with enough look-back:
    D' = last bar date on or before `reference_date`, then the period's
    sessions or calendar days ending on D'. Returns (bars, Window), or
    (empty frame, None) if nothing traded on or before the date."""
    if df.empty:
        return df, None
    dates = _local_dates(df)
    on_or_before = dates[dates <= reference_date]
    if len(on_or_before) == 0:
        return _empty_chart(), None
    end = on_or_before.max()
    if period in _SESSIONS:
        sessions = sorted(set(on_or_before))[-_SESSIONS[period] :]
        start = sessions[0]
    else:
        start = end - timedelta(days=_CALENDAR_DAYS[period] - 1)
    return df[(dates >= start) & (dates <= end)], Window(start, end)


def _load_window(
    ticker: str, period: Period, reference_date: Optional[date], interval: str, retention_days: Optional[int]
) -> Loaded:
    """One upstream call for the bars of `period`'s window ending on the
    last session on or before `reference_date` (None = today). The call
    covers the nominal span plus the resolution look-back, clamped to
    Yahoo's retention for the bar size; D' and the exact window are then
    taken from the bars themselves."""
    today = _today_utc()
    # Rolling: "today" in the exchange's zone is not known before the call
    # (exchanges east of UTC are on tomorrow for part of every UTC day), so
    # ask up to UTC tomorrow and take the newest bar as D'.
    asked = reference_date if reference_date is not None else today + timedelta(days=1)
    first_day = asked - timedelta(days=_NOMINAL_SPAN_DAYS[period] - 1 + RESOLUTION_LOOKBACK_DAYS)
    if retention_days is not None:
        first_day = max(first_day, today - timedelta(days=retention_days))
    df = _upstream(lambda: _fetch_window(ticker, first_day, asked + timedelta(days=1), interval))
    bars, window = _resolve_window(df, period, asked)
    if reference_date is None:
        reference_date = _today_local(df.index.tz) if not df.empty else today
    return Loaded(bars, window, reference_date, _yahoo_symbol(df, ticker))


# --------------------------------------------------------------------------
# referenceDate parsing and validation (before any upstream call)
# --------------------------------------------------------------------------
def _parse_reference_dates(raw: str) -> List[date]:
    """Parse the `referenceDate` query value: 1..MAX_REFERENCE_DATES
    comma-separated YYYY-MM-DD dates, whitespace tolerated, no duplicates.
    Every problem is a 422 naming the offending item, raised before any
    upstream call is made."""
    items = [item.strip() for item in raw.split(",")]
    if len(items) > MAX_REFERENCE_DATES:
        raise HTTPException(
            status_code=422,
            detail=f"referenceDate accepts at most {MAX_REFERENCE_DATES} dates, got {len(items)}.",
        )
    dates: List[date] = []
    for item in items:
        if not item:
            raise HTTPException(
                status_code=422,
                detail="referenceDate contains an empty item; expected comma-separated YYYY-MM-DD dates.",
            )
        if not _ISO_DATE.match(item):
            raise HTTPException(status_code=422, detail=f"referenceDate '{item}' is not a valid YYYY-MM-DD date.")
        try:
            parsed = date.fromisoformat(item)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"referenceDate '{item}' is not a valid calendar date.")
        if parsed in dates:
            raise HTTPException(status_code=422, detail=f"referenceDate contains '{item}' more than once.")
        dates.append(parsed)
    return dates


class Retention(NamedTuple):
    """How far back Yahoo keeps the bars a period needs, for the 422 message."""

    days: int
    bars: str  # e.g. "hourly"
    alternative: str  # e.g. "a daily period (WEEK, MONTH...)"


def _validate_reference_date(reference_date: date, period: Period, retention: Optional[Retention]) -> None:
    """Reject windows that can never have data BEFORE spending upstream
    budget: future dates, and windows whose nominal span starts before
    Yahoo's retention of the bars the period needs (`retention` is None
    for daily bars, which go back decades)."""
    today = _today_utc()
    # Exchanges east of UTC are already on "tomorrow" for part of every UTC
    # day, so their current local date must still be accepted: allow one
    # day of slack (no exchange is more than 14h ahead of UTC).
    if reference_date > today + timedelta(days=1):
        raise HTTPException(
            status_code=422,
            detail=f"referenceDate {reference_date.isoformat()} is in the future (today is {today.isoformat()} UTC).",
        )
    if retention is None:
        return
    nominal_start = reference_date - timedelta(days=_NOMINAL_SPAN_DAYS[period] - 1)
    if nominal_start < today - timedelta(days=retention.days):
        raise HTTPException(
            status_code=422,
            detail=(
                f"referenceDate {reference_date.isoformat()} is too old for period {period.value}: Yahoo Finance "
                f"keeps {retention.bars} bars for ~{retention.days} days only. Use a more recent referenceDate or "
                f"{retention.alternative}."
            ),
        )


# --------------------------------------------------------------------------
# Request flow
# --------------------------------------------------------------------------
class Bars(NamedTuple):
    """How a period is served: the bar size fetched, the record granularity
    label reported, and the retention of that bar size (None: daily bars,
    which go back decades)."""

    interval: str
    label: str
    retention: Optional[Retention]

    @property
    def intraday(self) -> bool:
        return self.interval != DAILY_INTERVAL


def _serve(ticker: str, period: Period, reference_date: Optional[str], bars: Bars, render) -> dict:
    """Validate every reference date first, load one window per date,
    render each (None = nothing usable), 404 only if nothing at all was
    served. Without `referenceDate` the single window ending today is
    returned flat; with it the response is keyed by date."""
    ticker = ticker.upper()
    header = {"ticker": ticker, "symbol": ticker, "period": period.value, "interval": bars.label}
    retention_days = bars.retention.days if bars.retention else None

    if reference_date is None:
        loaded = _load_window(ticker, period, None, bars.interval, retention_days)
        body = render(loaded.df, bars.intraday) if loaded.window is not None else None
        if body is None:
            raise HTTPException(status_code=404, detail=f"No price data found for {ticker} over period {period.value}.")
        header["symbol"] = loaded.symbol
        return {**header, "referenceDate": loaded.reference_date.isoformat(), "window": loaded.window.as_json(), **body}

    dates = _parse_reference_dates(reference_date)
    for d in dates:
        _validate_reference_date(d, period, bars.retention)

    results = {}
    for d in dates:
        loaded = _load_window(ticker, period, d, bars.interval, retention_days)
        body = render(loaded.df, bars.intraday) if loaded.window is not None else None
        if body is not None:
            header["symbol"] = loaded.symbol
        results[d.isoformat()] = None if body is None else {"window": loaded.window.as_json(), **body}
    if all(r is None for r in results.values()):
        listed = ", ".join(d.isoformat() for d in dates)
        raise HTTPException(
            status_code=404,
            detail=f"No price data found for {ticker} over period {period.value} ending on {listed}.",
        )
    return {**header, "results": results}


_REFERENCE_DATE_QUERY = Query(
    None,
    alias="referenceDate",
    description=(
        "Optional end date(s) of the window, YYYY-MM-DD: one date (2026-10-23) or up to "
        f"{MAX_REFERENCE_DATES} unique dates separated by commas (2026-10-23,2026-10-16). The window ends on the "
        "last trading day on or before each date instead of today, and the response is keyed by date. Dates must "
        "not be in the future; periods served from intraday bars must be within Yahoo's retention (see the "
        "endpoint description)."
    ),
)


# --------------------------------------------------------------------------
# Downsampling / serialisation helpers
# --------------------------------------------------------------------------
_OHLC_COLUMNS = ["Open", "High", "Low", "Close"]


def _aggregate_bins(df: pd.DataFrame, bin_ids: np.ndarray) -> pd.DataFrame:
    """One OHLC bar per bin: first Open, max High, min Low, last Close,
    stamped with the last bar of the bin (so the record's datetime is the
    last trading day in it). NaNs are skipped within a bin."""
    groups = df.groupby(bin_ids, sort=False)
    out = groups[_OHLC_COLUMNS].agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"})
    out.index = pd.DatetimeIndex([df.index[groups.indices[key][-1]] for key in out.index], name=df.index.name)
    return out.sort_index()


def _fortnightly(df: pd.DataFrame, bin_days: int = 14) -> pd.DataFrame:
    """One record per `bin_days`-wide bin, bins anchored at the newest bar
    so the latest record always ends on the window's last close."""
    if df.empty:
        return df
    # age in exchange-local calendar days (timestamp arithmetic would be
    # off by one across a DST change: local midnights are 23/25h apart)
    dates = _local_dates(df)
    age_days = np.array([(dates[-1] - d).days for d in dates])
    return _aggregate_bins(df, age_days // bin_days)


def _monthly(df: pd.DataFrame) -> pd.DataFrame:
    """One record per calendar month (the current month's is partial)."""
    if df.empty:
        return df
    return _aggregate_bins(df, df.index.strftime("%Y-%m").to_numpy())


def _number(value) -> Optional[float]:
    return None if pd.isna(value) else round(float(value), 4)


def _to_records(df: pd.DataFrame, intraday: bool) -> List[dict]:
    """[{datetime, open, high, low, close}] per bar. Intraday bars carry a
    real time -> full ISO-8601 with exchange UTC offset; daily-or-coarser
    bars only know the date -> YYYY-MM-DD. Bars without a Close are
    skipped; a missing Open/High/Low is null."""
    records = []
    for ts, row in df[_OHLC_COLUMNS].iterrows():
        if pd.isna(row["Close"]):
            continue
        records.append(
            {
                "datetime": ts.isoformat() if intraday else ts.date().isoformat(),
                "open": _number(row["Open"]),
                "high": _number(row["High"]),
                "low": _number(row["Low"]),
                "close": _number(row["Close"]),
            }
        )
    return records


# ==========================================================================
# GET /stock/{symbol}
# ==========================================================================
_SNAPSHOT_REQUIRED_ANY = ("regularMarketPrice", "currentPrice", "longName", "shortName")


def _build_snapshot(ticker: str, info: dict) -> dict:
    price = info.get("regularMarketPrice")
    if price is None:
        price = info.get("currentPrice")
    if price is None:
        price = info.get("previousClose")
    return {
        "ticker": ticker,
        "sector": info.get("sector"),
        "fulldayPrice": price,
        "currency": info.get("currency"),
        "symbol": info.get("symbol") or ticker,
        "language": info.get("language"),
        "longName": info.get("longName") or info.get("shortName"),
        "exchange": info.get("exchange"),
        "market": info.get("market"),
        "displayName": info.get("displayName") or info.get("shortName"),
    }


@router.get("/{ticker}")
def get_stock_snapshot(ticker: str):
    """Current snapshot of a symbol.

    Response::

        {
          "ticker": "AAPL",
          "sector": "Technology",
          "fulldayPrice": 347.725,
          "currency": "USD",
          "symbol": "AAPL",
          "language": "en-US",
          "longName": "Apple Inc.",
          "exchange": "NMS",
          "market": "us_market",
          "displayName": "Apple"
        }

    * `fulldayPrice` is Yahoo's `regularMarketPrice` (the live price during
      the session, the official close after it), falling back to
      `currentPrice` then `previousClose`.
    * `sector` comes from Yahoo's company profile; it is null for ETFs,
      indices and funds. `displayName` falls back to `shortName`.
    * Upstream cost: 2 requests per call (yfinance `.info` = quoteSummary
      + quote); 4 if a dotted symbol has to be retried in dash form.
    """
    ticker = ticker.upper()

    def fetch_info(symbol: str):
        # yfinance .info issues two HTTP requests (quoteSummary + v7 quote).
        _limiter.try_acquire()
        _limiter.try_acquire()
        return yf.Ticker(symbol).info or {}

    def fetch():
        info = fetch_info(ticker)
        if not any(info.get(k) is not None for k in _SNAPSHOT_REQUIRED_ANY) and "." in ticker:
            # Yahoo expects US share classes dashed (BRK.B -> BRK-B).
            info = fetch_info(ticker.replace(".", "-"))
        if not any(info.get(k) is not None for k in _SNAPSHOT_REQUIRED_ANY):
            return None
        return _build_snapshot(ticker, info)

    snapshot = _upstream(fetch)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} not found.")
    return snapshot


# ==========================================================================
# GET /stock/{symbol}/historic
# ==========================================================================
_HOURLY_RETENTION = Retention(HOURLY_RETENTION_DAYS, "hourly", "a daily period (WEEK, MONTH...)")

# period -> bars fetched and record granularity reported
_BARS = {
    Period.DAY: Bars(HOURLY_INTERVAL, "1h", _HOURLY_RETENTION),
    Period.WEEK: Bars(DAILY_INTERVAL, "1d", None),
    Period.MONTH: Bars(DAILY_INTERVAL, "1d", None),
    Period.HALF_YEAR: Bars(DAILY_INTERVAL, "2wk", None),
    Period.YEAR: Bars(DAILY_INTERVAL, "2wk", None),
    Period.FIVE_YEAR: Bars(DAILY_INTERVAL, "1mo", None),
}

# period -> downsampler applied to the window's daily bars
_DOWNSAMPLE = {
    Period.HALF_YEAR: _fortnightly,
    Period.YEAR: _fortnightly,
    Period.FIVE_YEAR: _monthly,
}


@router.get("/{ticker}/historic")
def get_historic_prices(
    ticker: str,
    period: Period = Query(Period.DAY, description="Look-back window. Case-insensitive. Default DAY."),
    reference_date: Optional[str] = _REFERENCE_DATE_QUERY,
):
    """OHLC price series for `ticker` over the chosen period.

    The window ends on D', the last trading day on or before
    `referenceDate` (default: today):

    | period    | window                          | one record per         | `datetime` format           |
    |-----------|---------------------------------|------------------------|-----------------------------|
    | DAY       | the session on D'               | hour (hourly bar)      | ISO-8601 w/ exchange offset |
    | WEEK      | the 5 sessions ending on D'     | trading day            | YYYY-MM-DD                  |
    | MONTH     | 30 calendar days ending on D'   | trading day            | YYYY-MM-DD                  |
    | HALF_YEAR | 182 calendar days ending on D'  | fortnight (14-day bin) | YYYY-MM-DD                  |
    | YEAR      | 365 calendar days ending on D'  | fortnight (14-day bin) | YYYY-MM-DD                  |
    | FIVE_YEAR | 5 years ending on D'            | calendar month         | YYYY-MM-DD                  |

    Response::

        {
          "ticker": "AAPL",
          "symbol": "AAPL",
          "period": "WEEK",
          "interval": "1d",
          "referenceDate": "2026-10-25",
          "window": {"start": "2026-10-19", "end": "2026-10-23"},
          "count": 5,
          "data": [
            {"datetime": "2026-10-19", "open": 171.9, "high": 173.2, "low": 171.4, "close": 172.34},
            ...
          ]
        }

    * Each record is one bar's Open / High / Low / Close, so the window's
      traded extremes are `max(high)` and `min(low)` over `data`, and the
      record they sit in says when (to the hour for DAY, to the day
      otherwise). Fortnight/month records are aggregates of their bin:
      first open, highest high, lowest low, last close, stamped with the
      bin's last trading day; bins are anchored at the newest bar so the
      final record always ends on the window's last close. DAY includes
      the running hourly bar while the market is open.
    * `referenceDate` is the date asked for (today when omitted), `window`
      the exchange-local dates actually served: `end` is D' (a Sunday
      resolves to the Friday before), `start` the first session of a
      DAY/WEEK window or `end - 29/181/364 days / 5 years` for the
      calendar periods.
    * With `referenceDate=YYYY-MM-DD[,YYYY-MM-DD...]` (up to 5 unique
      dates) the response is keyed by date, in the order given::

        {
          "ticker": "BRK.B", "symbol": "BRK-B", "period": "WEEK", "interval": "1d",
          "results": {
            "2026-10-25": {"window": {"start": "2026-10-19", "end": "2026-10-23"}, "count": 5, "data": [...]},
            "2010-01-01": null
          }
        }

      An entry is `null` only when nothing traded on or before that date
      (before the listing); the request is 404 only if every entry is
      null.
    * `ticker` echoes the request; `symbol` is the symbol Yahoo actually
      answered for - they differ only for US share classes typed with a
      dot (BRK.B -> BRK-B), which are retried in Yahoo's dash form.
    * Upstream cost: one live chart call per window (dotted symbols may
      be retried once). Nothing is cached. Invalid input (malformed or
      duplicate dates, more than 5, future dates, DAY older than Yahoo's
      ~730-day hourly retention) is rejected with 422 before any call.
    """
    downsample = _DOWNSAMPLE.get(period)

    def render(df: pd.DataFrame, intraday: bool) -> Optional[dict]:
        if downsample is not None and not df.empty:
            df = downsample(df)
        records = _to_records(df, intraday)
        return {"count": len(records), "data": records} if records else None

    return _serve(ticker, period, reference_date, _BARS[period], render)


# ==========================================================================
# Upstream budget (read-only)
# ==========================================================================
def upstream_budget() -> dict:
    """How much of the hourly upstream budget is left; used by /status."""
    return {"per_hour": UPSTREAM_CALL_BUDGET_PER_HOUR, "remaining": _limiter.remaining()}
