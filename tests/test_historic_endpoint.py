"""Tests for GET /stock/{symbol}/historic?period=..."""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from yfinance.exceptions import YFException, YFRateLimitError

import stock_router
from tests.conftest import EXCHANGE_TZ, make_ohlc_frame

EXPECTED_KEYS = {"ticker", "symbol", "period", "interval", "count", "data"}


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


@pytest.fixture
def daily_5y():
    """~5 years of business-day bars ending today. Yahoo returns a range
    ending now whatever the range is, so the fake answers every daily
    range with this frame (the API slices it to the look-back itself)."""
    return make_ohlc_frame(periods=1300, freq="B", intraday=False, end=pd.Timestamp.now(tz=EXCHANGE_TZ).normalize())


def _daily_for_all_ranges(daily, hourly=None):
    def respond(period, interval):
        if interval == "1d":
            return daily
        if hourly is not None and interval == "60m":
            return hourly
        return pd.DataFrame()

    return respond


DAILY_RANGE = {"WEEK": "1mo", "MONTH": "3mo", "HALF_YEAR": "1y", "YEAR": "2y", "FIVE_YEAR": "5y"}


def _dates(body):
    return [p["datetime"] for p in body["data"]]


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------
def test_default_period_is_day(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    resp = client.get("/stock/aapl/historic")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert set(body) == EXPECTED_KEYS
    assert body["ticker"] == "AAPL"
    assert body["symbol"] == "AAPL"
    assert body["period"] == "DAY"
    assert body["interval"] == "1h"
    assert body["count"] == len(body["data"])
    assert "X-Cache" not in resp.headers  # nothing is cached
    assert all(set(p) == {"datetime", "price"} for p in body["data"])


@pytest.mark.parametrize("raw", ["day", "Week", " MONTH "])
def test_period_is_case_insensitive(client, fake_yfinance, hourly_30_bars, daily_5y, raw):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y, hourly_30_bars)
    resp = client.get(f"/stock/AAPL/historic?period={raw}")
    assert resp.status_code == 200
    assert resp.json()["period"] == raw.strip().upper()


def test_invalid_period_is_422_without_upstream_call(client, fake_yfinance):
    before = stock_router._limiter.remaining()
    resp = client.get("/stock/AAPL/historic?period=DECADE")
    assert resp.status_code == 422
    assert fake_yfinance.calls == []
    assert stock_router._limiter.remaining() == before


# --------------------------------------------------------------------------
# DAY: last 24h, one record per hour
# --------------------------------------------------------------------------
def test_day_returns_only_bars_within_last_24h_in_order(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    body = client.get("/stock/AAPL/historic?period=DAY").json()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stamps = [datetime.fromisoformat(d) for d in _dates(body)]
    assert all(ts >= cutoff for ts in stamps)
    assert 24 <= len(stamps) <= 25
    assert stamps == sorted(stamps)
    assert all(ts.tzinfo is not None for ts in stamps)
    assert body["data"][-1]["price"] == pytest.approx(round(float(hourly_30_bars["Close"].iloc[-1]), 4))
    assert fake_yfinance.calls == [{"symbol": "AAPL", "period": "2d", "interval": "60m"}]


def test_day_falls_back_to_5d_when_2d_is_empty(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("5d", "60m"): hourly_30_bars}
    before = stock_router._limiter.remaining()

    resp = client.get("/stock/AAPL/historic?period=DAY")

    assert resp.status_code == 200
    assert [c["period"] for c in fake_yfinance.calls] == ["2d", "5d"]
    assert before - stock_router._limiter.remaining() == 2


def test_day_returns_last_24_bars_when_market_closed_over_24h(client, fake_yfinance, stale_hourly_bars):
    fake_yfinance.responses = {("2d", "60m"): stale_hourly_bars}

    body = client.get("/stock/AAPL/historic?period=DAY").json()

    assert body["count"] == 24
    assert datetime.fromisoformat(body["data"][-1]["datetime"]) < datetime.now(timezone.utc) - timedelta(hours=24)


def test_day_skips_nan_close_bars(client, fake_yfinance):
    df = make_ohlc_frame(periods=10, freq="h")
    df.iloc[-1, df.columns.get_loc("Close")] = float("nan")
    fake_yfinance.responses = {("2d", "60m"): df}

    body = _strict_json(client.get("/stock/AAPL/historic?period=DAY").text)

    assert body["count"] == 9


# --------------------------------------------------------------------------
# WEEK / MONTH: one record per trading day
# --------------------------------------------------------------------------
def test_week_is_last_7_days_one_record_per_trading_day(client, fake_yfinance, daily_5y):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y)

    body = client.get("/stock/AAPL/historic?period=WEEK").json()

    assert body["interval"] == "1d"
    cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=7)
    expected = daily_5y[daily_5y.index >= cutoff]
    assert _dates(body) == [ts.date().isoformat() for ts in expected.index]
    assert [p["price"] for p in body["data"]] == [round(float(c), 4) for c in expected["Close"]]
    assert 4 <= body["count"] <= 6  # 7 calendar days = ~5 trading days
    assert len(_dates(body)) == len(set(_dates(body)))  # one per day


