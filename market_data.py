"""
Access layer between the API and yfinance / Yahoo Finance.

Why this module exists
----------------------
Yahoo's free endpoint tolerates roughly 2000 requests per rolling hour per
IP before answering HTTP 429. yfinance itself does *nothing* to protect you
from that: every ``Ticker.history()`` is one HTTP request (two for a ticker
the process has never seen, because of a timezone lookup), ``Ticker.info``
is two requests plus a one-off cookie/crumb bootstrap, and ``yf.download()``
with N tickers is simply N ``history()`` calls run in threads.

So this module provides three things every endpoint should go through:

* ``TTLCache``      - thread-safe in-memory cache with a "fresh" TTL and a
                      longer "stale" retention used as a fallback.
* ``RequestBudget`` - sliding-window counter of outbound Yahoo requests so we
                      stop *before* Yahoo starts rejecting us, plus a cooldown
                      that is armed if Yahoo does return 429.
* ``fetch()``       - single-flight fetch: concurrent requests for the same
                      key share one Yahoo call; fresh cache wins, then Yahoo,
                      then stale cache, then an explicit "try later" error.

Configuration is via environment variables (see ``Settings``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, Generic, List, Optional, TypeVar

import pandas as pd
import yfinance as yf
from yfinance.exceptions import (
    YFInvalidPeriodError,
    YFPricesMissingError,
    YFRateLimitError,
    YFTickerMissingError,
)

logger = logging.getLogger(__name__)

# Make yfinance raise instead of logging-and-returning-empty-DataFrame. This
# lets us tell "ticker does not exist" apart from "no trades in this window",
# and it disables yfinance's fallback of calling the expensive ``.info``
# (2 requests) when a timezone lookup fails.
yf.config.debug.hide_exceptions = False

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer value for %s=%r, using %d", name, raw, default)
        return default


@dataclass(frozen=True)
class Settings:
    # Yahoo's observed free-tier ceiling. Kept only as documentation/reference.
    yahoo_hard_limit_per_hour: int = 2000
    # Our own budget. Default is 75% of the hard limit: headroom for yfinance's
    # hidden extra requests (tz lookup, crumb bootstrap, internal retries) and
    # for anything else sharing this machine's IP.
    request_budget_per_hour: int = _env_int("YAHOO_REQUEST_BUDGET", 1500)
    budget_window_seconds: int = 3600
    # How long an hourly-price response is considered fresh. Hourly bars only
    # change once an hour (except the in-progress bar), so a few minutes is a
    # good trade-off between freshness and request count.
    hourly_cache_ttl_seconds: int = _env_int("HOURLY_CACHE_TTL", 300)
    # Other endpoints (info / daily history) change even less often.
    info_cache_ttl_seconds: int = _env_int("INFO_CACHE_TTL", 900)
    history_cache_ttl_seconds: int = _env_int("HISTORY_CACHE_TTL", 900)
    # Stale entries are kept this long and served only when we cannot go to
    # Yahoo (budget exhausted or Yahoo returned 429).
    stale_max_age_seconds: int = _env_int("STALE_MAX_AGE", 6 * 3600)
    # If Yahoo answers 429 we stop calling it for this long.
    rate_limit_cooldown_seconds: int = _env_int("RATE_LIMIT_COOLDOWN", 300)
    # "Ticker not found" / "no data" answers are remembered this long so a
    # client polling a bad symbol cannot drain the budget (each miss costs
    # Yahoo 1-2 requests because of yfinance's timezone lookup).
    negative_cache_ttl_seconds: int = _env_int("NEGATIVE_CACHE_TTL", 120)
    # Network timeout passed to yfinance.
    yahoo_timeout_seconds: int = _env_int("YAHOO_TIMEOUT", 10)


settings = Settings()


# --------------------------------------------------------------------------- #
# Errors surfaced to the router
# --------------------------------------------------------------------------- #
class MarketDataError(Exception):
    """Base class for errors the router maps to HTTP responses."""


class TickerNotFound(MarketDataError):
    pass


class NoPriceData(MarketDataError):
    pass


class InvalidRequest(MarketDataError):
    pass


class UpstreamUnavailable(MarketDataError):
    """Yahoo could not be reached or returned garbage."""


class BudgetExhausted(MarketDataError):
    """We refuse to call Yahoo right now; ``retry_after`` is in seconds."""

    def __init__(self, retry_after: int, reason: str):
        super().__init__(reason)
        self.retry_after = max(1, int(retry_after))
        self.reason = reason


# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #
@dataclass
class CacheEntry(Generic[T]):
    value: T
    fetched_at: float
    ttl: float

    def is_fresh(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now - self.fetched_at < self.ttl

    def age(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return now - self.fetched_at


class TTLCache:
    """Small thread-safe dict cache. Entries past ``stale_max_age`` are evicted."""

    def __init__(self, stale_max_age: float):
        self._data: Dict[Any, CacheEntry] = {}
        self._lock = threading.Lock()
        self._stale_max_age = stale_max_age

    def get(self, key: Any) -> Optional[CacheEntry]:
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.age(now) > self._stale_max_age:
                del self._data[key]
                return None
            return entry

    def set(self, key: Any, value: Any, ttl: float) -> CacheEntry:
        entry = CacheEntry(value=value, fetched_at=time.time(), ttl=ttl)
        with self._lock:
            self._data[key] = entry
        return entry

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# --------------------------------------------------------------------------- #
# Request budget
# --------------------------------------------------------------------------- #
class RequestBudget:
    """
    Sliding-window counter of outbound Yahoo requests.

    ``cost`` lets callers account for yfinance operations that issue more than
    one HTTP request (e.g. ``.info`` = 2).
    """

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._events: Deque[tuple[float, int]] = deque()  # (timestamp, cost)
        self._used = 0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] <= cutoff:
            _, cost = self._events.popleft()
            self._used -= cost

    def try_acquire(self, cost: int = 1) -> bool:
        now = time.time()
        with self._lock:
            if now < self._blocked_until:
                return False
            self._prune(now)
            if self._used + cost > self.limit:
                return False
            self._events.append((now, cost))
            self._used += cost
            return True

    def block_for(self, seconds: float) -> None:
        """Arm a cooldown, e.g. after Yahoo returned HTTP 429."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.time() + seconds)

    def retry_after(self) -> int:
        """Seconds until at least one unit of budget is available again."""
        now = time.time()
        with self._lock:
            waits: List[float] = []
            if now < self._blocked_until:
                waits.append(self._blocked_until - now)
            self._prune(now)
            if self._used >= self.limit and self._events:
                waits.append(self._events[0][0] + self.window - now)
            return max(1, int(max(waits) + 0.999)) if waits else 1

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._prune(now)
            return {
                "limit_per_window": self.limit,
                "window_seconds": self.window,
                "used": self._used,
                "remaining": max(0, self.limit - self._used),
                "cooldown_seconds_left": max(0, int(self._blocked_until - now)),
            }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._used = 0
            self._blocked_until = 0.0


