"""
Shared fixtures.

yfinance is patched at the `stock_router.yf.Ticker` seam with a fake that
answers `history(start=, end=, interval=)` from a synthetic exchange
(`FakeMarket`) whose frames are shaped exactly like the real library's:

* tz-aware DatetimeIndex named "Date" (daily) or "Datetime" (intraday) in
  the exchange's zone, as set in yfinance/scrapers/history.py
* columns Open/High/Low/Close (float64), Volume (int64), Dividends,
  Stock Splits (float64)
* intraday bars anchored on the session open (09:30, 10:30, ... like
  Yahoo's hourly bars), daily bars stamped at local midnight
* `[start, end)` with bare dates taken as exchange-local midnight

The market has weekday sessions minus a known set of holidays, and a
clock (`FakeMarket.now`) that decides which bars "exist yet": the daily
bar for today appears together with the first intraday bar, as on Yahoo.
Both bar sizes are derived from one session list and one price path,
so hourly and daily bars agree exactly.
"""

import sys
from functools import lru_cache
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stock_router  # noqa: E402
import stores  # noqa: E402
from main import app  # noqa: E402

EXCHANGE_TZ = "America/New_York"
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
TODAY = date.today()


def _weekday_on_or_before(d: date, weekday: int) -> date:
    return d - timedelta(days=(d.weekday() - weekday) % 7)


# Holidays of the default market, placed relative to today so the tests
# are stable whatever the calendar says:
#   * HOLIDAY: a Wednesday 3-4 weeks back (inside every intraday retention)
#   * CLOSURE: a 9-calendar-day shutdown (Golden-Week style: Sat..Sun
#     around a full week) ending ~300 days back
HOLIDAY = _weekday_on_or_before(TODAY - timedelta(days=21), 2)
CLOSURE_END = _weekday_on_or_before(TODAY - timedelta(days=300), 6)  # a Sunday
CLOSURE_START = CLOSURE_END - timedelta(days=8)  # the Saturday before the previous week
DEFAULT_HOLIDAYS = frozenset(
    {HOLIDAY} | {CLOSURE_START + timedelta(days=n) for n in range(9) if (CLOSURE_START + timedelta(days=n)).weekday() < 5}
)


def _empty_frame() -> pd.DataFrame:
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    empty.index.name = "Date"
    return empty


def slice_like_yahoo(df: pd.DataFrame, start, end) -> pd.DataFrame:
    """What yfinance returns for history(start=, end=): bars in
    [start, end) with naive dates taken as exchange-local midnight."""
    if df.empty:
        return df
    tz = df.index.tz
    lo = pd.Timestamp(start).tz_localize(tz)
    hi = pd.Timestamp(end).tz_localize(tz)
    return df[(df.index >= lo) & (df.index < hi)]


