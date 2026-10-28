"""Tests for GET /stock/{ticker}/historic - what the records contain:
OHLC per bar, downsampling as OHLC aggregation, serialisation. The window
rule (D' resolution, DAY/WEEK/calendar spans, referenceDate lists,
validation, error mapping, dotted symbols) is covered in
test_window_rule.py.
"""

import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

import stock_router
from tests.conftest import EXCHANGE_TZ, make_market
from tests.test_window_rule import FRIDAY, HOLIDAY, MONDAY, get, only, weekday_back

RESPONSE_KEYS = {"ticker", "symbol", "period", "interval", "referenceDate", "window", "count", "data"}
RECORD_KEYS = {"datetime", "open", "high", "low", "close"}
# Planted extremes: the synthetic market's prices are ~100..25000.
HIGH_SPIKE = 1_000_000.0
LOW_SPIKE = 0.01


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


def _datetimes(result):
    return [p["datetime"] for p in result["data"]]


def _record(result, stamp_prefix: str) -> dict:
    (match,) = [p for p in result["data"] if p["datetime"].startswith(stamp_prefix)]
    return match


def _ohlc(bars: pd.DataFrame) -> dict:
    """The OHLC aggregate of a run of bars, rounded like the API."""
    return {
        "open": round(float(bars["Open"].iloc[0]), 4),
        "high": round(float(bars["High"].max()), 4),
        "low": round(float(bars["Low"].min()), 4),
        "close": round(float(bars["Close"].iloc[-1]), 4),
    }


def _spike(df: pd.DataFrame, ts: pd.Timestamp, *, high: float | None = None, low: float | None = None) -> None:
    if high is not None:
        df.loc[ts, "High"] = high
    if low is not None:
        df.loc[ts, "Low"] = low


# --------------------------------------------------------------------------
# Response contract
# --------------------------------------------------------------------------
def test_default_period_is_day_and_shape_is_exact(client, market):
    resp = client.get("/stock/aapl/historic")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert set(body) == RESPONSE_KEYS
    assert body["ticker"] == "AAPL" and body["symbol"] == "AAPL"
    assert body["period"] == "DAY" and body["interval"] == "1h"
    assert body["count"] == len(body["data"])
    assert all(set(p) == RECORD_KEYS for p in body["data"])
    assert all(isinstance(p[k], float) for p in body["data"] for k in ("open", "high", "low", "close"))
    assert all(p["low"] <= min(p["open"], p["close"]) <= max(p["open"], p["close"]) <= p["high"] for p in body["data"])


@pytest.mark.parametrize("raw", ["day", "Week", " MONTH "])
def test_period_is_case_insensitive(client, market, raw):
    resp = client.get(f"/stock/AAPL/historic?period={raw}")
    assert resp.status_code == 200
    assert resp.json()["period"] == raw.strip().upper()


@pytest.mark.parametrize(
    "period,label", [("DAY", "1h"), ("WEEK", "1d"), ("MONTH", "1d"), ("HALF_YEAR", "2wk"), ("YEAR", "2wk"), ("FIVE_YEAR", "1mo")]
)
def test_interval_label_per_period(client, market, period, label):
    assert client.get(f"/stock/AAPL/historic?period={period}").json()["interval"] == label


# --------------------------------------------------------------------------
# DAY: the hourly bars of the session
# --------------------------------------------------------------------------
def test_day_is_the_sessions_hourly_ohlc_bars_anchored_on_the_open(client, market):
    result = only(get(client, "DAY", FRIDAY).json())

    assert result["count"] == 7
    assert [d[11:16] for d in _datetimes(result)] == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert all(d.startswith(FRIDAY.isoformat()) for d in _datetimes(result))
    assert all(d.endswith(("-04:00", "-05:00")) for d in _datetimes(result))  # exchange UTC offset kept
    bars = market.bars("60m", FRIDAY, FRIDAY)
    for record, (ts, bar) in zip(result["data"], bars.iterrows()):
        assert record["datetime"] == ts.isoformat()
        assert record == {"datetime": ts.isoformat(), **_ohlc(bars.loc[[ts]])}


def test_day_includes_the_running_bar_while_the_market_is_open(client, fake_yfinance, clock):
    wednesday = weekday_back(10, 2)
    now = clock(pd.Timestamp(f"{wednesday} 13:00", tz=EXCHANGE_TZ))
    market = make_market(now=now)
    fake_yfinance.window_responses = market.respond

    body = client.get("/stock/AAPL/historic?period=DAY").json()

    assert [d[11:16] for d in _datetimes(body)] == ["09:30", "10:30", "11:30", "12:30"]  # 12:30 bar is running
    assert body["data"][-1]["close"] == round(float(market.hourly["Close"].iloc[-1]), 4)


