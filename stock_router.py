"""
Stock endpoints.

    GET /stock/{symbol}                     snapshot: name, sector, price, exchange...
    GET /stock/{symbol}/historic?period=    price series (hourly / daily / fortnightly / monthly)
    GET /stock/{symbol}/highlow?period=     highest & lowest traded price and when

All Yahoo Finance access goes through one cache + one rate limiter (see
`rate_limit.py`) so the whole app stays under yfinance's informal free-tier
budget of ~2000 requests/hour. See README.md for the per-endpoint upstream
cost table.
"""

import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, Optional, Tuple

import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException, Query, Response
from yfinance.exceptions import YFException, YFRateLimitError

from caching import get_or_fetch, make_cache_from_env
from rate_limit import RateLimitExceeded, SlidingWindowRateLimiter

router = APIRouter(prefix="/stock", tags=["Stock Data"])
cache_router = APIRouter(prefix="/cache", tags=["Cache Admin"])

# --------------------------------------------------------------------------
# Rate limiting / caching configuration
#
# yfinance talks to Yahoo Finance's free, unauthenticated endpoints, which
# are informally capped around 2000 requests/hour per source IP. Every
# upstream call made by this module is funneled through:
#
#   1. A two-tier cache (caching.py): memory first, then a disk tier that
#      survives restarts; write-through; each tier bounded and LRU-evicted.
#      Every response says where it came from: X-Cache: MEMORY | DISK | MISS.
#
#      Two TTLs, chosen per cached source frame:
#        SHORT (5 min, MWL_CACHE_SHORT_TTL_SECONDS) for "as of now" data:
#          the snapshot, and the frames that only serve period=DAY
#          (hourly bars for /historic, 5-minute session bars for /highlow).
#        LONG  (1 day, MWL_CACHE_TTL_SECONDS) for everything else: the
#          5-day hourly frame (/highlow WEEK) and the shared 5-year daily
#          frame (/historic WEEK..FIVE_YEAR, /highlow MONTH..FIVE_YEAR).
#
#   2. A sliding-window rate limiter (UPSTREAM_CALL_BUDGET_PER_HOUR) that
#      hard-caps real upstream calls in any trailing 60 minutes, leaving
#      headroom under Yahoo's ceiling for other traffic on the same IP.
#
# Worst case per symbol: the three short-lived frames cost at most 12
# calls/hour each (4 requests: hourly, session, .info x2 = 48/hour), the
# long-lived ones at most 2/day.
# --------------------------------------------------------------------------
UPSTREAM_CALL_BUDGET_PER_HOUR = 1800  # 10% safety margin under the 2000/hr informal cap

_cache = make_cache_from_env()
_limiter = SlidingWindowRateLimiter(max_calls=UPSTREAM_CALL_BUDGET_PER_HOUR, window_seconds=3600)

SHORT_TTL_SECONDS = float(os.environ.get("MWL_CACHE_SHORT_TTL_SECONDS", 300))  # 5 minutes
LONG_TTL_SECONDS = _cache.ttl_seconds  # MWL_CACHE_TTL_SECONDS, default 1 day


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


# Calendar-day look-back used to slice the shared daily frame.
_LOOKBACK_DAYS = {
    Period.WEEK: 7,
    Period.MONTH: 30,
    Period.HALF_YEAR: 182,
    Period.YEAR: 365,
    Period.FIVE_YEAR: 5 * 365 + 2,
}

# --------------------------------------------------------------------------
# Source frames (each = exactly one Yahoo chart call per ticker, cached)
#
#   hourly   2d @ 60m  (fallback 5d)   -> /historic DAY
#   session  1d @ 5m   (fallback 5d)   -> /highlow DAY
#   week     5d @ 60m                  -> /highlow WEEK
#   daily5y  5y @ 1d                   -> /historic WEEK..FIVE_YEAR and
#                                         /highlow MONTH..FIVE_YEAR
#
# Yahoo's chart endpoint accepts ONE symbol per request and retains
# intraday bars for only ~60 days, so daily bars are the only option for
# the longer windows anyway. Fetching the longest daily window once and
# slicing/downsampling it locally means nine different period views of a
# ticker share a single upstream request.
# --------------------------------------------------------------------------
HOURLY_INTERVAL = "60m"
PRIMARY_HOURLY_PERIOD = "2d"
FALLBACK_PERIOD = "5d"
SESSION_INTERVAL = "5m"
DAILY_RANGE = ("5y", "1d")


