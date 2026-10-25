"""Tests for GET /stock/{ticker}/hourly and GET /stock/hourly/batch."""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from yfinance.exceptions import YFException, YFRateLimitError

import stock_router
from tests.conftest import EXCHANGE_TZ, make_ohlc_frame


def _strict_json(text: str):
    """json.loads that rejects NaN/Infinity tokens (which browsers reject)."""

    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_returns_only_bars_within_last_24h(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    resp = client.get("/stock/aapl/hourly")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert body["ticker"] == "AAPL"  # normalised to upper case
    assert body["interval"] == "60m"
    assert body["window_hours"] == 24
    assert body["cached"] is False
    assert body["count"] == len(body["data"])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    timestamps = [datetime.fromisoformat(p["datetime"]) for p in body["data"]]
    assert all(ts >= cutoff for ts in timestamps), "bars older than 24h must be excluded"
    assert 24 <= len(timestamps) <= 25  # 30 bars fed in, only the rolling 24h window kept
    assert timestamps == sorted(timestamps), "chronological order"
    # One upstream call only: the 2d window had data, no fallback needed.
    assert [c["period"] for c in fake_yfinance.calls] == ["2d"]


def test_data_points_have_expected_shape(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    point = client.get("/stock/AAPL/hourly").json()["data"][0]

    assert set(point) == {"datetime", "price"}
    # ISO-8601 with the exchange's UTC offset, e.g. 2024-05-01T09:30:00-04:00
    parsed = datetime.fromisoformat(point["datetime"])
    assert parsed.tzinfo is not None
    assert isinstance(point["price"], float)
    # Price comes from the Close column
    expected_first_close = hourly_30_bars["Close"].iloc[-len(client.get("/stock/AAPL/hourly").json()["data"])]
    assert point["price"] == pytest.approx(round(float(expected_first_close), 4))


def test_nan_close_bars_are_skipped_not_emitted(client, fake_yfinance):
    df = make_ohlc_frame(periods=10, freq="h")
    df.iloc[-1, df.columns.get_loc("Close")] = float("nan")
    fake_yfinance.responses = {("2d", "60m"): df}

    resp = client.get("/stock/AAPL/hourly")

    assert resp.status_code == 200
    body = _strict_json(resp.text)  # would raise if a NaN leaked into the body
    assert body["count"] == 9
    assert all(isinstance(p["price"], float) for p in body["data"])


# --------------------------------------------------------------------------
# Fallback behaviour
# --------------------------------------------------------------------------
def test_falls_back_to_5d_when_2d_window_is_empty(client, fake_yfinance, hourly_30_bars):
    # Simulates a Monday-morning/holiday request where the 2d window has no session.
    fake_yfinance.responses = {("5d", "60m"): hourly_30_bars}
    before = stock_router._limiter.remaining()

    resp = client.get("/stock/AAPL/hourly")

    assert resp.status_code == 200
    assert [c["period"] for c in fake_yfinance.calls] == ["2d", "5d"]
    # Both real upstream calls must be charged to the budget.
    assert before - stock_router._limiter.remaining() == 2


def test_returns_last_24_available_bars_when_market_closed_over_24h(client, fake_yfinance, stale_hourly_bars):
    fake_yfinance.responses = {("2d", "60m"): stale_hourly_bars}

    body = client.get("/stock/AAPL/hourly").json()

    assert body["count"] == 24
    newest = datetime.fromisoformat(body["data"][-1]["datetime"])
    assert newest < datetime.now(timezone.utc) - timedelta(hours=24)
    assert body["data"][-1]["price"] == pytest.approx(round(float(stale_hourly_bars["Close"].iloc[-1]), 4))


def test_404_when_no_intraday_data_at_all(client, fake_yfinance):
    fake_yfinance.responses = {}  # both 2d and 5d come back empty

    resp = client.get("/stock/NOPE/hourly")

    assert resp.status_code == 404
    assert [c["period"] for c in fake_yfinance.calls] == ["2d", "5d"]


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------
def test_second_request_is_served_from_cache_without_upstream_call(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    first = client.get("/stock/AAPL/hourly").json()
    remaining_after_first = stock_router._limiter.remaining()
    second = client.get("/stock/AAPL/hourly").json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["data"] == first["data"]
    assert len(fake_yfinance.calls) == 1
    assert stock_router._limiter.remaining() == remaining_after_first


def test_cache_is_per_ticker(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    client.get("/stock/AAPL/hourly")
    resp = client.get("/stock/MSFT/hourly")

    assert resp.json()["cached"] is False
    assert [c["symbol"] for c in fake_yfinance.calls] == ["AAPL", "MSFT"]


def test_cache_expires_after_ttl(client, fake_yfinance, hourly_30_bars, monkeypatch):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    import rate_limit

    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: fake_clock["t"])

    client.get("/stock/AAPL/hourly")
    fake_clock["t"] += stock_router.CACHE_TTL_SECONDS + 1
    resp = client.get("/stock/AAPL/hourly")

    assert resp.json()["cached"] is False
    assert len(fake_yfinance.calls) == 2


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
def test_429_when_local_upstream_budget_is_exhausted(client, fake_yfinance, hourly_30_bars, monkeypatch):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    monkeypatch.setattr(stock_router._limiter, "max_calls", 2)

    assert client.get("/stock/A/hourly").status_code == 200
    assert client.get("/stock/B/hourly").status_code == 200
    resp = client.get("/stock/C/hourly")

    assert resp.status_code == 429
    assert "budget" in resp.json()["detail"].lower()
    assert len(fake_yfinance.calls) == 2, "no upstream call must be made once the budget is spent"


def test_cached_tickers_still_served_when_budget_exhausted(client, fake_yfinance, hourly_30_bars, monkeypatch):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}
    monkeypatch.setattr(stock_router._limiter, "max_calls", 1)

    assert client.get("/stock/AAPL/hourly").status_code == 200
    assert client.get("/stock/MSFT/hourly").status_code == 429
    resp = client.get("/stock/AAPL/hourly")

    assert resp.status_code == 200
    assert resp.json()["cached"] is True


def test_budget_is_charged_even_when_ticker_has_no_data(client, fake_yfinance, monkeypatch):
    """Spamming unknown tickers cannot be used to bypass the limiter."""
    fake_yfinance.responses = {}
    monkeypatch.setattr(stock_router._limiter, "max_calls", 2)

    assert client.get("/stock/X1/hourly").status_code == 404  # consumes 2 (2d + 5d)
    resp = client.get("/stock/X2/hourly")

    assert resp.status_code == 429


def test_yahoo_rate_limit_error_maps_to_429(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFRateLimitError()

    resp = client.get("/stock/AAPL/hourly")

    assert resp.status_code == 429
    assert "yahoo" in resp.json()["detail"].lower()


def test_generic_yfinance_error_maps_to_502(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFException("Yahoo is down")

    resp = client.get("/stock/AAPL/hourly")

    assert resp.status_code == 502
    assert "Yahoo is down" in resp.json()["detail"]


def test_unexpected_exception_maps_to_502_not_500(client, fake_yfinance):
    fake_yfinance.raise_on_history = KeyError("Close")

    resp = client.get("/stock/AAPL/hourly")

    assert resp.status_code == 502


# --------------------------------------------------------------------------
# Batch endpoint
# --------------------------------------------------------------------------
def test_batch_returns_each_ticker_and_isolates_failures(client, fake_yfinance, hourly_30_bars):
    def responses(period, interval):
        # FakeTicker.calls[-1] is the call currently being served.
        symbol = fake_yfinance.calls[-1]["symbol"]
        if symbol == "BAD":
            return pd.DataFrame()
        return hourly_30_bars

    fake_yfinance.responses = responses

    resp = client.get("/stock/hourly/batch?tickers=aapl, msft,BAD,,")

    assert resp.status_code == 200
    body = _strict_json(resp.text)
    assert body["requested"] == ["AAPL", "MSFT", "BAD"]
    assert body["results"]["AAPL"]["count"] == body["results"]["MSFT"]["count"] >= 24
    assert body["results"]["AAPL"]["cached"] is False
    assert "error" in body["results"]["BAD"]
    assert "data" not in body["results"]["BAD"]


def test_batch_reuses_cache_from_single_endpoint(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    client.get("/stock/AAPL/hourly")
    body = client.get("/stock/hourly/batch?tickers=AAPL,MSFT").json()

    assert body["results"]["AAPL"]["cached"] is True
    assert body["results"]["MSFT"]["cached"] is False
    assert [c["symbol"] for c in fake_yfinance.calls] == ["AAPL", "MSFT"]


def test_batch_with_no_valid_tickers_is_400(client):
    assert client.get("/stock/hourly/batch?tickers=,%20,").status_code == 400


def test_batch_does_not_collide_with_ticker_routes(client, fake_yfinance, hourly_30_bars):
    """'/stock/hourly/batch' must not be interpreted as ticker='hourly'."""
    fake_yfinance.responses = {("2d", "60m"): hourly_30_bars}

    resp = client.get("/stock/hourly/batch?tickers=AAPL")

    assert resp.status_code == 200
    assert "results" in resp.json()
    assert fake_yfinance.calls[0]["symbol"] == "AAPL"
