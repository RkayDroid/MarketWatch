"""
Stock endpoints.

    GET /stock/{symbol}                         snapshot: name, sector, price, exchange...
    GET /stock/{symbol}/historic?period=        price series (hourly / daily / fortnightly / monthly)
                              &referenceDate=   ...the same window ending ON that date (or on each of
                                                up to 5 comma-separated dates) instead of now
    GET /stock/{symbol}/highlow?period=         highest & lowest traded price and when

There is NO caching in this version: every request is exactly one live
yfinance chart call (two for `.info`, plus one retry for dotted symbols),
so what a client asks for maps 1:1 onto Yahoo Finance traffic. A caching
layer is deliberately deferred until the use cases are settled. The only
protection is the sliding-window rate limiter in `rate_limit.py`, which
hard-caps upstream calls per hour and answers 429 beyond that.
"""

import re
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, Optional, Tuple

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
    """Look-back window accepted by /historic and /highlow (case-insensitive)."""

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


# Calendar-day length of each period's window.
_LOOKBACK_DAYS = {
    Period.DAY: 1,
    Period.WEEK: 7,
    Period.MONTH: 30,
    Period.HALF_YEAR: 182,
    Period.YEAR: 365,
    Period.FIVE_YEAR: 5 * 365 + 2,
}

# Smallest standard Yahoo range that covers each daily-bar window (Yahoo
# only accepts 1d,5d,1mo,3mo,6mo,1y,2y,5y,10y,ytd,max). The frame is then
# sliced to the exact look-back locally.
_DAILY_YAHOO_RANGE = {
    Period.WEEK: "1mo",
    Period.MONTH: "3mo",
    Period.HALF_YEAR: "1y",
    Period.YEAR: "2y",
    Period.FIVE_YEAR: "5y",
}

HOURLY_INTERVAL = "60m"
PRIMARY_HOURLY_PERIOD = "2d"
FALLBACK_PERIOD = "5d"
SESSION_INTERVAL = "5m"
DAILY_INTERVAL = "1d"

# Yahoo keeps hourly bars for ~730 days only (yfinance encodes the same
# limit), so a DAY window ending earlier than that can never be served and
# is rejected (422) before any upstream call.
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


def _fetch_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """One chart call for a Yahoo range (e.g. "5y"), charged to the limiter.

    Dotted symbols: Yahoo uses dots for exchange suffixes (SAP.DE, 0700.HK,
    BARC.L) - those work as-is - but expects a DASH for US share classes
    (BRK-B), which most people type with a dot (BRK.B). If a dotted symbol
    returns nothing, retry once with dots replaced by dashes; the retry is
    a second, separately charged upstream call.
    """
    return _fetch_chart(ticker, period=period, interval=interval)


def _fetch_window(ticker: str, first_day: date, day_after_last: date, interval: str) -> pd.DataFrame:
    """One chart call for an explicit date window, charged to the limiter.
    yfinance treats naive dates as midnight in the exchange's time zone,
    `start` inclusive and `end` exclusive, so the call returns exactly the
    calendar days [first_day, day_after_last). Same dotted-symbol retry as
    `_fetch_history`."""
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
# Source frames (each = one chart call per request)
#
#   hourly   2d @ 60m  (fallback 5d)   -> /historic DAY
#   session  1d @ 5m   (fallback 5d)   -> /highlow DAY
#   week     5d @ 60m                  -> /highlow WEEK
#   daily    <range> @ 1d              -> /historic and /highlow, daily periods
#
# Yahoo's chart endpoint accepts ONE symbol per request and retains
# intraday bars for a limited time only (~60 days for sub-hourly, 730 days
# for hourly), so daily bars are the only option for the longer windows.
# --------------------------------------------------------------------------
def _fetch_hourly_frame(ticker: str) -> pd.DataFrame:
    """Hourly bars covering at least the last 24h. A 2-day window is used
    (not 1d) so a full 24h is available right after the open; if it is
    empty (weekend/holiday) fall back to 5d - a second, separately
    charged upstream call."""
    df = _fetch_history(ticker, PRIMARY_HOURLY_PERIOD, HOURLY_INTERVAL)
    if df.empty:
        df = _fetch_history(ticker, FALLBACK_PERIOD, HOURLY_INTERVAL)
    return df