def _run_upstream(key: str, fetch_fn: Callable[[], object], ttl_seconds: float):
    """Execute `fetch_fn` through the two-tier cache, storing the result for
    `ttl_seconds`, and normalise failure modes (limiter exhaustion,
    yfinance errors, anything unexpected) into HTTP errors. `fetch_fn`
    must call `_limiter.try_acquire()` before each real HTTP request.

    Returns (value, cache_status) with cache_status in MEMORY | DISK | MISS.
    """
    try:
        return get_or_fetch(_cache, key, fetch_fn, ttl_seconds=ttl_seconds)
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
    """Single yfinance chart call, charged to the rate limiter.

    Dotted symbols: Yahoo uses dots for exchange suffixes (SAP.DE, 0700.HK,
    BARC.L) - those work as-is - but expects a DASH for US share classes
    (BRK-B), which most people type with a dot (BRK.B). If a dotted symbol
    returns nothing, retry once with dots replaced by dashes; the retry is
    a second, separately charged upstream call.
    """
    _limiter.try_acquire()
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.empty and "." in ticker:
        _limiter.try_acquire()
        df = yf.Ticker(ticker.replace(".", "-")).history(period=period, interval=interval, auto_adjust=True)
    return df


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


def _get_hourly_frame(ticker: str) -> Tuple[pd.DataFrame, str]:
    """Only serves period=DAY -> short TTL."""
    return _run_upstream(f"src:{ticker}:hourly", lambda: _fetch_hourly_frame(ticker), SHORT_TTL_SECONDS)


def _get_session_frame(ticker: str) -> Tuple[pd.DataFrame, str]:
    """Only serves period=DAY -> short TTL."""
    return _run_upstream(f"src:{ticker}:session", lambda: _fetch_last_session_frame(ticker), SHORT_TTL_SECONDS)


def _get_week_intraday_frame(ticker: str) -> Tuple[pd.DataFrame, str]:
    return _run_upstream(
        f"src:{ticker}:5d:60m", lambda: _fetch_history(ticker, "5d", HOURLY_INTERVAL), LONG_TTL_SECONDS
    )


def _get_daily_frame(ticker: str) -> Tuple[pd.DataFrame, str]:
    """The shared 5-year daily frame (~1260 rows)."""
    yf_period, interval = DAILY_RANGE
    return _run_upstream(
        f"src:{ticker}:daily5y", lambda: _fetch_history(ticker, yf_period, interval), LONG_TTL_SECONDS
    )


def _slice_lookback(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return df
    cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=days)
    return df[df.index >= cutoff]


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
def get_stock_snapshot(ticker: str, response: Response):
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
    * Upstream cost: 2 requests (yfinance `.info` = quoteSummary + quote),
      cached for the SHORT TTL (5 min). Cache tier in the `X-Cache` header.
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

    snapshot, cache_status = _run_upstream(f"snapshot:{ticker}", fetch, SHORT_TTL_SECONDS)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} not found.")
    response.headers["X-Cache"] = cache_status
    return snapshot


# ==========================================================================
# GET /stock/{symbol}/historic
# ==========================================================================
# period -> (record granularity label, lookback days, downsampler, intraday?)
_HISTORIC_SPEC = {
    Period.WEEK: ("1d", 7, None),
    Period.MONTH: ("1d", 30, None),
    Period.HALF_YEAR: ("2wk", 182, lambda df: _last_per_bin(df, 14)),
    Period.YEAR: ("2wk", 365, lambda df: _last_per_bin(df, 14)),
    Period.FIVE_YEAR: ("1mo", _LOOKBACK_DAYS[Period.FIVE_YEAR], _last_per_calendar_month),
}


