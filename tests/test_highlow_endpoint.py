"""Tests for GET /stock/{ticker}/highlow."""

import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError

import stock_router
from tests.conftest import EXCHANGE_TZ, make_ohlc_frame

EXPECTED_KEYS = {"symbol", "period", "high", "highOccured", "low", "lowOccured"}


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


def _spike(df: pd.DataFrame, position: int, *, high: float | None = None, low: float | None = None) -> pd.Timestamp:
    """Plant an extreme High and/or Low at integer row `position`; return its timestamp."""
    if high is not None:
        df.iloc[position, df.columns.get_loc("High")] = high
    if low is not None:
        df.iloc[position, df.columns.get_loc("Low")] = low
    return df.index[position]


@pytest.fixture
def daily_5y():
    """~5 years of daily bars ending today, baseline prices 100..1399."""
    return make_ohlc_frame(periods=1300, freq="B", intraday=False, end=pd.Timestamp.now(tz=EXCHANGE_TZ).normalize())


def _days_ago_position(df: pd.DataFrame, days: int) -> int:
    """Row position of the first bar that is at least `days` days old... i.e.
    the newest bar older than the cutoff."""
    cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=days)
    older = np.where(df.index < cutoff)[0]
    return int(older[-1])


# --------------------------------------------------------------------------
# Response contract
# --------------------------------------------------------------------------
def test_default_period_is_week_and_shape_is_exact(client, fake_yfinance):
    df = make_ohlc_frame(periods=35, freq="h")
    fake_yfinance.responses = {("5d", "60m"): df}

    resp = client.get("/stock/aapl/highlow")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert set(body) == EXPECTED_KEYS, "body must be exactly the agreed shape"
    assert body["symbol"] == "AAPL"
    assert body["period"] == "WEEK"
    assert resp.headers["X-Cache"] == "MISS"
    assert fake_yfinance.calls == [{"symbol": "AAPL", "period": "5d", "interval": "60m"}]


def test_high_uses_high_column_and_low_uses_low_column_with_bar_timestamps(client, fake_yfinance):
    df = make_ohlc_frame(periods=35, freq="h")
    max_ts = _spike(df, 10, high=999.5)
    min_ts = _spike(df, 22, low=1.25)
    fake_yfinance.responses = {("5d", "60m"): df}

    body = client.get("/stock/AAPL/highlow?period=WEEK").json()

    assert body["high"] == 999.5
    assert body["highOccured"] == max_ts.isoformat()  # e.g. 2024-05-01T10:00:00-04:00
    assert body["low"] == 1.25
    assert body["lowOccured"] == min_ts.isoformat()
    assert body["highOccured"].endswith(("-04:00", "-05:00")), "intraday occurrence keeps exchange UTC offset"


@pytest.mark.parametrize("raw", ["week", "Week", " WEEK "])
def test_period_is_case_insensitive(client, fake_yfinance, raw):
    fake_yfinance.responses = {("5d", "60m"): make_ohlc_frame(periods=5, freq="h")}

    resp = client.get(f"/stock/AAPL/highlow?period={raw}")

    assert resp.status_code == 200
    assert resp.json()["period"] == "WEEK"