class FakeMarket:
    """A synthetic exchange with mutually consistent daily and hourly bars.

    Every bar is a true aggregation of 5-minute "slots": price rises by
    0.01 per slot from 100 at the first session, so a bar's Close is the
    close of its last slot, High = that + 1, Low = first slot's close - 1.
    Hence the newest bar of any window holds its high and the oldest its
    low unless a test plants a spike, and a daily bar's High equals the
    max High of its hourly bars exactly. Hourly bars are anchored on the
    open like Yahoo's (09:30 ... 15:30, the last one 30 minutes long).
    Only bars that have started by `now` exist; the daily bar of the
    running session is partial, as on Yahoo.

    Hourly bars only reach back as far as Yahoo keeps them (plus a
    margin), daily bars span the whole history.
    """

    SLOT = pd.Timedelta(minutes=5)
    SESSION_LENGTH = pd.Timedelta(hours=6, minutes=30)

    def __init__(
        self,
        first_session: date,
        last_session: date,
        holidays: Iterable[date] = (),
        now: Optional[pd.Timestamp] = None,
        tz: str = EXCHANGE_TZ,
    ):
        self.tz = tz
        self.holidays = frozenset(holidays)
        self.now = now if now is not None else pd.Timestamp.now(tz=tz)
        all_days = pd.date_range(first_session, last_session, freq="D").date
        self.sessions: List[date] = [d for d in all_days if d.weekday() < 5 and d not in self.holidays]
        self._origin = self._open(self.sessions[0])
        # sessions whose open has passed by `now` (the only ones with bars)
        self.started: List[date] = [s for s in self.sessions if self._open(s) <= self.now]
        self.daily = self._bars(self.sessions, "6h30min", "Date")
        self.hourly = self._bars(self._recent(stock_router.HOURLY_RETENTION_DAYS + 40), "60min", "Datetime")

    # -- construction --------------------------------------------------
    def _open(self, s: date) -> pd.Timestamp:
        return pd.Timestamp(datetime.combine(s, SESSION_OPEN)).tz_localize(self.tz)

    def _recent(self, days: int) -> List[date]:
        cutoff = self.now.date() - timedelta(days=days)
        return [s for s in self.sessions if s >= cutoff]

    def _bars(self, sessions: List[date], freq: str, name: str) -> pd.DataFrame:
        if not sessions:
            return _empty_frame()
        offsets = pd.timedelta_range(start="0min", end=self.SESSION_LENGTH, freq=freq)
        offsets = offsets[offsets < self.SESSION_LENGTH]
        opens_utc = pd.DatetimeIndex([self._open(s) for s in sessions]).tz_convert(None).to_numpy()
        starts = (opens_utc[:, None] + offsets.to_numpy()[None, :]).ravel()
        session_close = np.repeat(opens_utc, len(offsets)) + self.SESSION_LENGTH.to_numpy()
        ends = np.minimum(starts + pd.Timedelta(freq).to_numpy(), session_close)
        now_utc = self.now.tz_convert(None).to_datetime64()
        started = starts <= now_utc
        starts, ends = starts[started], ends[started]
        # the last slot that has started by now
        now_slot_end = (self.now.floor("5min") + self.SLOT).tz_convert(None).to_datetime64()
        last_slot = np.minimum(ends, now_slot_end) - self.SLOT.to_numpy()
        to_local = lambda arr: pd.DatetimeIndex(arr).tz_localize("UTC").tz_convert(self.tz)  # noqa: E731
        starts, last_slot = to_local(starts), to_local(last_slot)
        first_units = ((starts - self._origin) / self.SLOT).astype(float)
        last_units = ((last_slot - self._origin) / self.SLOT).astype(float)
        close = 100.0 + 0.01 * last_units
        first_close = 100.0 + 0.01 * first_units
        idx = starts.normalize() if name == "Date" else starts
        idx = pd.DatetimeIndex(idx, name=name)
        n = len(idx)
        return pd.DataFrame(
            {
                "Open": first_close - 0.5,
                "High": close + 1.0,
                "Low": first_close - 1.0,
                "Close": close,
                "Volume": np.arange(1000, 1000 + n, dtype=np.int64),
                "Dividends": np.zeros(n),
                "Stock Splits": np.zeros(n),
            },
            index=idx,
        )

    # -- queries used by tests -------------------------------------------
    def frame(self, interval: str) -> pd.DataFrame:
        return {"60m": self.hourly, "1d": self.daily}[interval]

    def respond(self, start, end, interval) -> pd.DataFrame:
        """Answer a yfinance history(start=, end=, interval=) call."""
        return slice_like_yahoo(self.frame(interval), start, end)

    def sessions_on_or_before(self, d: date) -> List[date]:
        return [s for s in self.started if s <= d]

    def last_session_on_or_before(self, d: date) -> date:
        return self.sessions_on_or_before(d)[-1]

    def is_session(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def bars(self, interval: str, start: date, end: date) -> pd.DataFrame:
        """Bars of `interval` whose local date is in [start, end]."""
        df = self.frame(interval)
        dates = np.array(df.index.date, dtype=object)
        return df[(dates >= start) & (dates <= end)]


@lru_cache(maxsize=None)
def _build_market(first_session, last_session, holidays, now, tz) -> FakeMarket:
    return FakeMarket(first_session, last_session, holidays, now, tz)


def make_market(**overrides) -> FakeMarket:
    """The default market: ~21 years of sessions ending today, the
    holidays above, clock = now. Markets are memoised (building one takes
    ~0.4s); the frames are copied so tests may plant spikes freely."""
    params = dict(
        first_session=TODAY - timedelta(days=21 * 365),
        last_session=TODAY + timedelta(days=1),
        holidays=DEFAULT_HOLIDAYS,
        now=None,
        tz=EXCHANGE_TZ,
    )
    params.update(overrides)
    if params["now"] is None:
        params["now"] = pd.Timestamp.now(tz=params["tz"]).floor("min")
    base = _build_market(**params)
    m = FakeMarket.__new__(FakeMarket)
    m.__dict__.update(base.__dict__)
    m.daily, m.hourly = base.daily.copy(), base.hourly.copy()
    return m


class FakeTicker:
    """Stand-in for yfinance.Ticker.

    Configure via class attributes on a per-test basis:
      * ``window_responses``: callable(start, end, interval) -> DataFrame
        for ``history(start=, end=)`` calls. None -> empty df.
      * ``info_payload``: dict returned by ``.info``.
      * ``info_for``: per-symbol override, dict symbol -> info dict
        (symbols absent from it get an empty dict, i.e. "unknown").
      * ``raise_on_history``: exception instance raised by ``.history()``.
    ``calls`` records every ``.history()`` invocation for assertions.
    """

    window_responses = None
    info_payload = {}
    info_for = None  # dict symbol -> info dict; None = use info_payload for all
    raise_on_history = None
    calls = []

    def __init__(self, symbol, session=None):
        self.symbol = symbol

    def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
        FakeTicker.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
        if FakeTicker.raise_on_history is not None:
            raise FakeTicker.raise_on_history
        if FakeTicker.window_responses is None:
            return _empty_frame()
        return FakeTicker.window_responses(start, end, interval)

    @property
    def info(self):
        if FakeTicker.info_for is not None:
            return FakeTicker.info_for.get(self.symbol, {})
        return FakeTicker.info_payload


@pytest.fixture(autouse=True)
def fake_yfinance(monkeypatch, request):
    """Patch yfinance + reset limiter state before every test.

    Tests marked ``@pytest.mark.live`` are NOT patched: they hit the real
    Yahoo Finance API and are skipped unless LIVE_YF=1 is set.
    """
    if "live" in request.keywords:
        stock_router._limiter._calls.clear()
        yield None
        return

    FakeTicker.window_responses = None
    FakeTicker.info_payload = {}
    FakeTicker.info_for = None
    FakeTicker.raise_on_history = None
    FakeTicker.calls = []
    monkeypatch.setattr(stock_router.yf, "Ticker", FakeTicker)

    stock_router._limiter._calls.clear()
    monkeypatch.setattr(stock_router._limiter, "max_calls", stock_router.UPSTREAM_CALL_BUDGET_PER_HOUR)
    yield FakeTicker


@pytest.fixture
def market(fake_yfinance) -> FakeMarket:
    """The default market, wired into the fake yfinance."""
    m = make_market()
    fake_yfinance.window_responses = m.respond
    return m


@pytest.fixture
def clock(monkeypatch):
    """Freeze the router's idea of 'today' (UTC) and 'today at the
    exchange' to a given instant."""

    def freeze(now: pd.Timestamp):
        monkeypatch.setattr(stock_router, "_today_utc", lambda: now.tz_convert("UTC").date())
        monkeypatch.setattr(stock_router, "_today_local", lambda tz: now.tz_convert(tz).date())
        return now

    return freeze


@pytest.fixture(autouse=True)
def empty_stores():
    """Fresh in-memory stores for every test, with the users the watchlist
    tests create lists for already registered."""
    stores.users.clear()
    stores.watchlists.clear()
    for user_id in ("u1", "u2"):
        stores.users.create(user_id)
    yield stores
    stores.users.clear()
    stores.watchlists.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)
