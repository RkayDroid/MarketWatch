"""
Opt-in smoke tests against the REAL Yahoo Finance API via yfinance.

They are skipped by default because they need network access and consume a
handful of requests from the free-tier budget. Run them locally with:

    LIVE_YF=1 pytest tests/test_live_yfinance.py -v

These are the tests that validate assumptions the mocked suite cannot:
the actual frame/info shape yfinance returns today, NaN handling on real data,
and that the endpoints produce strictly valid JSON end-to-end.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LIVE_YF") != "1", reason="set LIVE_YF=1 to run live Yahoo Finance tests"),
]


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


def test_live_snapshot_aapl(client):
    resp = client.get("/stock/AAPL")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert set(body) == {"ticker", "sector", "fulldayPrice", "currency", "symbol",
                         "language", "longName", "exchange", "market", "displayName"}
    assert body["symbol"] == "AAPL" and body["currency"] == "USD"
    assert body["sector"] == "Technology"
    assert body["fulldayPrice"] > 0
    assert client.get("/stock/AAPL").headers["X-Cache"] == "MEMORY"


def test_live_snapshot_etf_has_null_sector(client):
    body = _strict_json(client.get("/stock/VTI").text)
    assert body["sector"] is None
    assert body["fulldayPrice"] > 0


def test_live_snapshot_unknown_ticker_is_404(client):
    assert client.get("/stock/THISTICKERDOESNOTEXIST123").status_code in (404, 502)


@pytest.mark.parametrize("period", ["DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"])
def test_live_historic_all_periods(client, period):
    resp = client.get(f"/stock/AAPL/historic?period={period}")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert body["period"] == period
    assert body["count"] >= 1
    stamps = [p["datetime"] for p in body["data"]]
    assert stamps == sorted(stamps)
    assert all(p["price"] > 0 for p in body["data"])
    if period == "DAY":
        newest = datetime.fromisoformat(stamps[-1])
        assert newest > datetime.now(timezone.utc) - timedelta(days=5)
    else:
        assert all(len(s) == 10 for s in stamps)  # YYYY-MM-DD


def test_live_historic_long_periods_cost_one_upstream_call(client):
    import stock_router

    stock_router._cache.clear()
    before = stock_router._limiter.remaining()
    for period in ("WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert client.get(f"/stock/MSFT/historic?period={period}").status_code == 200
    assert client.get("/stock/MSFT/highlow?period=YEAR").status_code == 200
    assert before - stock_router._limiter.remaining() == 1


def test_live_unknown_ticker_historic_is_404_not_500(client):
    assert client.get("/stock/THISTICKERDOESNOTEXIST123/historic").status_code in (404, 502)


@pytest.mark.parametrize("period", ["DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"])
def test_live_highlow_all_periods(client, period):
    resp = client.get(f"/stock/AAPL/highlow?period={period}")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert set(body) == {"symbol", "period", "high", "highOccured", "low", "lowOccured"}
    assert body["symbol"] == "AAPL" and body["period"] == period
    assert body["low"] <= body["high"]
    assert body["low"] > 0
    # occurrence strings must be parseable
    datetime.fromisoformat(body["highOccured"])
    datetime.fromisoformat(body["lowOccured"])


def test_live_highlow_long_periods_cost_one_upstream_call(client):
    import stock_router

    stock_router._cache.clear()
    before = stock_router._limiter.remaining()
    for period in ("MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert client.get(f"/stock/MSFT/highlow?period={period}").status_code == 200
    assert before - stock_router._limiter.remaining() == 1


@pytest.mark.parametrize("symbol", ["SAP.DE", "0700.HK", "BARC.L"])
def test_live_exchange_suffix_dotted_symbols(client, symbol):
    """Dots as exchange suffixes are Yahoo-native and must work directly."""
    resp = client.get(f"/stock/{symbol}/historic?period=WEEK")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert body["ticker"] == symbol
    assert body["count"] >= 1


def test_live_us_share_class_dot_is_resolved_via_dash_fallback(client):
    """Yahoo only knows BRK-B; BRK.B must still answer via the dash retry."""
    resp = client.get("/stock/BRK.B/historic?period=WEEK")

    assert resp.status_code == 200, resp.text
    assert _strict_json(resp.text)["count"] >= 1

    highlow = client.get("/stock/BRK.B/highlow?period=YEAR")
    assert highlow.status_code == 200, highlow.text


def test_live_snapshot_dotted_share_class(client):
    resp = client.get("/stock/BRK.B")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert body["ticker"] == "BRK.B"
    assert body["symbol"] == "BRK-B"  # Yahoo's canonical form
    assert body["fulldayPrice"] > 0