def test_month_is_last_30_days_one_record_per_trading_day(client, fake_yfinance, daily_5y):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y)

    body = client.get("/stock/AAPL/historic?period=MONTH").json()

    assert body["interval"] == "1d"
    cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=30)
    expected = daily_5y[daily_5y.index >= cutoff]
    assert body["count"] == len(expected)
    assert 20 <= body["count"] <= 23
    assert _dates(body)[-1] == daily_5y.index[-1].date().isoformat()


# --------------------------------------------------------------------------
# HALF_YEAR / YEAR: one record per fortnight
# --------------------------------------------------------------------------
@pytest.mark.parametrize("period,days,expected_records", [("HALF_YEAR", 182, 13), ("YEAR", 365, 26)])
def test_fortnightly_periods(client, fake_yfinance, daily_5y, period, days, expected_records):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y)

    body = client.get(f"/stock/AAPL/historic?period={period}").json()

    assert body["interval"] == "2wk"
    # ~one record per 14 days (+1 for a possible partial bin at the old end)
    assert expected_records <= body["count"] <= expected_records + 1

    stamps = [pd.Timestamp(d) for d in _dates(body)]
    assert stamps == sorted(stamps)
    gaps = [(b - a).days for a, b in zip(stamps, stamps[1:])]
    assert all(12 <= g <= 16 for g in gaps[1:]), f"records should be ~14 days apart, got {gaps}"

    # Latest record is the most recent close; each record is a real trading day's close.
    assert _dates(body)[-1] == daily_5y.index[-1].date().isoformat()
    assert body["data"][-1]["price"] == round(float(daily_5y["Close"].iloc[-1]), 4)
    closes_by_date = {ts.date().isoformat(): round(float(c), 4) for ts, c in daily_5y["Close"].items()}
    assert all(closes_by_date[p["datetime"]] == p["price"] for p in body["data"])

    # Everything lies inside the look-back window.
    cutoff = (pd.Timestamp.now(tz="UTC") - timedelta(days=days)).date().isoformat()
    assert all(d >= cutoff for d in _dates(body))


# --------------------------------------------------------------------------
# FIVE_YEAR: one record per calendar month
# --------------------------------------------------------------------------
def test_five_year_is_one_record_per_calendar_month(client, fake_yfinance, daily_5y):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y)

    body = client.get("/stock/AAPL/historic?period=FIVE_YEAR").json()

    assert body["interval"] == "1mo"
    months = [d[:7] for d in _dates(body)]
    assert len(months) == len(set(months)), "exactly one record per month"
    assert 59 <= body["count"] <= 62
    # Each record is the last trading day of its month (month-end close).
    last_per_month = daily_5y.groupby(daily_5y.index.strftime("%Y-%m")).tail(1)
    assert _dates(body) == [ts.date().isoformat() for ts in last_per_month.index]
    assert [p["price"] for p in body["data"]] == [round(float(c), 4) for c in last_per_month["Close"]]


# --------------------------------------------------------------------------
# Budget: no cache - every request is one live call, sized to the period
# --------------------------------------------------------------------------
@pytest.mark.parametrize("period,yahoo_range", list(DAILY_RANGE.items()))
def test_daily_periods_fetch_the_smallest_covering_yahoo_range(client, fake_yfinance, daily_5y, period, yahoo_range):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y)

    assert client.get(f"/stock/AAPL/historic?period={period}").status_code == 200

    assert fake_yfinance.calls == [{"symbol": "AAPL", "period": yahoo_range, "interval": "1d"}]


def test_every_request_is_a_live_call(client, fake_yfinance, daily_5y, hourly_30_bars):
    fake_yfinance.responses = _daily_for_all_ranges(daily_5y, hourly_30_bars)
    before = stock_router._limiter.remaining()

    for _ in range(2):
        for p in ("DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
            assert client.get(f"/stock/AAPL/historic?period={p}").status_code == 200

    assert len(fake_yfinance.calls) == 12
    assert before - stock_router._limiter.remaining() == 12


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
def test_404_when_no_data(client, fake_yfinance):
    fake_yfinance.responses = {}
    assert client.get("/stock/NOPE/historic?period=DAY").status_code == 404
    assert client.get("/stock/NOPE/historic?period=YEAR").status_code == 404


def test_429_when_local_budget_exhausted(client, fake_yfinance, hourly_30_bars, monkeypatch):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    monkeypatch.setattr(stock_router._limiter, "max_calls", 1)

    assert client.get("/stock/AAPL/historic").status_code == 200
    assert client.get("/stock/MSFT/historic").status_code == 429
    assert client.get("/stock/AAPL/historic").status_code == 429  # no cache to fall back on


def test_yahoo_rate_limit_maps_to_429(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFRateLimitError()
    assert client.get("/stock/AAPL/historic").status_code == 429


def test_yfinance_error_maps_to_502(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFException("Yahoo is down")
    resp = client.get("/stock/AAPL/historic?period=MONTH")
    assert resp.status_code == 502
    assert "Yahoo is down" in resp.json()["detail"]


def test_unexpected_exception_maps_to_502_not_500(client, fake_yfinance):
    fake_yfinance.raise_on_history = KeyError("Close")
    assert client.get("/stock/AAPL/historic").status_code == 502