def _fetch_last_session_frame(ticker: str) -> pd.DataFrame:
    """5-minute bars for the most recent trading session. Yahoo's range=1d
    normally returns the last session even on weekends; if it is empty,
    fall back to 5d and keep only the latest session's bars."""
    df = _fetch_history(ticker, "1d", SESSION_INTERVAL)
    if df.empty:
        df = _fetch_history(ticker, FALLBACK_PERIOD, SESSION_INTERVAL)
        if not df.empty:
            last_session_date = df.index[-1].date()
            df = df[[ts.date() == last_session_date for ts in df.index]]
    return df


def _fetch_week_intraday_frame(ticker: str) -> pd.DataFrame:
    return _fetch_history(ticker, FALLBACK_PERIOD, HOURLY_INTERVAL)


def _fetch_daily_frame(ticker: str, period: Period) -> pd.DataFrame:
    """Daily bars covering `period`'s look-back, sliced to it exactly."""
    df = _fetch_history(ticker, _DAILY_YAHOO_RANGE[period], DAILY_INTERVAL)
    return _slice_lookback(df, _LOOKBACK_DAYS[period])


def _slice_lookback(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return df
    cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=days)
    return df[df.index >= cutoff]


def _localize_date(d: date, tz) -> pd.Timestamp:
    """Midnight of calendar day `d` in the frame's (exchange) time zone."""
    return pd.Timestamp(d).tz_localize(tz or "UTC")


def _window_bounds(reference_date: date, days: int) -> Tuple[date, date]:
    """(first_day, day_after_last) of the `days`-long window that ENDS on
    `reference_date` inclusive: start inclusive, end exclusive - the
    convention yfinance uses for start/end."""
    return reference_date - timedelta(days=days - 1), reference_date + timedelta(days=1)


def _slice_window(df: pd.DataFrame, reference_date: date, days: int) -> pd.DataFrame:
    """Keep only the bars of the `days`-long window ending on
    `reference_date` (inclusive), measured in calendar days of the
    exchange's time zone. yfinance already fetches exactly this window; the
    local slice just guarantees the contract regardless of upstream quirks
    and makes DAY one local trading date, not 24 absolute hours."""
    if df.empty:
        return df
    first_day, day_after = _window_bounds(reference_date, days)
    tz = df.index.tz
    return df[(df.index >= _localize_date(first_day, tz)) & (df.index < _localize_date(day_after, tz))]