# --------------------------------------------------------------------------- #
# Fetch coordinator (single-flight + cache + budget)
# --------------------------------------------------------------------------- #
@dataclass
class FetchResult(Generic[T]):
    value: T
    source: str  # "yahoo" | "cache" | "stale"
    fetched_at: float

    @property
    def fetched_at_iso(self) -> str:
        return datetime.fromtimestamp(self.fetched_at, tz=timezone.utc).isoformat(timespec="seconds")


class MarketDataService:
    def __init__(self, settings: Settings = settings):
        self.settings = settings
        self.cache = TTLCache(stale_max_age=settings.stale_max_age_seconds)
        self.budget = RequestBudget(
            limit=settings.request_budget_per_hour,
            window_seconds=settings.budget_window_seconds,
        )
        self._key_locks: Dict[Any, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()

    # -- plumbing ---------------------------------------------------------- #
    def _lock_for(self, key: Any) -> threading.Lock:
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def _usable_entry(self, key: Any) -> Optional[CacheEntry]:
        """Cache entry for ``key``; expired negative (error) entries count as absent."""
        entry = self.cache.get(key)
        if entry is not None and isinstance(entry.value, MarketDataError) and not entry.is_fresh():
            return None
        return entry

    @staticmethod
    def _from_entry(entry: CacheEntry, source: str) -> FetchResult:
        if isinstance(entry.value, MarketDataError):
            raise entry.value
        return FetchResult(entry.value, source, entry.fetched_at)

    def fetch(self, key: Any, ttl: float, cost: int, loader: Callable[[], T]) -> FetchResult[T]:
        """
        Return ``loader()``'s result, going to Yahoo at most once per ``ttl``
        per key, and never when the budget says no.
        """
        entry = self._usable_entry(key)
        if entry is not None and entry.is_fresh():
            return self._from_entry(entry, "cache")

        with self._lock_for(key):
            # Another thread may have refreshed while we waited for the lock.
            entry = self._usable_entry(key)
            if entry is not None and entry.is_fresh():
                return self._from_entry(entry, "cache")

            if not self.budget.try_acquire(cost):
                if entry is not None:
                    logger.warning("Budget exhausted; serving stale %s (age %.0fs)", key, entry.age())
                    return self._from_entry(entry, "stale")
                raise BudgetExhausted(
                    self.budget.retry_after(),
                    "Outbound request budget to Yahoo Finance is exhausted and no cached data is available.",
                )

            try:
                value = loader()
            except (TickerNotFound, NoPriceData) as exc:
                # Negative cache: remember the miss briefly, never serve it as "stale".
                self.cache.set(key, exc, self.settings.negative_cache_ttl_seconds)
                raise
            except YFRateLimitError:
                self.budget.block_for(self.settings.rate_limit_cooldown_seconds)
                logger.error("Yahoo returned 429; pausing outbound requests for %ss",
                             self.settings.rate_limit_cooldown_seconds)
                if entry is not None:
                    return self._from_entry(entry, "stale")
                raise BudgetExhausted(
                    self.budget.retry_after(),
                    "Yahoo Finance is rate limiting this server and no cached data is available.",
                )

            entry = self.cache.set(key, value, ttl)
            return FetchResult(value, "yahoo", entry.fetched_at)

    # -- loaders ----------------------------------------------------------- #
    @staticmethod
    def _translate(exc: Exception) -> MarketDataError:
        """Map yfinance / network exceptions onto our error types."""
        if isinstance(exc, YFInvalidPeriodError):
            return InvalidRequest(str(exc))
        if isinstance(exc, YFPricesMissingError):
            return NoPriceData(str(exc))
        if isinstance(exc, YFTickerMissingError):
            return TickerNotFound(str(exc))
        if isinstance(exc, MarketDataError):
            return exc
        return UpstreamUnavailable(f"{type(exc).__name__}: {exc}")

    def get_intraday_bars(self, ticker: str, prepost: bool = False) -> FetchResult[pd.DataFrame]:
        """
        Hourly OHLC bars for the last 5 trading days (one chart request).

        We fetch 5 days rather than exactly 24h on purpose: it is the same
        single request, the payload is tiny (~35 bars), it lets us answer the
        "last 24h" question on a weekend with a useful last-known price, and
        the same cached frame can serve day-high/low style questions later.
        """
        key = ("bars", ticker, "1h", bool(prepost))

        def load() -> pd.DataFrame:
            try:
                tkr = yf.Ticker(ticker)
                df = tkr.history(
                    period="5d",
                    interval="1h",
                    prepost=prepost,
                    actions=False,
                    auto_adjust=True,
                    timeout=self.settings.yahoo_timeout_seconds,
                )
            except YFRateLimitError:
                raise  # handled by fetch(): arms the cooldown
            except Exception as exc:  # noqa: BLE001 - translated below
                raise self._translate(exc) from exc
            meta = tkr.history_metadata or {}
            df.attrs["currency"] = meta.get("currency")
            df.attrs["exchange_timezone"] = meta.get("exchangeTimezoneName")
            df.attrs["instrument_type"] = meta.get("instrumentType")
            return df

        return self.fetch(key, self.settings.hourly_cache_ttl_seconds, cost=1, loader=load)

    def get_history(self, ticker: str, period: str) -> FetchResult[pd.DataFrame]:
        key = ("history", ticker, period)

        def load() -> pd.DataFrame:
            try:
                return yf.Ticker(ticker).history(period=period, timeout=self.settings.yahoo_timeout_seconds)
            except YFRateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc

        return self.fetch(key, self.settings.history_cache_ttl_seconds, cost=1, loader=load)

    def get_info(self, ticker: str) -> FetchResult[dict]:
        key = ("info", ticker)

        def load() -> dict:
            try:
                info = yf.Ticker(ticker).info
            except YFRateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
            # yfinance returns a near-empty dict for unknown symbols.
            if not info or (info.get("quoteType") is None and info.get("regularMarketPrice") is None):
                raise TickerNotFound(f"{ticker}: no data found, symbol may be delisted")
            return info

        # quoteSummary + v7 quote = 2 HTTP requests.
        return self.fetch(key, self.settings.info_cache_ttl_seconds, cost=2, loader=load)


# --------------------------------------------------------------------------- #
# Pure helpers for shaping the hourly response
# --------------------------------------------------------------------------- #
def build_hourly_series(
    bars: pd.DataFrame,
    hours: int = 24,
    now: Optional[datetime] = None,
) -> dict:
    """
    Reduce a frame of hourly OHLC bars to ``[{datetime, price}, ...]`` for the
    trailing ``hours`` window ending at ``now`` (UTC by default).

    ``price`` is the bar's Close. Timestamps are returned in the exchange's
    local timezone (as yfinance provides them) in ISO-8601 with offset.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_start = now - timedelta(hours=hours)

    points: List[dict] = []
    high: Optional[float] = None
    low: Optional[float] = None
    last_available: Optional[dict] = None

    if not bars.empty and "Close" in bars.columns:
        df = bars.dropna(subset=["Close"])
        if not df.empty:
            idx = pd.DatetimeIndex(df.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            last_ts = idx[-1]
            last_available = {
                "datetime": last_ts.isoformat(),
                "price": float(df["Close"].iloc[-1]),
            }
            mask = (idx >= window_start) & (idx <= now)
            window = df.loc[mask]
            for ts, close in zip(idx[mask], window["Close"].tolist()):
                points.append({"datetime": ts.isoformat(), "price": float(close)})
            if not window.empty:
                # True extremes inside the window come from High/Low, not Close.
                high_col = window["High"] if "High" in window else window["Close"]
                low_col = window["Low"] if "Low" in window else window["Close"]
                high = float(high_col.max())
                low = float(low_col.min())

    return {
        "window": {
            "start": window_start.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds"),
            "hours": hours,
        },
        "count": len(points),
        "prices": points,
        "high": high,
        "low": low,
        "last_available": last_available,
    }


# Module-level singleton used by the routers.
service = MarketDataService()
