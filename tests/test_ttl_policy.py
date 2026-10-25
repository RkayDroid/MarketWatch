"""TTL policy through the API:

    GET /stock/{symbol}                      5 minutes
    GET /stock/{symbol}/historic?period=DAY  5 minutes; other periods 1 day
    GET /stock/{symbol}/highlow?period=DAY   5 minutes; other periods 1 day

Each case is driven with a fake clock shared by both cache tiers: request,
advance just under / just over the expected TTL, and check whether a live
upstream call happened (X-Cache MISS) or the cache answered.
"""

import pandas as pd
import pytest

import stock_router
from tests.conftest import EXCHANGE_TZ, make_ohlc_frame, make_tiered_cache
from tests.test_snapshot_endpoint import AAPL_INFO

FIVE_MINUTES = 300
ONE_DAY = 24 * 3600


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def clocked_cache(monkeypatch, tmp_path, clock):
    cache = make_tiered_cache(tmp_path, time_fn=clock, name="ttl.sqlite")
    monkeypatch.setattr(stock_router, "_cache", cache)
    yield cache
    cache.close()


@pytest.fixture
def all_frames(fake_yfinance):
    daily = make_ohlc_frame(periods=1300, freq="B", intraday=False, end=pd.Timestamp.now(tz=EXCHANGE_TZ).normalize())
    fake_yfinance.responses = {
        ("2d", "60m"): make_ohlc_frame(periods=30, freq="h"),
        ("1d", "5m"): make_ohlc_frame(periods=78, freq="5min"),
        ("5d", "60m"): make_ohlc_frame(periods=35, freq="h"),
        ("5y", "1d"): daily,
    }
    fake_yfinance.info_payload = AAPL_INFO


def _expiry_behaviour(client, clock, url, ttl):
    """Returns (status just before ttl, status just after ttl)."""
    assert client.get(url).headers["X-Cache"] == "MISS"
    clock.t += ttl - 1
    before = client.get(url).headers["X-Cache"]
    clock.t += 2
    after = client.get(url).headers["X-Cache"]
    return before, after


def test_defaults_are_five_minutes_and_one_day():
    assert stock_router.SHORT_TTL_SECONDS == FIVE_MINUTES
    assert stock_router.LONG_TTL_SECONDS == ONE_DAY


@pytest.mark.parametrize(
    "url,ttl",
    [
        ("/stock/AAPL", FIVE_MINUTES),
        ("/stock/AAPL/historic?period=DAY", FIVE_MINUTES),
        ("/stock/AAPL/historic", FIVE_MINUTES),  # default period is DAY
        ("/stock/AAPL/highlow?period=DAY", FIVE_MINUTES),
        ("/stock/AAPL/historic?period=WEEK", ONE_DAY),
        ("/stock/AAPL/historic?period=MONTH", ONE_DAY),
        ("/stock/AAPL/historic?period=HALF_YEAR", ONE_DAY),
        ("/stock/AAPL/historic?period=YEAR", ONE_DAY),
        ("/stock/AAPL/historic?period=FIVE_YEAR", ONE_DAY),
        ("/stock/AAPL/highlow?period=WEEK", ONE_DAY),
        ("/stock/AAPL/highlow", ONE_DAY),  # default period is WEEK
        ("/stock/AAPL/highlow?period=MONTH", ONE_DAY),
        ("/stock/AAPL/highlow?period=HALF_YEAR", ONE_DAY),
        ("/stock/AAPL/highlow?period=YEAR", ONE_DAY),
        ("/stock/AAPL/highlow?period=FIVE_YEAR", ONE_DAY),
    ],
)
def test_path_expires_exactly_at_its_ttl(client, clock, all_frames, url, ttl):
    before, after = _expiry_behaviour(client, clock, url, ttl)
    assert before == "MEMORY", f"{url} expired too early"
    assert after == "MISS", f"{url} did not expire at {ttl}s"


@pytest.mark.parametrize(
    "url",
    [
        "/stock/AAPL/historic?period=WEEK",
        "/stock/AAPL/historic?period=FIVE_YEAR",
        "/stock/AAPL/highlow?period=WEEK",
        "/stock/AAPL/highlow?period=YEAR",
    ],
)
def test_non_day_paths_survive_well_past_five_minutes(client, clock, all_frames, url):
    client.get(url)
    clock.t += 6 * 3600  # six hours later
    assert client.get(url).headers["X-Cache"] == "MEMORY"


def test_day_and_non_day_ttls_are_independent_for_the_same_symbol(client, clock, all_frames, fake_yfinance):
    client.get("/stock/AAPL/historic?period=DAY")
    client.get("/stock/AAPL/historic?period=MONTH")
    calls_after_warmup = len(fake_yfinance.calls)

    clock.t += FIVE_MINUTES + 1
    assert client.get("/stock/AAPL/historic?period=DAY").headers["X-Cache"] == "MISS"
    assert client.get("/stock/AAPL/historic?period=MONTH").headers["X-Cache"] == "MEMORY"
    assert len(fake_yfinance.calls) == calls_after_warmup + 1  # only the DAY frame was refetched


def test_short_ttl_applies_on_disk_tier_too(client, clock, all_frames):
    """After a memory loss the disk copy must honour the same 5-minute expiry."""
    client.get("/stock/AAPL/historic?period=DAY")
    stock_router._cache.memory.clear()
    clock.t += FIVE_MINUTES - 1
    assert client.get("/stock/AAPL/historic?period=DAY").headers["X-Cache"] == "DISK"

    stock_router._cache.memory.clear()
    clock.t += 2
    assert client.get("/stock/AAPL/historic?period=DAY").headers["X-Cache"] == "MISS"


def test_promotion_from_disk_keeps_short_ttl(client, clock, all_frames):
    client.get("/stock/AAPL")
    stock_router._cache.memory.clear()
    clock.t += FIVE_MINUTES - 10
    assert client.get("/stock/AAPL").headers["X-Cache"] == "DISK"  # promoted with ~10s left
    clock.t += 11
    assert client.get("/stock/AAPL").headers["X-Cache"] == "MISS"  # not extended by promotion


def test_ttls_are_env_configurable(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("MWL_CACHE_SHORT_TTL_SECONDS", "60")
    monkeypatch.setenv("MWL_CACHE_TTL_SECONDS", "7200")
    monkeypatch.setenv("MWL_CACHE_DIR", str(tmp_path))
    module = importlib.reload(stock_router)
    try:
        assert module.SHORT_TTL_SECONDS == 60
        assert module.LONG_TTL_SECONDS == 7200
    finally:
        module._cache.close()
        monkeypatch.delenv("MWL_CACHE_SHORT_TTL_SECONDS")
        monkeypatch.delenv("MWL_CACHE_TTL_SECONDS")
        importlib.reload(stock_router)


def test_stats_report_both_ttls(client):
    body = client.get("/cache/stats").json()
    assert body["short_ttl_seconds"] == FIVE_MINUTES
    assert body["long_ttl_seconds"] == ONE_DAY
    assert "ttl_seconds" not in body
