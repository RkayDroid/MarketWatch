"""Integration tests: dotted ticker symbols through every endpoint
(exchange suffixes pass through, US share classes fall back to Yahoo's
dash form)."""

import pandas as pd

import stock_router
from tests.conftest import make_ohlc_frame
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
    assert resp.json()["symbol"] == "BRK-B"  # the form Yahoo answered for, as in the snapshot

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



def test_exchange_suffix_symbol_is_reported_unchanged(client, fake_yfinance, hourly_30_bars):
    fake_yfinance.responses = _frames_for_symbols(fake_yfinance, {"SAP.DE"}, hourly_30_bars)
    body = client.get("/stock/SAP.DE/historic?period=DAY").json()
    assert body["ticker"] == "SAP.DE" and body["symbol"] == "SAP.DE"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["SAP.DE"]


def test_dash_fallback_also_fires_when_yfinance_raises_for_the_dotted_form(client, fake_yfinance, hourly_30_bars):
    """With yf.config.debug.hide_exceptions = False yfinance raises
    YFTickerMissingError (tz/prices missing) instead of returning an empty
    frame for an unknown symbol. The retry must treat that the same way."""
    from yfinance.exceptions import YFTzMissingError

    class RaisesForDotted(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "period": period, "interval": interval})
            if "." in self.symbol:
                raise YFTzMissingError(self.symbol)
            return hourly_30_bars

    stock_router.yf.Ticker = RaisesForDotted
    resp = client.get("/stock/BRK.B/historic?period=DAY")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "BRK.B" and resp.json()["symbol"] == "BRK-B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B"]