def test_the_high_of_a_session_sits_in_the_hourly_bar_it_happened_in(client, market):
    bars = market.bars("60m", FRIDAY, FRIDAY)
    _spike(market.hourly, bars.index[3], high=HIGH_SPIKE)  # the 12:30 bar
    _spike(market.hourly, bars.index[5], low=LOW_SPIKE)  # the 14:30 bar

    result = only(get(client, "DAY", FRIDAY).json())

    assert max(p["high"] for p in result["data"]) == HIGH_SPIKE
    assert _record(result, f"{FRIDAY}T12:30")["high"] == HIGH_SPIKE
    assert _record(result, f"{FRIDAY}T14:30")["low"] == LOW_SPIKE
    assert _record(result, f"{FRIDAY}T12:30")["close"] < HIGH_SPIKE  # the close is still the close


def test_day_ignores_the_neighbouring_sessions(client, market):
    wednesday = weekday_back(20, 2)
    if wednesday == HOLIDAY:
        wednesday -= timedelta(days=7)
    before = market.bars("60m", wednesday - timedelta(days=1), wednesday - timedelta(days=1))
    after = market.bars("60m", wednesday + timedelta(days=1), wednesday + timedelta(days=1))
    _spike(market.hourly, before.index[-1], high=HIGH_SPIKE, low=LOW_SPIKE)  # 15:30 the day before
    _spike(market.hourly, after.index[0], high=HIGH_SPIKE, low=LOW_SPIKE)  # 09:30 the day after

    result = only(get(client, "DAY", wednesday).json())

    assert max(p["high"] for p in result["data"]) < HIGH_SPIKE
    assert min(p["low"] for p in result["data"]) > LOW_SPIKE


# --------------------------------------------------------------------------
# Daily periods: one record per trading day, then downsampled as OHLC
# --------------------------------------------------------------------------
def test_week_is_one_ohlc_record_per_session(client, market):
    result = only(get(client, "WEEK", FRIDAY).json())

    sessions = market.sessions_on_or_before(FRIDAY)[-5:]
    assert _datetimes(result) == [s.isoformat() for s in sessions]
    for record, s in zip(result["data"], sessions):
        assert record == {"datetime": s.isoformat(), **_ohlc(market.bars("1d", s, s))}


def test_week_ignores_the_sixth_session_back_and_the_next_session(client, market):
    sessions = market.sessions_on_or_before(FRIDAY)
    sixth_back, next_session = sessions[-6], [s for s in market.sessions if s > FRIDAY][0]
    _spike(market.daily, market.bars("1d", sixth_back, sixth_back).index[0], high=HIGH_SPIKE)
    _spike(market.daily, market.bars("1d", next_session, next_session).index[0], high=HIGH_SPIKE)
    _spike(market.daily, market.bars("1d", sessions[-5], sessions[-5]).index[0], low=LOW_SPIKE)  # first session IN the window

    result = only(get(client, "WEEK", FRIDAY).json())

    assert max(p["high"] for p in result["data"]) < HIGH_SPIKE
    assert result["data"][0]["low"] == LOW_SPIKE


def test_month_is_one_record_per_session_in_30_days_and_includes_its_first_day_only(client, market):
    start = MONDAY - timedelta(days=29)  # a Sunday -> the first session in the window is the Monday after
    first_session = [s for s in market.sessions if s >= start][0]
    day_before = [s for s in market.sessions if s < start][-1]
    _spike(market.daily, market.bars("1d", first_session, first_session).index[0], high=HIGH_SPIKE)
    _spike(market.daily, market.bars("1d", day_before, day_before).index[0], low=LOW_SPIKE)

    result = only(get(client, "MONTH", MONDAY).json())

    expected = market.bars("1d", start, MONDAY)
    assert 20 <= result["count"] <= 23
    assert _datetimes(result) == [ts.date().isoformat() for ts in expected.index]
    assert result["data"][0]["high"] == HIGH_SPIKE
    assert min(p["low"] for p in result["data"]) > LOW_SPIKE