# --------------------------------------------------------------------------
# Downsampling / serialisation helpers
# --------------------------------------------------------------------------
def _last_per_bin(df: pd.DataFrame, bin_days: int) -> pd.DataFrame:
    """Keep the last (most recent) bar of each `bin_days`-wide bin, with
    bins anchored at the newest bar so the latest record is always the
    most recent close. Used for fortnightly (14-day) records."""
    if df.empty:
        return df
    end = df.index[-1]
    age_days = (end - df.index).days
    bin_ids = pd.Series(age_days // bin_days, index=df.index)
    keep = ~bin_ids.duplicated(keep="last")
    return df[keep.values]


def _last_per_calendar_month(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the last bar of each calendar month (month-end close, or the
    latest bar for the current month)."""
    if df.empty:
        return df
    month_keys = pd.Series(df.index.strftime("%Y-%m"), index=df.index)
    keep = ~month_keys.duplicated(keep="last")
    return df[keep.values]


def _last_24h(df: pd.DataFrame) -> pd.DataFrame:
    """Rolling 24h window; if the market has been closed longer than that
    (long weekend) return the most recent 24 bars instead of nothing."""
    if df.empty:
        return df
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    windowed = df[df.index >= cutoff]
    return windowed if not windowed.empty else df.tail(24)


def _to_points(df: pd.DataFrame, intraday: bool) -> List[dict]:
    """[{datetime, price}] using the Close of each bar. Intraday bars carry
    a real time -> full ISO-8601 with exchange UTC offset; daily-or-coarser
    bars only know the date -> YYYY-MM-DD. NaN closes are skipped."""
    points = []
    for ts, close in df["Close"].items():
        if pd.isna(close):
            continue
        points.append(
            {
                "datetime": ts.isoformat() if intraday else ts.date().isoformat(),
                "price": round(float(close), 4),
            }
        )
    return points


def _format_occurrence(ts: pd.Timestamp, intraday: bool) -> str:
    return ts.isoformat() if intraday else ts.date().isoformat()


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
# period -> (record granularity label, downsampler)
_HISTORIC_SPEC = {
    Period.WEEK: ("1d", None),
    Period.MONTH: ("1d", None),
    Period.HALF_YEAR: ("2wk", lambda df: _last_per_bin(df, 14)),
    Period.YEAR: ("2wk", lambda df: _last_per_bin(df, 14)),
    Period.FIVE_YEAR: ("1mo", _last_per_calendar_month),
}


def _historic_points(ticker: str, period: Period) -> Tuple[List[dict], str, str]:
    """Return (points, interval_label, yahoo_symbol) for the rolling window
    ending now."""
    if period == Period.DAY:
        df = _upstream(lambda: _fetch_hourly_frame(ticker))
        return _to_points(_last_24h(df), intraday=True), "1h", _yahoo_symbol(df, ticker)

    interval_label, downsample = _HISTORIC_SPEC[period]
    df = _upstream(lambda: _fetch_daily_frame(ticker, period))
    if downsample is not None and not df.empty:
        df = downsample(df)
    return _to_points(df, intraday=False), interval_label, _yahoo_symbol(df, ticker)


def _historic_points_ending_on(ticker: str, period: Period, reference_date: date) -> Tuple[List[dict], str, str]:
    """Return (points, interval_label, yahoo_symbol) for the window of
    `period`'s length that ENDS on `reference_date` (inclusive): one chart
    call for exactly that window - hourly bars for DAY, daily bars
    otherwise - then the same downsampling as the rolling window."""
    days = _LOOKBACK_DAYS[period]
    first_day, day_after = _window_bounds(reference_date, days)
    if period == Period.DAY:
        df = _upstream(lambda: _fetch_window(ticker, first_day, day_after, HOURLY_INTERVAL))
        return _to_points(_slice_window(df, reference_date, days), intraday=True), "1h", _yahoo_symbol(df, ticker)

    interval_label, downsample = _HISTORIC_SPEC[period]
    df = _upstream(lambda: _fetch_window(ticker, first_day, day_after, DAILY_INTERVAL))
    df = _slice_window(df, reference_date, days)
    if downsample is not None and not df.empty:
        df = downsample(df)
    return _to_points(df, intraday=False), interval_label, _yahoo_symbol(df, ticker)


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


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


def _validate_reference_date(reference_date: date, period: Period) -> None:
    """Reject windows that can never have data BEFORE spending upstream
    budget: future dates, and DAY windows older than Yahoo's hourly
    retention."""
    today = _today_utc()
    # Exchanges east of UTC are already on "tomorrow" for part of every UTC
    # day, so their current local date must still be accepted: allow one
    # day of slack (no exchange is more than 14h ahead of UTC).
    if reference_date > today + timedelta(days=1):
        raise HTTPException(
            status_code=422,
            detail=f"referenceDate {reference_date.isoformat()} is in the future (today is {today.isoformat()} UTC).",
        )
    if period == Period.DAY and reference_date < today - timedelta(days=HOURLY_RETENTION_DAYS):
        raise HTTPException(
            status_code=422,
            detail=(
                f"referenceDate {reference_date.isoformat()} is too old for period DAY: Yahoo Finance keeps hourly "
                f"bars for ~{HOURLY_RETENTION_DAYS} days only. Use a more recent referenceDate or a daily period "
                "(WEEK, MONTH...)."
            ),
        )


@router.get("/{ticker}/historic")
def get_historic_prices(
    ticker: str,
    period: Period = Query(Period.DAY, description="Look-back window. Case-insensitive. Default DAY."),
    reference_date: Optional[str] = Query(
        None,
        alias="referenceDate",
        description=(
            "Optional end date(s) of the window, YYYY-MM-DD, inclusive: one date (2026-10-23) or up to "
            f"{MAX_REFERENCE_DATES} unique dates separated by commas (2026-10-23,2026-10-16). The window is the "
            "length of `period` ENDING on each date (exchange-local calendar days) instead of ending now, and the "
            "response is keyed by date. Dates must not be in the future; for DAY they must be within Yahoo's "
            "~730-day hourly retention."
        ),
    ),
):
    """Price series for `ticker` over the chosen period.

    | period    | window        | one record per            | `datetime` format          |
    |-----------|---------------|---------------------------|----------------------------|
    | DAY       | last 24 hours | hour (hourly bar)         | ISO-8601 w/ exchange offset|
    | WEEK      | last 7 days   | trading day               | YYYY-MM-DD                 |
    | MONTH     | last 30 days  | trading day               | YYYY-MM-DD                 |
    | HALF_YEAR | last 6 months | fortnight (14-day bin)    | YYYY-MM-DD                 |
    | YEAR      | last 12 months| fortnight (14-day bin)    | YYYY-MM-DD                 |
    | FIVE_YEAR | last 5 years  | calendar month            | YYYY-MM-DD                 |

    Response (rolling window)::

        {
          "ticker": "AAPL",
          "symbol": "AAPL",
          "period": "DAY",
          "interval": "1h",
          "count": 7,
          "data": [ {"datetime": "2024-05-01T09:30:00-04:00", "price": 172.34}, ... ]
        }

    With `referenceDate=YYYY-MM-DD[,YYYY-MM-DD...]` (up to 5 unique dates)
    the same window length ENDS on each date (inclusive) instead of now:
    DAY -> that trading date's hourly bars, WEEK -> the 7 calendar days up
    to and including it, and so on. Window bounds are calendar days in
    the exchange's time zone. The response is then keyed by date, in the
    order given::

        {
          "ticker": "BRK.B",
          "symbol": "BRK-B",
          "period": "WEEK",
          "interval": "1d",
          "results": {
            "2024-05-07": {"count": 5, "data": [ {"datetime": "2024-05-01", "price": 172.34}, ... ]},
            "2024-04-30": {"count": 5, "data": [ ... ]}
          }
        }

    A date with no bars (market closed) is reported as `count: 0,
    data: []`; the request is 404 only if every date is empty.

    `ticker` echoes the request; `symbol` is the symbol Yahoo actually
    answered for - they differ only for US share classes typed with a dot
    (BRK.B -> BRK-B), which are retried in Yahoo's dash form when the
    dotted form is unknown (same rule as the snapshot endpoint).

    * `price` is the bar's Close. For fortnight/month records it is the
      close of the last trading day in that bin, and `datetime` is that
      day; bins are anchored at the newest bar so the final record is
      always the latest close (of the window).
    * DAY without referenceDate: if the market has been closed for more
      than 24h (long weekend) the most recent 24 hourly bars are returned
      instead of an empty list. With referenceDate, a date the market was
      closed is simply 404 (there is nothing to show for that day).
    * Upstream cost: one live chart call per request, or one per reference
      date (DAY may need a second one on a long weekend; dotted symbols may
      be retried once). Nothing is cached. Invalid input (malformed or
      duplicate dates, more than 5, future dates, DAY beyond retention) is
      rejected with 422 before any upstream call.
    """
    ticker = ticker.upper()
    if reference_date is None:
        points, interval_label, yahoo_symbol = _historic_points(ticker, period)
        if not points:
            raise HTTPException(
                status_code=404, detail=f"No price data found for {ticker} over period {period.value}."
            )
        return {
            "ticker": ticker,
            "symbol": yahoo_symbol,
            "period": period.value,
            "interval": interval_label,
            "count": len(points),
            "data": points,
        }

    dates = _parse_reference_dates(reference_date)
    for d in dates:
        _validate_reference_date(d, period)

    results = {}
    yahoo_symbol = ticker
    interval_label = ""
    for d in dates:
        points, interval_label, symbol_used = _historic_points_ending_on(ticker, period, d)
        if points:
            yahoo_symbol = symbol_used
        results[d.isoformat()] = {"count": len(points), "data": points}
    if not any(r["count"] for r in results.values()):
        listed = ", ".join(d.isoformat() for d in dates)
        raise HTTPException(
            status_code=404,
            detail=f"No price data found for {ticker} over period {period.value} ending on {listed}.",
        )
    return {
        "ticker": ticker,
        "symbol": yahoo_symbol,
        "period": period.value,
        "interval": interval_label,
        "results": results,
    }


# ==========================================================================
# GET /stock/{symbol}/highlow
# ==========================================================================
def _load_highlow_frame(ticker: str, period: Period) -> Tuple[pd.DataFrame, bool]:
    """(frame, is_intraday) with the bars needed for `period`."""
    if period == Period.DAY:
        return _upstream(lambda: _fetch_last_session_frame(ticker)), True
    if period == Period.WEEK:
        return _upstream(lambda: _fetch_week_intraday_frame(ticker)), True
    return _upstream(lambda: _fetch_daily_frame(ticker, period)), False


def _compute_extremes(df: pd.DataFrame) -> Optional[dict]:
    """Max of High and min of Low with the bar timestamp each occurred in.
    NaN bars are ignored. None if there is nothing usable."""
    if df.empty or "High" not in df.columns or "Low" not in df.columns:
        return None
    highs = df["High"].dropna()
    lows = df["Low"].dropna()
    if highs.empty or lows.empty:
        return None
    high_ts = highs.idxmax()
    low_ts = lows.idxmin()
    return {
        "high": round(float(highs.loc[high_ts]), 4),
        "high_ts": high_ts,
        "low": round(float(lows.loc[low_ts]), 4),
        "low_ts": low_ts,
    }


@router.get("/{ticker}/highlow")
def get_high_low(
    ticker: str,
    period: Period = Query(Period.WEEK, description="Look-back window. Case-insensitive. Default WEEK."),
):
    """Highest and lowest traded price for `ticker` over the chosen period,
    with when each occurred.

    Response::

        {
          "symbol": "AAPL",
          "period": "WEEK",
          "high": 195.12,
          "highOccured": "2024-05-01T10:00:00-04:00",
          "low": 188.4,
          "lowOccured": "2024-04-29T15:00:00-04:00"
        }

    * `high`/`low` are the High/Low of the bar in which they occurred (the
      true traded extreme, not the close).
    * `highOccured`/`lowOccured` precision follows the bar size: DAY ->
      5-minute bar, WEEK -> hourly bar (both ISO-8601 with exchange UTC
      offset); MONTH/HALF_YEAR/YEAR/FIVE_YEAR -> date (YYYY-MM-DD).
    * Upstream cost: one live chart call per request (DAY may need a
      second one on a weekend; dotted symbols may be retried once).
      Nothing is cached.
    """
    ticker = ticker.upper()
    df, intraday = _load_highlow_frame(ticker, period)
    extremes = _compute_extremes(df)
    if extremes is None:
        raise HTTPException(status_code=404, detail=f"No price data found for {ticker} over period {period.value}.")
    return {
        "symbol": ticker,
        "period": period.value,
        "high": extremes["high"],
        "highOccured": _format_occurrence(extremes["high_ts"], intraday),
        "low": extremes["low"],
        "lowOccured": _format_occurrence(extremes["low_ts"], intraday),
    }


# ==========================================================================
# Upstream budget (read-only)
# ==========================================================================
def upstream_budget() -> dict:
    """How much of the hourly upstream budget is left; used by /status."""
    return {"per_hour": UPSTREAM_CALL_BUDGET_PER_HOUR, "remaining": _limiter.remaining()}
