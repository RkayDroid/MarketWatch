"""Tests for GET /stock/{symbol} (snapshot)."""

import json

import stock_router

EXPECTED_KEYS = {
    "ticker", "sector", "fulldayPrice", "currency", "symbol",
    "language", "longName", "exchange", "market", "displayName",
}

# Subset of a real yfinance `.info` dict for AAPL (plus noise keys that must
# NOT leak into the response).
AAPL_INFO = {
    "symbol": "AAPL",
    "sector": "Technology",
    "industry": "Consumer Electronics",
    "regularMarketPrice": 347.725,
    "currentPrice": 347.7,
    "previousClose": 345.1,
    "currency": "USD",
    "language": "en-US",
    "longName": "Apple Inc.",
    "shortName": "Apple Inc.",
    "displayName": "Apple",
    "exchange": "NMS",
    "fullExchangeName": "NasdaqGS",
    "market": "us_market",
    "marketCap": 5_000_000_000_000,
    "companyOfficers": [{"name": "Tim Cook"}],
    "trailingPegRatio": 2.1,
}


def test_snapshot_has_exact_shape_and_values(client, fake_yfinance):
    fake_yfinance.info_payload = AAPL_INFO

    resp = client.get("/stock/aapl")

    assert resp.status_code == 200
    body = json.loads(resp.text)
    assert set(body) == EXPECTED_KEYS
    assert body == {
        "ticker": "AAPL",
        "sector": "Technology",
        "fulldayPrice": 347.725,
        "currency": "USD",
        "symbol": "AAPL",
        "language": "en-US",
        "longName": "Apple Inc.",
        "exchange": "NMS",
        "market": "us_market",
        "displayName": "Apple",
    }
    assert "X-Cache" not in resp.headers  # nothing is cached


def test_snapshot_charges_two_upstream_calls_every_time(client, fake_yfinance):
    fake_yfinance.info_payload = AAPL_INFO
    before = stock_router._limiter.remaining()

    client.get("/stock/AAPL")
    after_first = stock_router._limiter.remaining()
    client.get("/stock/AAPL")

    assert before - after_first == 2, "yfinance .info = quoteSummary + quote = 2 HTTP requests"
    assert after_first - stock_router._limiter.remaining() == 2, "no cache: the second call pays again"


def test_snapshot_price_fallbacks(client, fake_yfinance):
    info = dict(AAPL_INFO)
    del info["regularMarketPrice"]
    fake_yfinance.info_payload = info
    assert client.get("/stock/AAPL").json()["fulldayPrice"] == 347.7  # currentPrice

    del info["currentPrice"]
    assert client.get("/stock/AAPL").json()["fulldayPrice"] == 345.1  # previousClose


def test_snapshot_etf_without_sector_or_display_name(client, fake_yfinance):
    fake_yfinance.info_payload = {
        "symbol": "VTI",
        "regularMarketPrice": 250.0,
        "currency": "USD",
        "language": "en-US",
        "shortName": "Vanguard Total Stock Market ETF",
        "longName": "Vanguard Total Stock Market Index Fund ETF Shares",
        "exchange": "PCX",
        "market": "us_market",
    }

    body = client.get("/stock/VTI").json()

    assert body["sector"] is None
    assert body["displayName"] == "Vanguard Total Stock Market ETF"  # shortName fallback
    assert body["longName"].startswith("Vanguard Total Stock Market Index")


def test_snapshot_404_for_unknown_ticker(client, fake_yfinance):
    # yfinance returns a near-empty dict for unknown symbols.
    fake_yfinance.info_payload = {"trailingPegRatio": None}
    assert client.get("/stock/NOPE123").status_code == 404

    fake_yfinance.info_payload = {}
    assert client.get("/stock/NOPE123").status_code == 404


def test_snapshot_429_when_budget_exhausted(client, fake_yfinance, monkeypatch):
    fake_yfinance.info_payload = AAPL_INFO
    monkeypatch.setattr(stock_router._limiter, "max_calls", 1)  # .info needs 2

    assert client.get("/stock/AAPL").status_code == 429


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------
def test_removed_routes_are_gone(client):
    assert client.get("/stock/AAPL/hourly").status_code == 404
    assert client.get("/stock/AAPL/history").status_code == 404
    assert client.get("/stock/hourly/batch?tickers=AAPL").status_code == 404


def test_openapi_lists_exactly_two_stock_routes():
    from main import app

    stock_paths = sorted(p for p in app.openapi()["paths"] if p.startswith("/stock"))
    assert stock_paths == ["/stock/{ticker}", "/stock/{ticker}/historic"]


def test_status_endpoint_and_time_removed(client):
    assert client.get("/status").json()["status"] == "online"
    assert client.get("/time").status_code == 404