@pytest.mark.parametrize("period,days,expected_records", [("HALF_YEAR", 182, 13), ("YEAR", 365, 26)])
def test_fortnightly_records_aggregate_14_day_bins_anchored_at_the_newest_bar(client, market, period, days, expected_records):
    window = market.bars("1d", FRIDAY - timedelta(days=days - 1), FRIDAY)
    # plant a high mid-bin (not on a bin's last day) and a low on a bin's first day
    by_bin = {}
    for ts in window.index:
        by_bin.setdefault((FRIDAY - ts.date()).days // 14, []).append(ts)
    mid_bin = by_bin[3][len(by_bin[3]) // 2]
    first_of_bin = by_bin[5][0]
    _spike(market.daily, mid_bin, high=HIGH_SPIKE)
    _spike(market.daily, first_of_bin, low=LOW_SPIKE)
    window = market.bars("1d", FRIDAY - timedelta(days=days - 1), FRIDAY)

    result = only(get(client, period, FRIDAY).json())

    assert expected_records <= result["count"] <= expected_records + 1
    # one record per bin, stamped with the bin's last session, OHLC aggregated over the bin
    expected = []
    for key in sorted(by_bin, reverse=True):
        bars = window.loc[by_bin[key]]
        expected.append({"datetime": by_bin[key][-1].date().isoformat(), **_ohlc(bars)})
    assert result["data"] == expected
    assert _datetimes(result)[-1] == FRIDAY.isoformat()  # anchored at the newest bar
    assert _record(result, by_bin[3][-1].date().isoformat())["high"] == HIGH_SPIKE  # mid-bin high surfaces
    assert _record(result, by_bin[5][-1].date().isoformat())["low"] == LOW_SPIKE


def test_five_year_records_aggregate_calendar_months(client, market):
    result = only(get(client, "FIVE_YEAR", FRIDAY).json())

    assert 60 <= result["count"] <= 61
    months = [d[:7] for d in _datetimes(result)]
    assert len(set(months)) == len(months)  # one per month
    assert _datetimes(result)[-1] == FRIDAY.isoformat()  # current (partial) month = up to its latest close
    window = market.bars("1d", FRIDAY - timedelta(days=5 * 365 + 1), FRIDAY)
    for record in result["data"]:
        month_bars = window[window.index.strftime("%Y-%m") == record["datetime"][:7]]
        assert record == {"datetime": month_bars.index[-1].date().isoformat(), **_ohlc(month_bars)}
        d = pd.Timestamp(record["datetime"]).date()
        if record is not result["data"][-1]:
            assert [s for s in market.sessions if s > d][0].month != d.month  # the month's last session


def test_daily_records_carry_a_date_only(client, market):
    body = client.get("/stock/AAPL/historic?period=MONTH").json()
    assert all(len(p["datetime"]) == 10 and p["datetime"].count("-") == 2 for p in body["data"])


# --------------------------------------------------------------------------
# NaN handling
# --------------------------------------------------------------------------
def test_bars_without_a_close_are_skipped_and_missing_fields_are_null(client, market):
    bars = market.bars("60m", FRIDAY, FRIDAY)
    market.hourly.loc[bars.index[:2], "Close"] = np.nan  # 09:30, 10:30 unusable
    market.hourly.loc[bars.index[3], "High"] = np.nan  # 12:30 has no High

    resp = get(client, "DAY", FRIDAY)

    result = only(_strict_json(resp.text))  # NaN must not leak into JSON
    assert [d[11:16] for d in _datetimes(result)] == ["11:30", "12:30", "13:30", "14:30", "15:30"]
    assert _record(result, f"{FRIDAY}T12:30")["high"] is None
    assert _record(result, f"{FRIDAY}T12:30")["close"] is not None


def test_nan_bars_inside_a_bin_are_skipped_in_the_aggregate(client, market):
    window = market.bars("1d", FRIDAY - timedelta(days=181), FRIDAY)
    market.daily.loc[window.index[-2], ["Open", "High", "Low", "Close"]] = np.nan  # the session before FRIDAY

    result = only(get(client, "HALF_YEAR", FRIDAY).json())

    last_bin = market.bars("1d", FRIDAY - timedelta(days=13), FRIDAY).drop(window.index[-2])
    assert result["data"][-1] == {"datetime": FRIDAY.isoformat(), **_ohlc(last_bin)}


def test_all_nan_window_is_404_not_500(client, market):
    bars = market.bars("60m", FRIDAY, FRIDAY)
    market.hourly.loc[bars.index, ["Open", "High", "Low", "Close"]] = np.nan

    assert get(client, "DAY", FRIDAY).status_code == 404


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
def test_invalid_period_is_422_without_upstream_call(client, market):
    before = stock_router._limiter.remaining()

    resp = client.get("/stock/AAPL/historic?period=DECADE")

    assert resp.status_code == 422
    detail = json.dumps(resp.json())
    for allowed in ("DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"):
        assert allowed in detail
    assert stock_router.yf.Ticker.calls == []
    assert stock_router._limiter.remaining() == before


def test_historic_route_does_not_shadow_other_ticker_routes(client, market):
    assert client.get("/stock/AAPL/history").status_code == 404


def test_the_exchanges_today_is_reported_as_the_rolling_reference_date(client, market):
    expected = pd.Timestamp.now(tz=EXCHANGE_TZ).date().isoformat()
    assert client.get("/stock/AAPL/historic").json()["referenceDate"] == expected
