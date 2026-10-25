"""Tests for the pre-existing endpoints GET /stock/{ticker} and
GET /stock/{ticker}/history after they were routed through the shared
cache / rate limiter / serialisation helpers."""

import json

import numpy as np
import pandas as pd
import pytest

import stock_router
from tests.conftest import make_ohlc_frame


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


# --------------------------------------------------------------------------
# /stock/{ticker}/history
# --------------------------------------------------------------------------
def test_history_daily_frame_round_trips_to_valid_json(client, fake_yfinance):
    df = make_ohlc_frame(periods=5, freq="B", intraday=False, end=pd.Timestamp("2024-05-10", tz="America/New_York"))
    fake_yfinance.responses = {("1mo", "1d"): df}

    resp = client.get("/stock/aapl/history")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert body["ticker"] == "AAPL"
    assert body["period"] == "1mo"
    assert body["interval"] == "1d"
    assert body["count"] == 5 == len(body["history"])
    row = body["history"][0]
    assert row["Date"] == "2024-05-06T00:00:00-04:00"
    assert set(row) == {"Date", "Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"}
    assert isinstance(row["Volume"], int)  # numpy int64 -> python int
    assert isinstance(row["Close"], float)


def test_history_with_nan_values_returns_null_instead_of_crashing(client, fake_yfinance):
    """Regression: real yfinance frames contain NaN (e.g. ETF 'Capital
    Gains', partial bars). The original endpoint blew up with a 500
    ('Out of range float values are not JSON compliant: nan')."""
    df = make_ohlc_frame(
        periods=3,
        freq="B",
        intraday=False,
        with_nan_row=True,
        extra_columns={"Capital Gains": np.nan},
    )
    fake_yfinance.responses = {("1mo", "1d"): df}

    resp = client.get("/stock/VTI/history")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert body["history"][-1]["Open"] is None
    assert all(r["Capital Gains"] is None for r in body["history"])


def test_history_intraday_interval_uses_datetime_index(client, fake_yfinance):
    """yfinance names the index 'Datetime' for intraday bars; the endpoint
    must not hard-code 'Date'."""
    df = make_ohlc_frame(periods=4, freq="h", intraday=True)
    assert df.index.name == "Datetime"
    fake_yfinance.responses = {("1d", "60m"): df}

    resp = client.get("/stock/AAPL/history?period=1d&interval=60m")

    assert resp.status_code == 200
    assert len(resp.json()["history"]) == 4
    assert "Date" in resp.json()["history"][0]
    assert fake_yfinance.calls == [{"symbol": "AAPL", "period": "1d", "interval": "60m"}]


@pytest.mark.parametrize("query", ["period=1month", "period=7d", "interval=2h", "period=1mo&interval=tick"])
def test_history_rejects_invalid_period_or_interval_before_calling_upstream(client, fake_yfinance, query):
    before = stock_router._limiter.remaining()

    resp = client.get(f"/stock/AAPL/history?{query}")

    assert resp.status_code == 400
    assert "Valid values" in resp.json()["detail"]
    assert fake_yfinance.calls == []
    assert stock_router._limiter.remaining() == before


def test_history_404_when_empty(client, fake_yfinance):
    fake_yfinance.responses = {}
    assert client.get("/stock/AAPL/history").status_code == 404


def test_history_is_cached_per_ticker_period_interval(client, fake_yfinance):
    df = make_ohlc_frame(periods=3, freq="B", intraday=False)
    fake_yfinance.responses = lambda period, interval: df

    assert client.get("/stock/AAPL/history?period=1mo").json()["cached"] is False
    assert client.get("/stock/AAPL/history?period=1mo").json()["cached"] is True
    assert client.get("/stock/AAPL/history?period=3mo").json()["cached"] is False
    assert client.get("/stock/AAPL/history?period=1mo&interval=1wk").json()["cached"] is False
    assert len(fake_yfinance.calls) == 3


# --------------------------------------------------------------------------
# /stock/{ticker}
# --------------------------------------------------------------------------
def test_info_returns_payload_and_is_cached(client, fake_yfinance):
    fake_yfinance.info_payload = {"symbol": "AAPL", "shortName": "Apple Inc.", "regularMarketPrice": 190.5}
    before = stock_router._limiter.remaining()

    first = client.get("/stock/aapl")
    second = client.get("/stock/AAPL")

    assert first.status_code == 200
    assert first.json()["ticker"] == "AAPL"
    assert first.json()["info"]["shortName"] == "Apple Inc."
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert before - stock_router._limiter.remaining() == 1


def test_info_404_when_empty(client, fake_yfinance):
    fake_yfinance.info_payload = {}
    assert client.get("/stock/NOPE").status_code == 404


# --------------------------------------------------------------------------
# Non-stock routes still work after router changes
# --------------------------------------------------------------------------
def test_status_and_time_endpoints(client):
    assert client.get("/status").json()["status"] == "online"
    times = client.get("/time").json()["current_times"]
    assert "UTC" in times and "America/New_York" in times
