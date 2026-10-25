"""Integration tests: dotted ticker symbols, the two-tier cache as seen
through the API (X-Cache tiers, restart survival, TTL), and the cache
admin endpoints."""

import pandas as pd
import pytest

import stock_router
from tests.conftest import make_ohlc_frame, make_tiered_cache
from tests.test_snapshot_endpoint import AAPL_INFO


# --------------------------------------------------------------------------
# Dotted tickers
# --------------------------------------------------------------------------
def _frames_for_symbols(fake_yfinance, symbols_with_data, frame):
    """FakeTicker responder that returns `frame` only for given symbols."""

    def responses(period, interval):
        if fake_yfinance.calls[-1]["symbol"] in symbols_with_data:
            return frame
        return pd.DataFrame()

    return responses


def test_exchange_suffix_symbol_routes_and_resolves_directly(client, fake_yfinance, hourly_30_bars):
    """SAP.DE-style symbols (dot = exchange suffix) work as-is: the dot must
    survive FastAPI routing and reach yfinance unchanged, with no retry."""
    fake_yfinance.responses = _frames_for_symbols(fake_yfinance, {"SAP.DE"}, hourly_30_bars)

    resp = client.get("/stock/sap.de/historic?period=DAY")

    assert resp.status_code == 200
    assert resp.json()["ticker"] == "SAP.DE"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["SAP.DE"]


def test_numeric_dotted_symbol_routes(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = _frames_for_symbols(fake_yfinance, {"0700.HK"}, hourly_30_bars)
    resp = client.get("/stock/0700.HK/historic")
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "0700.HK"


def test_us_share_class_dot_falls_back_to_yahoo_dash_form(client, fake_yfinance, hourly_30_bars):
    """BRK.B: Yahoo only knows BRK-B. The dotted form is retried with a
    dash, and the retry is charged to the budget."""
    fake_yfinance.responses = _frames_for_symbols(fake_yfinance, {"BRK-B"}, hourly_30_bars)
    before = stock_router._limiter.remaining()

    resp = client.get("/stock/BRK.B/historic?period=DAY")

    assert resp.status_code == 200
    assert resp.json()["ticker"] == "BRK.B"  # echoes what the client asked for
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B"]
    assert before - stock_router._limiter.remaining() == 2


def test_dotted_symbol_fallback_applies_to_highlow_daily_frame(client, fake_yfinance):
    daily = make_ohlc_frame(periods=400, freq="B", intraday=False)
    fake_yfinance.responses = _frames_for_symbols(fake_yfinance, {"BRK-B"}, daily)

    resp = client.get("/stock/BRK.B/highlow?period=YEAR")

    assert resp.status_code == 200
    assert resp.json()["symbol"] == "BRK.B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B"]


def test_snapshot_dotted_symbol_falls_back_to_dash(client, fake_yfinance):
    info = dict(AAPL_INFO, symbol="BRK-B", longName="Berkshire Hathaway Inc.")
    fake_yfinance.info_for = {"BRK-B": info}  # only the dashed form exists
    before = stock_router._limiter.remaining()

    resp = client.get("/stock/BRK.B")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "BRK.B"
    assert body["symbol"] == "BRK-B"  # Yahoo's canonical form
    assert before - stock_router._limiter.remaining() == 4  # 2 per .info attempt


def test_unknown_dotted_symbol_is_404_after_both_forms_tried(client, fake_yfinance):
    fake_yfinance.responses = {}
    resp = client.get("/stock/NO.PE/historic?period=DAY")
    assert resp.status_code == 404
    # 2d dotted, 2d dashed, 5d dotted, 5d dashed
    assert [(c["symbol"], c["period"]) for c in fake_yfinance.calls] == [
        ("NO.PE", "2d"), ("NO-PE", "2d"), ("NO.PE", "5d"), ("NO-PE", "5d"),
    ]


