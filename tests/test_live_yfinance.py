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
    assert "X-Cache" not in resp.headers


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


def test_live_historic_each_period_is_one_upstream_call(client):
    import stock_router

    before = stock_router._limiter.remaining()
    for period in ("WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert client.get(f"/stock/MSFT/historic?period={period}").status_code == 200
    assert client.get("/stock/MSFT/highlow?period=YEAR").status_code == 200
    assert before - stock_router._limiter.remaining() == 6  # no cache: 1 per request


def test_live_unknown_ticker_historic_is_404_not_500(client):
    assert client.get("/stock/THISTICKERDOESNOTEXIST123/historic").status_code in (404, 502)


# --------------------------------------------------------------------------
# referenceDate
# --------------------------------------------------------------------------
def _recent_weekday(days_back: int):
    d = datetime.now(timezone.utc).date() - timedelta(days=days_back)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def test_live_historic_week_ending_on_reference_date(client):
    import stock_router

    before = stock_router._limiter.remaining()
    reference = _recent_weekday(45)

    resp = client.get(f"/stock/AAPL/historic?period=WEEK&referenceDate={reference.isoformat()}")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert list(body["results"]) == [reference.isoformat()]
    result = body["results"][reference.isoformat()]
    assert 3 <= result["count"] <= 5
    first_day = (reference - timedelta(days=6)).isoformat()
    assert all(first_day <= p["datetime"] <= reference.isoformat() for p in result["data"])
    assert result["data"][-1]["datetime"] == reference.isoformat()  # a weekday: inclusive end is present
    assert all(p["price"] > 0 for p in result["data"])
    assert before - stock_router._limiter.remaining() == 1  # one live call


def test_live_historic_multiple_reference_dates(client):
    import stock_router

    before = stock_router._limiter.remaining()
    dates = [_recent_weekday(n) for n in (45, 100, 200)]

    resp = client.get("/stock/AAPL/historic?period=WEEK&referenceDate=" + ",".join(d.isoformat() for d in dates))

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert list(body["results"]) == [d.isoformat() for d in dates]
    for d in dates:
        result = body["results"][d.isoformat()]
        assert 3 <= result["count"] <= 5
        assert all((d - timedelta(days=6)).isoformat() <= p["datetime"] <= d.isoformat() for p in result["data"])
    assert before - stock_router._limiter.remaining() == 3  # one live call per date


def test_live_historic_day_on_a_past_date_is_that_sessions_hourly_bars(client):
    # A holiday can make a single weekday empty; try a few and require one hit.
    hits = 0
    for days_back in (10, 11, 12, 13, 14):
        reference = _recent_weekday(days_back)
        resp = client.get(f"/stock/AAPL/historic?period=DAY&referenceDate={reference.isoformat()}")
        assert resp.status_code in (200, 404), resp.text
        if resp.status_code == 200:
            body = _strict_json(resp.text)
            result = body["results"][reference.isoformat()]
            assert body["interval"] == "1h"
            assert 6 <= result["count"] <= 8  # a regular NYSE session at hourly bars
            assert all(p["datetime"].startswith(reference.isoformat()) for p in result["data"])
            hits += 1
    assert hits, "expected at least one trading day among five weekdays"


def test_live_historic_day_beyond_hourly_retention_is_422_without_a_call(client):
    import stock_router

    before = stock_router._limiter.remaining()
    resp = client.get("/stock/AAPL/historic?period=DAY&referenceDate=2019-01-02")
    assert resp.status_code == 422
    assert stock_router._limiter.remaining() == before


def test_live_historic_month_ending_years_ago(client):
    resp = client.get("/stock/AAPL/historic?period=MONTH&referenceDate=2015-03-31")

    assert resp.status_code == 200, resp.text
    result = _strict_json(resp.text)["results"]["2015-03-31"]
    assert 19 <= result["count"] <= 23
    assert all("2015-03-02" <= p["datetime"] <= "2015-03-31" for p in result["data"])
    assert result["data"][-1]["datetime"] == "2015-03-31"


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


def test_live_highlow_each_period_is_one_upstream_call(client):
    import stock_router

    before = stock_router._limiter.remaining()
    for period in ("MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert client.get(f"/stock/MSFT/highlow?period={period}").status_code == 200
    assert before - stock_router._limiter.remaining() == 4


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