def test_invalid_period_is_rejected_without_upstream_call(client, fake_yfinance):
    before = stock_router._limiter.remaining()

    resp = client.get("/stock/AAPL/highlow?period=DECADE")

    assert resp.status_code == 422
    detail = json.dumps(resp.json())
    for allowed in ("DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert allowed in detail
    assert fake_yfinance.calls == []
    assert stock_router._limiter.remaining() == before


# --------------------------------------------------------------------------
# DAY
# --------------------------------------------------------------------------
def test_day_period_uses_5_minute_bars_of_last_session(client, fake_yfinance):
    df = make_ohlc_frame(periods=78, freq="5min")
    max_ts = _spike(df, 40, high=500.0)
    fake_yfinance.responses = {("1d", "5m"): df}

    body = client.get("/stock/AAPL/highlow?period=DAY").json()

    assert body["period"] == "DAY"
    assert body["high"] == 500.0
    assert body["highOccured"] == max_ts.isoformat()
    assert fake_yfinance.calls == [{"symbol": "AAPL", "period": "1d", "interval": "5m"}]


def test_day_period_falls_back_to_5d_and_keeps_only_last_session(client, fake_yfinance):
    # Two sessions, 3 days apart (e.g. Friday + the prior Tuesday); an extreme
    # in the older session must NOT count for DAY.
    # Anchor to mid-session so the bars don't straddle a date boundary.
    end = pd.Timestamp("2024-05-10 15:55", tz=EXCHANGE_TZ)
    recent = make_ohlc_frame(periods=12, freq="5min", end=end)
    older = make_ohlc_frame(periods=12, freq="5min", end=end - timedelta(days=3))
    _spike(older, 3, high=9999.0, low=0.01)
    fake_yfinance.responses = {("5d", "5m"): pd.concat([older, recent])}
    before = stock_router._limiter.remaining()

    body = client.get("/stock/AAPL/highlow?period=DAY").json()

    assert [c["period"] for c in fake_yfinance.calls] == ["1d", "5d"]
    assert before - stock_router._limiter.remaining() == 2
    assert body["high"] == round(float(recent["High"].max()), 4)
    assert body["low"] == round(float(recent["Low"].min()), 4)


# --------------------------------------------------------------------------
# MONTH / HALF_YEAR / YEAR / FIVE_YEAR share one daily frame
# --------------------------------------------------------------------------
def test_long_periods_share_a_single_upstream_call(client, fake_yfinance, daily_5y):
    fake_yfinance.responses = {("5y", "1d"): daily_5y}
    before = stock_router._limiter.remaining()

    statuses = {}
    cache_headers = {}
    for p in ("MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        r = client.get(f"/stock/AAPL/highlow?period={p}")
        statuses[p] = r.status_code
        cache_headers[p] = r.headers["X-Cache"]

    assert all(s == 200 for s in statuses.values())
    assert fake_yfinance.calls == [{"symbol": "AAPL", "period": "5y", "interval": "1d"}]
    assert before - stock_router._limiter.remaining() == 1
    assert cache_headers == {"MONTH": "MISS", "HALF_YEAR": "MEMORY", "YEAR": "MEMORY", "FIVE_YEAR": "MEMORY"}


def test_long_periods_are_sliced_correctly(client, fake_yfinance, daily_5y):
    # Plant extremes at known ages so each window sees a different answer.
    spike_400d = _spike(daily_5y, _days_ago_position(daily_5y, 400), high=9000.0)  # FIVE_YEAR only
    spike_200d = _spike(daily_5y, _days_ago_position(daily_5y, 200), high=8000.0)  # YEAR, FIVE_YEAR
    spike_100d = _spike(daily_5y, _days_ago_position(daily_5y, 100), high=7000.0)  # HALF_YEAR+
    low_100d = _spike(daily_5y, _days_ago_position(daily_5y, 100), low=0.5)  # HALF_YEAR+
    fake_yfinance.responses = {("5y", "1d"): daily_5y}

    month = client.get("/stock/AAPL/highlow?period=MONTH").json()
    half = client.get("/stock/AAPL/highlow?period=HALF_YEAR").json()
    year = client.get("/stock/AAPL/highlow?period=YEAR").json()
    five = client.get("/stock/AAPL/highlow?period=FIVE_YEAR").json()

    month_cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=30)
    month_window = daily_5y[daily_5y.index >= month_cutoff]
    assert month["high"] == round(float(month_window["High"].max()), 4)
    assert month["low"] == round(float(month_window["Low"].min()), 4)
    assert month["high"] < 7000

    assert half["high"] == 7000.0 and half["highOccured"] == spike_100d.date().isoformat()
    assert half["low"] == 0.5 and half["lowOccured"] == low_100d.date().isoformat()

    assert year["high"] == 8000.0 and year["highOccured"] == spike_200d.date().isoformat()
    assert year["low"] == 0.5

    assert five["high"] == 9000.0 and five["highOccured"] == spike_400d.date().isoformat()
    assert five["low"] == 0.5


def test_daily_periods_report_date_only_occurrence(client, fake_yfinance, daily_5y):
    fake_yfinance.responses = {("5y", "1d"): daily_5y}

    body = client.get("/stock/AAPL/highlow?period=YEAR").json()

    assert len(body["highOccured"]) == 10 and body["highOccured"].count("-") == 2  # YYYY-MM-DD
    assert len(body["lowOccured"]) == 10
    pd.Timestamp(body["highOccured"])  # parseable


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------
def test_nan_bars_are_ignored(client, fake_yfinance):
    df = make_ohlc_frame(periods=20, freq="h")
    df.iloc[5, df.columns.get_loc("High")] = np.nan
    df.iloc[6, df.columns.get_loc("Low")] = np.nan
    df.iloc[7, :] = np.nan
    fake_yfinance.responses = {("5d", "60m"): df}

    resp = client.get("/stock/AAPL/highlow")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert body["high"] == round(float(df["High"].max()), 4)
    assert body["low"] == round(float(df["Low"].min()), 4)


def test_404_when_no_data(client, fake_yfinance):
    fake_yfinance.responses = {}

    resp = client.get("/stock/NOPE/highlow?period=YEAR")

    assert resp.status_code == 404
    assert "YEAR" in resp.json()["detail"]


def test_all_nan_frame_is_404_not_500(client, fake_yfinance):
    df = make_ohlc_frame(periods=5, freq="h")
    df[["Open", "High", "Low", "Close"]] = np.nan
    fake_yfinance.responses = {("5d", "60m"): df}

    assert client.get("/stock/AAPL/highlow").status_code == 404


def test_cache_hit_reported_in_header_and_saves_budget(client, fake_yfinance):
    fake_yfinance.responses = {("5d", "60m"): make_ohlc_frame(periods=10, freq="h")}

    first = client.get("/stock/AAPL/highlow")
    after_first = stock_router._limiter.remaining()
    second = client.get("/stock/AAPL/highlow")

    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "MEMORY"
    assert second.json() == first.json()
    assert stock_router._limiter.remaining() == after_first


def test_intraday_and_daily_sources_are_cached_independently(client, fake_yfinance, daily_5y):
    fake_yfinance.responses = {
        ("5d", "60m"): make_ohlc_frame(periods=10, freq="h"),
        ("5y", "1d"): daily_5y,
        ("1d", "5m"): make_ohlc_frame(periods=10, freq="5min"),
    }

    for p in ("DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert client.get(f"/stock/AAPL/highlow?period={p}").status_code == 200

    # All six periods for one ticker cost exactly three upstream calls.
    assert sorted((c["period"], c["interval"]) for c in fake_yfinance.calls) == [("1d", "5m"), ("5d", "60m"), ("5y", "1d")]


def test_yahoo_rate_limit_maps_to_429(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFRateLimitError()
    assert client.get("/stock/AAPL/highlow").status_code == 429


def test_local_budget_exhausted_maps_to_429(client, fake_yfinance, monkeypatch):
    fake_yfinance.responses = {("5d", "60m"): make_ohlc_frame(periods=10, freq="h")}
    monkeypatch.setattr(stock_router._limiter, "max_calls", 1)

    assert client.get("/stock/AAPL/highlow").status_code == 200
    assert client.get("/stock/MSFT/highlow").status_code == 429
    assert client.get("/stock/AAPL/highlow").headers["X-Cache"] == "MEMORY"  # cached still served


def test_highlow_route_does_not_shadow_other_ticker_routes(client, fake_yfinance):
    fake_yfinance.responses = {("2d", "60m"): make_ohlc_frame(periods=5, freq="h")}
    assert client.get("/stock/AAPL/historic").status_code == 200