def test_dotted_symbols_have_distinct_cache_entries(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = _frames_for_symbols(fake_yfinance, {"SAP.DE", "SAPDE"}, hourly_30_bars)

    client.get("/stock/SAP.DE/historic")
    resp = client.get("/stock/SAPDE/historic")

    assert resp.headers["X-Cache"] == "MISS"  # not served from SAP.DE's entry
    assert {c["symbol"] for c in fake_yfinance.calls} == {"SAP.DE", "SAPDE"}


# --------------------------------------------------------------------------
# Two-tier cache through the API
# --------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def clocked_cache(monkeypatch, tmp_path, clock):
    cache = make_tiered_cache(tmp_path, time_fn=clock, name="clocked.sqlite")
    monkeypatch.setattr(stock_router, "_cache", cache)
    yield cache
    cache.close()


def test_x_cache_reports_tier(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    first = client.get("/stock/AAPL/historic")
    second = client.get("/stock/AAPL/historic")
    stock_router._cache.memory.clear()  # memory lost, disk intact
    third = client.get("/stock/AAPL/historic")
    fourth = client.get("/stock/AAPL/historic")

    assert [r.headers["X-Cache"] for r in (first, second, third, fourth)] == ["MISS", "MEMORY", "DISK", "MEMORY"]
    assert [r.json()["cached"] for r in (first, second, third, fourth)] == [False, True, True, True]
    assert len(fake_yfinance.calls) == 1


def test_entries_survive_app_restart_via_disk(client, fake_yfinance, hourly_30_bars, tmp_path, monkeypatch):
    """Restart = new memory tier, same disk file: served from DISK with no
    upstream call, then from MEMORY."""
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    before = make_tiered_cache(tmp_path, name="restart.sqlite")
    monkeypatch.setattr(stock_router, "_cache", before)
    first = client.get("/stock/AAPL/historic")
    assert first.headers["X-Cache"] == "MISS"
    before.close()

    after = make_tiered_cache(tmp_path, name="restart.sqlite")
    monkeypatch.setattr(stock_router, "_cache", after)
    second = client.get("/stock/AAPL/historic")
    third = client.get("/stock/AAPL/historic")

    assert second.headers["X-Cache"] == "DISK"
    assert second.json()["data"] == first.json()["data"]
    assert third.headers["X-Cache"] == "MEMORY"
    assert len(fake_yfinance.calls) == 1
    after.close()


def test_ttl_applies_to_both_tiers(client, fake_yfinance, hourly_30_bars, clocked_cache, clock):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    client.get("/stock/AAPL/historic")

    clock.t += clocked_cache.ttl_seconds + 1

    resp = client.get("/stock/AAPL/historic")
    assert resp.headers["X-Cache"] == "MISS"  # neither memory nor disk served the expired entry
    assert len(fake_yfinance.calls) == 2


def test_ttl_is_one_day_by_default(clocked_cache):
    assert clocked_cache.ttl_seconds == 24 * 3600


def test_exhausted_budget_is_429_even_with_expired_entry(client, fake_yfinance, hourly_30_bars, clocked_cache, clock, monkeypatch):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    client.get("/stock/AAPL/historic")
    clock.t += clocked_cache.ttl_seconds + 1
    monkeypatch.setattr(stock_router._limiter, "max_calls", 0)

    assert client.get("/stock/AAPL/historic").status_code == 429


# --------------------------------------------------------------------------
# Cache admin endpoints
# --------------------------------------------------------------------------
def test_delete_cache_clears_both_tiers(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    client.get("/stock/AAPL/historic")
    client.get("/stock/MSFT/historic")
    stock_router._cache.memory.clear()
    client.get("/stock/AAPL/historic")  # promotes AAPL back to memory
    assert stock_router._cache.stats()["memory"]["entries"] == 1
    assert stock_router._cache.stats()["disk"]["entries"] == 2

    resp = client.delete("/cache")

    assert resp.status_code == 200
    body = resp.json()
    assert body["cleared"] == {"memory": 1, "disk": 2}
    assert body["memory"]["entries"] == 0
    assert body["disk"]["entries"] == 0
    assert client.get("/stock/AAPL/historic").headers["X-Cache"] == "MISS"
    assert client.get("/stock/MSFT/historic").headers["X-Cache"] == "MISS"


def test_cache_stats_endpoint(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    client.get("/stock/AAPL/historic")

    body = client.get("/cache/stats").json()

    assert body["short_ttl_seconds"] == 300 and body["long_ttl_seconds"] == 24 * 3600
    assert body["memory"] == {"entries": 1, "max_entries": 512}
    assert body["disk"]["entries"] == 1
    assert 0 < body["disk"]["bytes"] <= body["disk"]["max_bytes"]
    assert body["purged_now"] == {"memory": 0, "disk": 0}
    assert 0 <= body["upstream_budget_remaining"] <= stock_router.UPSTREAM_CALL_BUDGET_PER_HOUR
