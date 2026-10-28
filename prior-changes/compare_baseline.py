"""Run the same requests against the baseline (git d473f3e) and the current
code, both fed by the same synthetic market at the same frozen clock.
This is how the samples in SUMMARY-baseline-to-now.md were produced.

    python prior-changes/compare_baseline.py before > /tmp/before.json
    python prior-changes/compare_baseline.py after  > /tmp/after.json
"""
import importlib
import json
import subprocess
import sys
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "d473f3e"
sys.path.insert(0, str(ROOT))
from tests.conftest import make_market, slice_like_yahoo  # noqa: E402


def checkout_baseline() -> str:
    """The baseline's three modules, extracted from git into a temp dir."""
    tmp = tempfile.mkdtemp(prefix="mwl-baseline-")
    for name in ("stock_router.py", "rate_limit.py", "main.py"):
        blob = subprocess.check_output(["git", "show", f"{BASELINE_COMMIT}:MarketWatchLite/{name}"], cwd=ROOT)
        Path(tmp, name).write_bytes(blob)
    return tmp

NOW = pd.Timestamp("2026-10-21 14:00", tz="America/New_York")
HOLIDAYS = frozenset({date(2026, 10, 14)})
market = make_market(now=NOW, holidays=HOLIDAYS)
five = market._bars(market._recent(70), "5min", "Datetime")
for f in (market.daily, market.hourly, five):
    for c in ("Open", "High", "Low", "Close"):
        f[c] = (f[c] / 100).round(2)
FRAMES = {"1d": market.daily, "60m": market.hourly, "5m": five}
TODAY = NOW.date()


def yahoo_range(period, interval):
    """Emulate Yahoo's range= semantics: sessions for 1d/2d/5d, calendar for 1mo+."""
    df = FRAMES[interval]
    if period.endswith("d"):
        n = int(period[:-1])
        sessions = sorted({ts.date() for ts in df.index})[-n:]
        return df[[ts.date() in sessions for ts in df.index]]
    months = {"1mo": 1, "3mo": 3, "6mo": 6, "1y": 12, "2y": 24, "5y": 60}[period]
    start = (pd.Timestamp(TODAY) - pd.DateOffset(months=months)).date()
    return slice_like_yahoo(df, start, TODAY + timedelta(days=1))


class Fake:
    calls = []

    def __init__(self, symbol, session=None):
        self.symbol = symbol

    def history(self, period=None, interval="1d", start=None, end=None, **kw):
        Fake.calls.append((self.symbol, period, interval, str(start), str(end)))
        if start is not None or end is not None:
            return slice_like_yahoo(FRAMES[interval], start, end)
        return yahoo_range(period, interval)


def load(path, name):
    for m in list(sys.modules):
        if m in ("stock_router", "main", "rate_limit"):
            del sys.modules[m]
    sys.path.insert(0, path)
    sr = importlib.import_module("stock_router")
    mn = importlib.import_module("main")
    sys.path.pop(0)
    sr.yf.Ticker = Fake
    sr._today_utc = lambda: NOW.tz_convert("UTC").date()
    if hasattr(sr, "_today_local"):
        sr._today_local = lambda tz: NOW.tz_convert(tz).date()
    # baseline slices rolling daily windows with pd.Timestamp.now: freeze it
    sr.pd = types.SimpleNamespace(**{k: getattr(pd, k) for k in dir(pd) if not k.startswith("_")})
    class FrozenTs(pd.Timestamp):
        @classmethod
        def now(cls, tz=None):
            return NOW.tz_convert(tz) if tz else NOW.tz_localize(None)
    sr.pd.Timestamp = FrozenTs
    sr.datetime = types.SimpleNamespace(now=lambda tz=None: NOW.tz_convert(tz).to_pydatetime() if tz else NOW.to_pydatetime())
    return TestClient(mn.app), sr


def trim(b, keep):
    def t(v):
        if isinstance(v, dict) and "data" in v and len(v["data"]) > keep:
            v["data"] = v["data"][:keep - 1] + ["..."] + v["data"][-1:]
    t(b)
    for v in (b.get("results") or {}).values():
        t(v)
    return b


def run(client, url, keep=3):
    Fake.calls = []
    r = client.get(url)
    body = r.json()
    return r.status_code, trim(body, keep), list(Fake.calls)


if __name__ == "__main__":
    which = sys.argv[1]
    client, sr = load(checkout_baseline() if which == "before" else str(ROOT), which)
    out = {}
    for url, keep in [
        ("/stock/AAPL/historic?period=DAY", 3),
        ("/stock/AAPL/historic?period=WEEK", 6),
        ("/stock/AAPL/historic?period=WEEK&referenceDate=2026-10-18", 6),
        ("/stock/AAPL/historic?period=YEAR", 3),
        ("/stock/AAPL/historic?period=DAY&referenceDate=2026-10-18", 3),
        ("/stock/AAPL/historic?period=MONTH&referenceDate=2015-03-31,1970-01-01", 3),
        ("/stock/AAPL/highlow?period=WEEK", 3),
        ("/stock/AAPL/highlow?period=DAY", 3),
        ("/stock/AAPL/highlow?period=WEEK&referenceDate=2026-10-18", 3),
    ]:
        status, body, calls = run(client, url, keep)
        out[url] = {"status": status, "body": body, "calls": calls}
    print(json.dumps(out, indent=1))
