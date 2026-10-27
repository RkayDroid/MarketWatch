"""
Shared fixtures.

yfinance is patched at the `stock_router.yf.Ticker` seam with a fake that
returns DataFrames shaped exactly like the real library does:

* tz-aware DatetimeIndex named "Date" (interval >= 1d) or "Datetime"
  (intraday), as set in yfinance/scrapers/history.py
* columns Open/High/Low/Close (float64), Volume (int64), Dividends,
  Stock Splits (float64), optionally Capital Gains
* may contain NaN, as real Yahoo data does

This lets the full FastAPI request/response path (including JSON
rendering, which is where the NaN bug lived) run without network access.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Importing stock_router builds the real cache from env; point its disk
# tier at a throwaway location so test runs never touch ./.cache.
os.environ.setdefault("MWL_CACHE_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "mwl-test-import-cache"))

import stock_router  # noqa: E402
from caching import DiskLRUCache, MemoryLRUCache, TieredCache  # noqa: E402
from main import app  # noqa: E402

EXCHANGE_TZ = "America/New_York"


def make_ohlc_frame(
    *,
    periods: int,
    freq: str,
    end: Optional[pd.Timestamp] = None,
    intraday: bool = True,
    start_price: float = 100.0,
    with_nan_row: bool = False,
    extra_columns: Optional[dict] = None,
) -> pd.DataFrame:
    """Build a yfinance-shaped OHLC frame ending at `end` (default: now)."""
    if end is None:
        end = pd.Timestamp.now(tz=EXCHANGE_TZ).floor("h")
    idx = pd.date_range(end=end, periods=periods, freq=freq, tz=EXCHANGE_TZ)
    idx.name = "Datetime" if intraday else "Date"
    close = np.linspace(start_price, start_price + periods - 1, periods)
    df = pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.arange(1000, 1000 + periods, dtype=np.int64),
            "Dividends": np.zeros(periods),
            "Stock Splits": np.zeros(periods),
        },
        index=idx,
    )
    if extra_columns:
        for name, value in extra_columns.items():
            df[name] = value
    if with_nan_row:
        df.iloc[-1, df.columns.get_loc("Open")] = np.nan
    return df


def _empty_frame() -> pd.DataFrame:
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    empty.index.name = "Date"
    return empty


def slice_like_yahoo(df: pd.DataFrame, start, end) -> pd.DataFrame:
    """What yfinance returns for history(start=, end=): bars in
    [start, end) with naive dates taken as exchange-local midnight."""
    tz = df.index.tz
    lo = pd.Timestamp(start).tz_localize(tz)
    hi = pd.Timestamp(end).tz_localize(tz)
    return df[(df.index >= lo) & (df.index < hi)]


class FakeTicker:
    """Stand-in for yfinance.Ticker.

    Configure via class attributes on a per-test basis:
      * ``responses``: dict mapping (period, interval) -> DataFrame, or a
        callable(period, interval) -> DataFrame. Missing keys -> empty df.
      * ``window_responses``: callable(start, end, interval) -> DataFrame
        for ``history(start=, end=)`` calls (the referenceDate path).
        None -> empty df.
      * ``info_payload``: dict returned by ``.info``.
      * ``info_for``: per-symbol override, dict symbol -> info dict
        (symbols absent from it get an empty dict, i.e. "unknown").
      * ``raise_on_history``: exception instance raised by ``.history()``.
    ``calls`` records every ``.history()`` invocation for assertions.
    """

    responses = {}
    window_responses = None
    info_payload = {}
    info_for = None  # dict symbol -> info dict; None = use info_payload for all
    raise_on_history = None
    calls = []

    def __init__(self, symbol, session=None):
        self.symbol = symbol

    def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
        if start is not None or end is not None:
            # Like yfinance: start/end replace the Yahoo range.
            FakeTicker.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if FakeTicker.raise_on_history is not None:
                raise FakeTicker.raise_on_history
            if FakeTicker.window_responses is None:
                return _empty_frame()
            return FakeTicker.window_responses(start, end, interval)
        period = period or "1mo"
        FakeTicker.calls.append({"symbol": self.symbol, "period": period, "interval": interval})
        if FakeTicker.raise_on_history is not None:
            raise FakeTicker.raise_on_history
        responses = FakeTicker.responses
        if callable(responses):
            return responses(period, interval)
        df = responses.get((period, interval))
        if df is None:
            return _empty_frame()
        return df

    @property
    def info(self):
        if FakeTicker.info_for is not None:
            return FakeTicker.info_for.get(self.symbol, {})
        return FakeTicker.info_payload


def make_tiered_cache(tmp_path, *, ttl_seconds=24 * 3600, time_fn=None, memory_max=512, disk_max_bytes=256 * 1024 * 1024, name="cache.sqlite"):
    """A fresh two-tier cache whose disk file lives under tmp_path."""
    import time as _time

    time_fn = time_fn or _time.time
    return TieredCache(
        memory=MemoryLRUCache(max_entries=memory_max, ttl_seconds=ttl_seconds, time_fn=time_fn),
        disk=DiskLRUCache(path=str(tmp_path / name), max_bytes=disk_max_bytes, ttl_seconds=ttl_seconds, time_fn=time_fn),
    )


@pytest.fixture(autouse=True)
def fake_yfinance(monkeypatch, request, tmp_path):
    """Patch yfinance + reset cache/limiter state before every test.

    Tests marked ``@pytest.mark.live`` are NOT patched: they hit the real
    Yahoo Finance API and are skipped unless LIVE_YF=1 is set.
    """
    if "live" in request.keywords:
        stock_router._cache.clear()
        stock_router._limiter._calls.clear()
        yield None
        return

    # A fresh two-tier cache per test (disk tier in tmp_path): isolation.
    cache = make_tiered_cache(tmp_path)
    monkeypatch.setattr(stock_router, "_cache", cache)

    FakeTicker.responses = {}
    FakeTicker.window_responses = None
    FakeTicker.info_payload = {}
    FakeTicker.info_for = None
    FakeTicker.raise_on_history = None
    FakeTicker.calls = []
    monkeypatch.setattr(stock_router.yf, "Ticker", FakeTicker)

    stock_router._cache.clear()
    stock_router._limiter._calls.clear()
    monkeypatch.setattr(stock_router._limiter, "max_calls", stock_router.UPSTREAM_CALL_BUDGET_PER_HOUR)
    yield FakeTicker
    cache.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)


@pytest.fixture
def hourly_30_bars():
    """30 consecutive hourly bars ending now -> 24 fall inside the last 24h."""
    return make_ohlc_frame(periods=30, freq="h", intraday=True)


@pytest.fixture
def stale_hourly_bars():
    """Hourly bars whose newest bar is 3 days old (e.g. long weekend)."""
    end = pd.Timestamp.now(tz=EXCHANGE_TZ).floor("h") - timedelta(days=3)
    return make_ohlc_frame(periods=40, freq="h", end=end, intraday=True)
