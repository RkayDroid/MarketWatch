"""Integration tests: dotted ticker symbols through every endpoint
(exchange suffixes pass through, US share classes fall back to Yahoo's
dash form). The historic cases live in test_window_rule.py; this file
keeps the snapshot cases and the budget accounting."""

import pandas as pd

import stock_router
from tests.test_snapshot_endpoint import AAPL_INFO
from tests.test_window_rule import FRIDAY


def _dash_only(fake_yfinance, market):
    """A yfinance whose chart data exists only under the dashed symbol."""

    class DashOnly(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if self.symbol == "BRK-B":
                return market.respond(start, end, interval)
            return pd.DataFrame()

    return DashOnly


def test_exchange_suffix_symbols_route_and_resolve_directly(client, market):
    for symbol in ("sap.de", "0700.HK", "BARC.L"):
        resp = client.get(f"/stock/{symbol}/historic?period=DAY")
        assert resp.status_code == 200, resp.text
        assert resp.json()["ticker"] == symbol.upper() and resp.json()["symbol"] == symbol.upper()
    assert [c["symbol"] for c in stock_router.yf.Ticker.calls] == ["SAP.DE", "0700.HK", "BARC.L"]  # no retries


def test_us_share_class_dot_falls_back_to_dash_rolling_and_anchored(client, fake_yfinance, market):
    stock_router.yf.Ticker = _dash_only(fake_yfinance, market)
    before = stock_router._limiter.remaining()

    rolling = client.get("/stock/BRK.B/historic?period=WEEK")
    anchored = client.get(f"/stock/BRK.B/historic?period=YEAR&referenceDate={FRIDAY}")

    assert rolling.status_code == 200 and anchored.status_code == 200
    for body in (rolling.json(), anchored.json()):
        assert body["ticker"] == "BRK.B" and body["symbol"] == "BRK-B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B", "BRK.B", "BRK-B"]
    assert before - stock_router._limiter.remaining() == 4  # the retry is charged


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
    resp = client.get("/stock/NO.PE/historic?period=DAY")

    assert resp.status_code == 404
    assert [c["symbol"] for c in fake_yfinance.calls] == ["NO.PE", "NO-PE"]  # one window, two forms