def _historic_points(ticker: str, period: Period) -> Tuple[List[dict], str, str]:
    """Return (points, interval_label, cache_status) for the period."""
    if period == Period.DAY:
        df, cache_status = _get_hourly_frame(ticker)
        return _to_points(_last_24h(df), intraday=True), "1h", cache_status

    interval_label, lookback_days, downsample = _HISTORIC_SPEC[period]
    df, cache_status = _get_daily_frame(ticker)
    df = _slice_lookback(df, lookback_days)
    if downsample is not None and not df.empty:
        df = downsample(df)
    return _to_points(df, intraday=False), interval_label, cache_status


@router.get("/{ticker}/historic")
def get_historic_prices(
    ticker: str,
    response: Response,
    period: Period = Query(Period.DAY, description="Look-back window. Case-insensitive. Default DAY."),
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

    Response::

        {
          "ticker": "AAPL",
          "period": "DAY",
          "interval": "1h",
          "count": 7,
          "cached": false,
          "data": [ {"datetime": "2024-05-01T09:30:00-04:00", "price": 172.34}, ... ]
        }

    * `price` is the bar's Close. For fortnight/month records it is the
      close of the last trading day in that bin, and `datetime` is that
      day; bins are anchored at the newest bar so the final record is
      always the latest close.
    * DAY: if the market has been closed for more than 24h (long weekend)
      the most recent 24 hourly bars are returned instead of an empty list.
    * Upstream cost: DAY = one hourly call cached for the SHORT TTL
      (5 min); WEEK..FIVE_YEAR all share one 5-year daily call per ticker
      cached for the LONG TTL (1 day), also shared with /highlow.
    """
    ticker = ticker.upper()
    points, interval_label, cache_status = _historic_points(ticker, period)
    if not points:
        raise HTTPException(status_code=404, detail=f"No price data found for {ticker} over period {period.value}.")
    response.headers["X-Cache"] = cache_status
    return {
        "ticker": ticker,
        "period": period.value,
        "interval": interval_label,
        "count": len(points),
        "cached": cache_status != "MISS",
        "data": points,
    }


# ==========================================================================
# GET /stock/{symbol}/highlow
# ==========================================================================
def _load_highlow_frame(ticker: str, period: Period) -> Tuple[pd.DataFrame, str, bool]:
    """(frame, cache_status, is_intraday) with the bars needed for `period`."""
    if period == Period.DAY:
        df, cache_status = _get_session_frame(ticker)
        return df, cache_status, True
    if period == Period.WEEK:
        df, cache_status = _get_week_intraday_frame(ticker)
        return df, cache_status, True
    df, cache_status = _get_daily_frame(ticker)
    return _slice_lookback(df, _LOOKBACK_DAYS[period]), cache_status, False


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
    response: Response,
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
    * Upstream cost: DAY = one session call cached for the SHORT TTL
      (5 min); WEEK = one call and MONTH..FIVE_YEAR share the 5-year daily
      frame, both cached for the LONG TTL (1 day; daily frame also used by
      /historic).
    """
    ticker = ticker.upper()
    df, cache_status, intraday = _load_highlow_frame(ticker, period)
    extremes = _compute_extremes(df)
    if extremes is None:
        raise HTTPException(status_code=404, detail=f"No price data found for {ticker} over period {period.value}.")
    response.headers["X-Cache"] = cache_status
    return {
        "symbol": ticker,
        "period": period.value,
        "high": extremes["high"],
        "highOccured": _format_occurrence(extremes["high_ts"], intraday),
        "low": extremes["low"],
        "lowOccured": _format_occurrence(extremes["low_ts"], intraday),
    }


# ==========================================================================
# Cache administration
# ==========================================================================
@cache_router.delete("")
def clear_cache():
    """Drop every cached entry from BOTH tiers (memory and disk). The next
    request for anything will be a live upstream call again."""
    cleared = _cache.clear()
    return {"cleared": cleared, **_cache.stats()}


@cache_router.get("/stats")
def cache_stats():
    """Per-tier entry counts and bounds, the TTL, and how much of the
    hourly upstream budget is left. Expired entries are purged first so
    the counts reflect live data."""
    purged = _cache.purge_expired()
    stats = _cache.stats()
    stats.pop("ttl_seconds", None)
    return {
        "short_ttl_seconds": SHORT_TTL_SECONDS,
        "long_ttl_seconds": LONG_TTL_SECONDS,
        **stats,
        "purged_now": purged,
        "upstream_budget_remaining": _limiter.remaining(),
    }
