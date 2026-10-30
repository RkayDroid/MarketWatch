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
from datetime import date, datetime, timedelta, timezone

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


PERIODS = ["DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"]
NOMINAL_SPAN = {"DAY": 1, "WEEK": 7, "MONTH": 30, "HALF_YEAR": 182, "YEAR": 365, "FIVE_YEAR": 5 * 365 + 2}


def _recent_weekday(days_back: int):
    d = datetime.now(timezone.utc).date() - timedelta(days=days_back)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _window(body):
    return date.fromisoformat(body["window"]["start"]), date.fromisoformat(body["window"]["end"])


def _ohlc_sane(record) -> bool:
    return 0 < record["low"] <= min(record["open"], record["close"]) <= max(record["open"], record["close"]) <= record["high"]


# --------------------------------------------------------------------------
# The window rule against real data, rolling
# --------------------------------------------------------------------------
@pytest.mark.parametrize("period", PERIODS)
def test_live_historic_rolling(client, period):
    resp = client.get(f"/stock/AAPL/historic?period={period}")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert body["period"] == period and body["count"] >= 1
    start, end = _window(body)
    today = datetime.now(timezone.utc).date()
    assert today - timedelta(days=5) <= end <= today + timedelta(days=1)  # the last session
    assert end.weekday() < 5
    assert body["referenceDate"] in (today.isoformat(), (today - timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat())
    stamps = [p["datetime"] for p in body["data"]]
    assert stamps == sorted(stamps)
    assert all(start.isoformat() <= s[:10] <= end.isoformat() for s in stamps)
    assert stamps[-1][:10] == end.isoformat()
    assert all(set(p) == {"datetime", "open", "high", "low", "close"} for p in body["data"])
    assert all(_ohlc_sane(p) for p in body["data"])
    if period == "DAY":
        assert start == end and 1 <= body["count"] <= 8  # hourly bars of one session (partial while open)
    elif period == "WEEK":
        assert body["count"] == 5 and (end - start).days in range(6, 12)  # 5 sessions
    else:
        assert (end - start).days == NOMINAL_SPAN[period] - 1
        assert all(len(s) == 10 for s in stamps)


def test_live_aggregated_records_contain_the_daily_extremes(client):
    """A YEAR's last fortnight record must carry the extremes of the daily
    bars it aggregates (served by a MONTH window over the same days).

    Both windows are anchored on the same *past* weekday so neither contains
    the running session: a rolling YEAR and an anchored MONTH are two live
    calls, and during market hours Yahoo's bar for today changes between
    them (high/close tick by tick), which made the rolling version of this
    test fail whenever it ran while the market was open. The bin is taken
    from the documented rule - the 14 calendar days ending on D' - not
    inferred from the previous record's stamp; bars with a null high/low
    are ignored; prices are compared to the cent since the two windows come
    from two different upstream range queries."""
    reference = _recent_weekday(3).isoformat()  # a completed session (D' resolves holidays)
    year = _strict_json(client.get(f"/stock/AAPL/historic?period=YEAR&referenceDate={reference}").text)
    year_window = year["results"][reference]
    end = year_window["window"]["end"]
    assert end <= reference
    month = _strict_json(client.get(f"/stock/AAPL/historic?period=MONTH&referenceDate={end}").text)["results"][end]
    assert month["window"]["end"] == end

    bin_start = (date.fromisoformat(end) - timedelta(days=13)).isoformat()
    daily_in_last_bin = [p for p in month["data"] if bin_start <= p["datetime"] <= end]
    assert daily_in_last_bin and daily_in_last_bin[-1]["datetime"] == end
    highs = [p["high"] for p in daily_in_last_bin if p["high"] is not None]
    lows = [p["low"] for p in daily_in_last_bin if p["low"] is not None]

    last = year_window["data"][-1]
    assert last["datetime"] == end
    cent = lambda expected: pytest.approx(expected, abs=0.01)  # noqa: E731
    assert last["high"] == cent(max(highs))
    assert last["low"] == cent(min(lows))
    assert last["close"] == cent(daily_in_last_bin[-1]["close"])
    assert last["open"] == cent(daily_in_last_bin[0]["open"])


def test_live_each_window_is_one_upstream_call(client):
    import stock_router

    before = stock_router._limiter.remaining()
    for period in PERIODS:
        assert client.get(f"/stock/MSFT/historic?period={period}").status_code == 200
    assert before - stock_router._limiter.remaining() == 6  # no cache: 1 per request


def test_live_unknown_ticker_historic_is_404_not_500(client):
    assert client.get("/stock/THISTICKERDOESNOTEXIST123/historic").status_code in (404, 502)


def test_live_highlow_endpoint_is_gone(client):
    assert client.get("/stock/AAPL/highlow").status_code == 404


# --------------------------------------------------------------------------
# referenceDate
# --------------------------------------------------------------------------
def test_live_week_ending_on_a_past_weekday(client):
    import stock_router

    before = stock_router._limiter.remaining()
    reference = _recent_weekday(45)

    resp = client.get(f"/stock/AAPL/historic?period=WEEK&referenceDate={reference.isoformat()}")

    assert resp.status_code == 200, resp.text
    h = _strict_json(resp.text)["results"][reference.isoformat()]
    assert h["window"]["end"] == reference.isoformat()  # a weekday (barring a holiday) is its own D'
    assert h["count"] == 5 and h["data"][-1]["datetime"] == reference.isoformat()
    assert all(_ohlc_sane(p) for p in h["data"])
    assert before - stock_router._limiter.remaining() == 1


def test_live_sunday_resolves_to_friday(client):
    weekday = _recent_weekday(20)
    sunday = weekday - timedelta(days=weekday.weekday() + 1)
    friday = sunday - timedelta(days=2)

    resp = client.get(f"/stock/AAPL/historic?period=DAY&referenceDate={sunday.isoformat()}")

    assert resp.status_code == 200, resp.text
    result = _strict_json(resp.text)["results"][sunday.isoformat()]
    assert result["window"] == {"start": friday.isoformat(), "end": friday.isoformat()}
    assert 6 <= result["count"] <= 8  # a regular NYSE session at hourly bars
    assert all(p["datetime"].startswith(friday.isoformat()) for p in result["data"])
    assert datetime.fromisoformat(result["data"][0]["datetime"]).strftime("%H:%M") == "09:30"  # open-anchored bars


def test_live_multiple_reference_dates(client):
    import stock_router

    before = stock_router._limiter.remaining()
    dates = [_recent_weekday(n) for n in (45, 100, 200)]

    resp = client.get("/stock/AAPL/historic?period=WEEK&referenceDate=" + ",".join(d.isoformat() for d in dates))

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert list(body["results"]) == [d.isoformat() for d in dates]
    for d in dates:
        assert body["results"][d.isoformat()]["count"] == 5
    assert before - stock_router._limiter.remaining() == 3  # one live call per date


def test_live_beyond_retention_is_422_without_a_call(client):
    import stock_router

    before = stock_router._limiter.remaining()
    resp = client.get("/stock/AAPL/historic?period=DAY&referenceDate=2019-01-02")
    assert resp.status_code == 422 and "hourly" in resp.json()["detail"]
    assert stock_router._limiter.remaining() == before


def test_live_month_ending_years_ago(client):
    body = _strict_json(client.get("/stock/AAPL/historic?period=MONTH&referenceDate=2015-03-31").text)

    h = body["results"]["2015-03-31"]
    assert h["window"] == {"start": "2015-03-02", "end": "2015-03-31"}
    assert 19 <= h["count"] <= 23 and h["data"][-1]["datetime"] == "2015-03-31"
    assert all(_ohlc_sane(p) for p in h["data"])


def test_live_date_before_listing_is_null(client):
    resp = client.get("/stock/AAPL/historic?period=MONTH&referenceDate=1970-01-01,2015-03-31")
    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert body["results"]["1970-01-01"] is None
    assert body["results"]["2015-03-31"]["count"] > 0


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
    assert _strict_json(resp.text)["symbol"] == "BRK-B"


def test_live_snapshot_dotted_share_class(client):
    resp = client.get("/stock/BRK.B")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert body["ticker"] == "BRK.B"
    assert body["symbol"] == "BRK-B"  # Yahoo's canonical form
    assert body["fulldayPrice"] > 0
