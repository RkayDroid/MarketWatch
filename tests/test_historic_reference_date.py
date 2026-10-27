"""Tests for GET /stock/{symbol}/historic?period=...&referenceDate=YYYY-MM-DD[,YYYY-MM-DD...]

The window keeps `period`'s length but ENDS on each referenceDate
(inclusive) instead of now. One yfinance call per date, no caching. The
response is keyed by date:

    {"ticker", "symbol", "period", "interval",
     "results": {"2026-10-23": {"count": N, "data": [...]}, ...}}
"""

import json
from datetime import date, timedelta

import pandas as pd
import pytest
from yfinance.exceptions import YFException, YFRateLimitError

import stock_router
from tests.conftest import EXCHANGE_TZ, make_ohlc_frame, slice_like_yahoo

TODAY = date.today()
RESPONSE_KEYS = {"ticker", "symbol", "period", "interval", "results"}


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


def _get(client, period, *reference_dates, ticker="AAPL"):
    joined = ",".join(d if isinstance(d, str) else d.isoformat() for d in reference_dates)
    return client.get(f"/stock/{ticker}/historic?period={period}&referenceDate={joined}")


def _only(body: dict) -> dict:
    """The single per-date result of a one-date request."""
    (result,) = body["results"].values()
    return result


def _dates(result: dict):
    return [p["datetime"] for p in result["data"]]


def _day(ts: str) -> str:
    """Exchange-local calendar date of an ISO timestamp with offset."""
    return ts[:10]


def _window(df: pd.DataFrame, reference_date: date, days: int) -> pd.DataFrame:
    """Bars of the `days`-long exchange-local window ending on reference_date."""
    return slice_like_yahoo(df, reference_date - timedelta(days=days - 1), reference_date + timedelta(days=1))


def _weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


@pytest.fixture
def daily_history():
    """~20 years of business-day bars ending today."""
    return make_ohlc_frame(periods=5200, freq="B", intraday=False, end=pd.Timestamp.now(tz=EXCHANGE_TZ).normalize())


@pytest.fixture
def hourly_history():
    """Hourly bars for the last ~600 days, 24 bars/day, ending now. Bars
    are round-the-clock so one exchange-local calendar day = its 24 bars."""
    return make_ohlc_frame(periods=24 * 600, freq="h", end=pd.Timestamp.now(tz=EXCHANGE_TZ).floor("h"))


@pytest.fixture
def yahoo(fake_yfinance, daily_history, hourly_history):
    """Answer history(start=, end=) like Yahoo would, from the fixtures."""

    def respond(start, end, interval):
        source = hourly_history if interval == "60m" else daily_history
        return slice_like_yahoo(source, start, end)

    fake_yfinance.window_responses = respond
    return fake_yfinance


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------
def test_single_reference_date_response_is_keyed_by_that_date(client, yahoo):
    reference = TODAY - timedelta(days=60)

    resp = _get(client, "WEEK", reference, ticker="aapl")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert set(body) == RESPONSE_KEYS
    assert body["ticker"] == "AAPL"
    assert body["symbol"] == "AAPL"  # no translation needed
    assert body["period"] == "WEEK"
    assert body["interval"] == "1d"
    assert list(body["results"]) == [reference.isoformat()]
    result = _only(body)
    assert set(result) == {"count", "data"}
    assert result["count"] == len(result["data"])
    assert all(set(p) == {"datetime", "price"} for p in result["data"])


def test_rolling_window_response_has_no_reference_date_keys(client, yahoo, daily_history):
    yahoo.responses = lambda period, interval: daily_history if interval == "1d" else pd.DataFrame()

    body = client.get("/stock/AAPL/historic?period=WEEK").json()

    assert set(body) == {"ticker", "symbol", "period", "interval", "count", "data"}


def test_period_is_still_case_insensitive_with_reference_date(client, yahoo):
    resp = client.get(f"/stock/AAPL/historic?period=month&referenceDate={(TODAY - timedelta(days=40)).isoformat()}")
    assert resp.status_code == 200
    assert resp.json()["period"] == "MONTH"


# --------------------------------------------------------------------------
# Multiple reference dates
# --------------------------------------------------------------------------
def test_multiple_dates_are_keyed_in_the_order_given(client, yahoo, daily_history):
    dates = [TODAY - timedelta(days=n) for n in (100, 30, 400)]  # deliberately unsorted

    resp = _get(client, "WEEK", *dates)

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert list(body["results"]) == [d.isoformat() for d in dates]
    for d in dates:
        expected = _window(daily_history, d, 7)
        result = body["results"][d.isoformat()]
        assert result["count"] == len(expected) == 5
        assert _dates(result) == [ts.date().isoformat() for ts in expected.index]
        assert [p["price"] for p in result["data"]] == [round(float(c), 4) for c in expected["Close"]]


def test_each_date_result_equals_the_single_date_result(client, yahoo):
    dates = [TODAY - timedelta(days=n) for n in (20, 50, 80)]

    combined = _get(client, "MONTH", *dates).json()
    singles = {d.isoformat(): _only(_get(client, "MONTH", d).json()) for d in dates}

    assert combined["results"] == singles


def test_each_date_is_one_upstream_call_with_its_own_window(client, yahoo):
    dates = [TODAY - timedelta(days=n) for n in (10, 20, 30)]
    before = stock_router._limiter.remaining()

    assert _get(client, "WEEK", *dates).status_code == 200

    assert yahoo.calls == [
        {"symbol": "AAPL", "start": d - timedelta(days=6), "end": d + timedelta(days=1), "interval": "1d"} for d in dates
    ]
    assert before - stock_router._limiter.remaining() == 3


def test_five_dates_are_accepted(client, yahoo):
    dates = [TODAY - timedelta(days=n) for n in (10, 20, 30, 40, 50)]

    resp = _get(client, "WEEK", *dates)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 5
    assert len(yahoo.calls) == 5


def test_whitespace_around_dates_is_tolerated(client, yahoo):
    a, b = TODAY - timedelta(days=10), TODAY - timedelta(days=20)

    resp = client.get(f"/stock/AAPL/historic?period=WEEK&referenceDate= {a.isoformat()} , {b.isoformat()}")

    assert resp.status_code == 200, resp.text
    assert list(resp.json()["results"]) == [a.isoformat(), b.isoformat()]


def test_multiple_dates_work_for_day_period(client, yahoo, hourly_history):
    dates = [_weekday(TODAY - timedelta(days=n)) for n in (10, 40)]

    body = _get(client, "DAY", *dates).json()

    assert body["interval"] == "1h"
    for d in dates:
        result = body["results"][d.isoformat()]
        assert result["count"] == 24
        assert {_day(s) for s in _dates(result)} == {d.isoformat()}
    assert [c["interval"] for c in yahoo.calls] == ["60m", "60m"]


def test_date_without_bars_is_empty_not_an_error_when_others_have_data(client, fake_yfinance):
    frame = make_ohlc_frame(periods=24 * 100, freq="h")
    frame = frame[frame.index.weekday < 5]  # no weekend bars
    fake_yfinance.window_responses = lambda start, end, interval: slice_like_yahoo(frame, start, end)
    sunday = TODAY - timedelta(days=30)
    sunday -= timedelta(days=(sunday.weekday() - 6) % 7)
    friday = sunday - timedelta(days=2)

    resp = _get(client, "DAY", friday, sunday)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][sunday.isoformat()] == {"count": 0, "data": []}
    assert body["results"][friday.isoformat()]["count"] == 24


def test_404_when_every_date_is_empty(client, fake_yfinance):
    a, b = TODAY - timedelta(days=10), TODAY - timedelta(days=20)

    resp = _get(client, "WEEK", a, b, ticker="NOPE")

    assert resp.status_code == 404
    assert a.isoformat() in resp.json()["detail"] and b.isoformat() in resp.json()["detail"]


def test_budget_exhausted_midway_fails_the_whole_request(client, yahoo, monkeypatch):
    monkeypatch.setattr(stock_router._limiter, "max_calls", 1)

    resp = _get(client, "WEEK", TODAY - timedelta(days=10), TODAY - timedelta(days=20))

    assert resp.status_code == 429
    assert len(yahoo.calls) == 1  # the second date never got to Yahoo


def test_dotted_share_class_symbol_is_reported_once_for_all_dates(client, fake_yfinance, daily_history):
    class DashOnly(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if self.symbol == "BRK-B":
                return slice_like_yahoo(daily_history, start, end)
            return pd.DataFrame()

    stock_router.yf.Ticker = DashOnly
    resp = _get(client, "WEEK", TODAY - timedelta(days=10), TODAY - timedelta(days=20), ticker="BRK.B")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "BRK.B" and resp.json()["symbol"] == "BRK-B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B", "BRK.B", "BRK-B"]


# --------------------------------------------------------------------------
# List validation: rejected before any upstream call
# --------------------------------------------------------------------------
def _assert_422_without_upstream(client, yahoo, query: str, *fragments: str):
    before = stock_router._limiter.remaining()
    resp = client.get(f"/stock/AAPL/historic?{query}")
    assert resp.status_code == 422, resp.text
    detail = json.dumps(resp.json()["detail"])
    for fragment in fragments:
        assert fragment in detail, f"{fragment!r} not in {detail}"
    assert yahoo.calls == []
    assert stock_router._limiter.remaining() == before


def test_more_than_five_dates_is_422(client, yahoo):
    dates = ",".join((TODAY - timedelta(days=n)).isoformat() for n in range(10, 70, 10))  # 6 dates
    _assert_422_without_upstream(client, yahoo, f"period=WEEK&referenceDate={dates}", "at most 5", "got 6")


def test_duplicate_dates_are_422(client, yahoo):
    d = (TODAY - timedelta(days=10)).isoformat()
    other = (TODAY - timedelta(days=20)).isoformat()
    _assert_422_without_upstream(client, yahoo, f"period=WEEK&referenceDate={d},{other},{d}", "more than once", d)


def test_duplicate_detection_ignores_surrounding_whitespace(client, yahoo):
    d = (TODAY - timedelta(days=10)).isoformat()
    _assert_422_without_upstream(client, yahoo, f"period=WEEK&referenceDate={d}, {d}", "more than once")


@pytest.mark.parametrize("raw", ["{a},,{b}", "{a},", ",{a}", "", " "])
def test_empty_items_are_422(client, yahoo, raw):
    a, b = (TODAY - timedelta(days=10)).isoformat(), (TODAY - timedelta(days=20)).isoformat()
    _assert_422_without_upstream(client, yahoo, f"period=WEEK&referenceDate={raw.format(a=a, b=b)}", "empty item")


def test_one_malformed_date_in_the_list_rejects_the_whole_request(client, yahoo):
    good = (TODAY - timedelta(days=10)).isoformat()
    _assert_422_without_upstream(
        client, yahoo, f"period=WEEK&referenceDate={good},23-10-2026", "23-10-2026", "not a valid YYYY-MM-DD"
    )


def test_one_future_date_in_the_list_rejects_the_whole_request(client, yahoo):
    good = (TODAY - timedelta(days=10)).isoformat()
    future = (TODAY + timedelta(days=30)).isoformat()
    _assert_422_without_upstream(client, yahoo, f"period=WEEK&referenceDate={good},{future}", future, "future")


def test_one_date_beyond_hourly_retention_rejects_a_day_request(client, yahoo):
    good = (TODAY - timedelta(days=10)).isoformat()
    _assert_422_without_upstream(client, yahoo, f"period=DAY&referenceDate={good},2019-01-02", "2019-01-02", "730")


def test_validation_covers_the_whole_list_before_the_first_call(client, yahoo):
    """Even when the invalid item is last, nothing is fetched for the
    valid ones before it."""
    valid = ",".join((TODAY - timedelta(days=n)).isoformat() for n in (10, 20, 30, 40))
    _assert_422_without_upstream(client, yahoo, f"period=WEEK&referenceDate={valid},2026-13-01", "2026-13-01")


@pytest.mark.parametrize(
    "query",
    [
        "period=DAY&referenceDate=" + (TODAY + timedelta(days=2)).isoformat(),  # future
        "period=WEEK&referenceDate=" + (TODAY + timedelta(days=365)).isoformat(),  # future
        "period=DAY&referenceDate=" + (TODAY - timedelta(days=stock_router.HOURLY_RETENTION_DAYS + 1)).isoformat(),
        "period=DAY&referenceDate=2019-01-01",  # beyond hourly retention
        "period=WEEK&referenceDate=23-10-2026",  # wrong format
        "period=WEEK&referenceDate=2026/10/23",
        "period=WEEK&referenceDate=20261023",  # compact ISO form is not accepted either
        "period=WEEK&referenceDate=2026-13-01",  # impossible month
        "period=WEEK&referenceDate=2026-02-30",  # impossible day
        "period=WEEK&referenceDate=yesterday",
        "period=DECADE&referenceDate=2024-01-01",  # invalid period still 422
    ],
)
def test_invalid_single_reference_date_is_422_without_upstream_call(client, yahoo, query):
    _assert_422_without_upstream(client, yahoo, query)


def test_future_and_retention_errors_are_explained(client, yahoo):
    future = client.get(f"/stock/AAPL/historic?period=WEEK&referenceDate={(TODAY + timedelta(days=2)).isoformat()}").json()
    assert "future" in future["detail"]

    old = client.get("/stock/AAPL/historic?period=DAY&referenceDate=2019-01-01").json()
    assert "730" in old["detail"] and "DAY" in old["detail"]


def test_old_reference_date_is_fine_for_daily_periods(client, yahoo):
    """Only DAY has a retention limit; daily bars go back decades."""
    resp = client.get("/stock/AAPL/historic?period=WEEK&referenceDate=2019-01-11")
    assert resp.status_code == 200, resp.text
    assert _dates(_only(resp.json()))[-1] == "2019-01-11"


def test_tomorrow_utc_is_accepted_for_exchanges_ahead_of_utc(client, fake_yfinance):
    """At 23:00 UTC Tokyo is already on the next date; asking for that
    exchange's local 'today' must not be rejected as a future date."""
    tokyo = make_ohlc_frame(periods=10, freq="B", intraday=False)
    tokyo.index = pd.date_range(end=pd.Timestamp.now(tz="Asia/Tokyo").normalize(), periods=10, freq="B", tz="Asia/Tokyo", name="Date")
    fake_yfinance.window_responses = lambda start, end, interval: slice_like_yahoo(tokyo, start, end)

    resp = _get(client, "WEEK", TODAY + timedelta(days=1), ticker="7203.T")

    assert resp.status_code == 200, resp.text
    assert len(fake_yfinance.calls) == 1


# --------------------------------------------------------------------------
# The upstream call asks for exactly the window
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "period,days,interval",
    [("DAY", 1, "60m"), ("WEEK", 7, "1d"), ("MONTH", 30, "1d"), ("HALF_YEAR", 182, "1d"), ("YEAR", 365, "1d"), ("FIVE_YEAR", 5 * 365 + 2, "1d")],
)
def test_yfinance_is_called_with_start_inclusive_end_exclusive(client, yahoo, period, days, interval):
    reference = TODAY - timedelta(days=100)

    assert _get(client, period, reference).status_code == 200

    assert yahoo.calls == [
        {
            "symbol": "AAPL",
            "start": reference - timedelta(days=days - 1),  # first day of the window
            "end": reference + timedelta(days=1),  # the day after referenceDate (exclusive)
            "interval": interval,
        }
    ]


# --------------------------------------------------------------------------
# Window semantics (daily periods)
# --------------------------------------------------------------------------
def test_week_is_the_7_calendar_days_ending_on_reference_date(client, yahoo, daily_history):
    reference = TODAY - timedelta(days=100)

    result = _only(_get(client, "WEEK", reference).json())

    expected = _window(daily_history, reference, 7)
    assert _dates(result) == [ts.date().isoformat() for ts in expected.index]
    assert [p["price"] for p in result["data"]] == [round(float(c), 4) for c in expected["Close"]]
    assert result["count"] == 5  # 7 calendar days of business-day bars
    assert _dates(result)[0] >= (reference - timedelta(days=6)).isoformat()
    assert _dates(result)[-1] <= reference.isoformat()


def test_reference_date_itself_is_included_and_the_day_before_the_window_is_not(client, yahoo):
    # a Friday reference: the window Sat..Fri holds Mon..Fri
    reference = TODAY - timedelta(days=100)
    reference -= timedelta(days=(reference.weekday() - 4) % 7)
    assert reference.weekday() == 4

    result = _only(_get(client, "WEEK", reference).json())

    assert _dates(result)[-1] == reference.isoformat()  # inclusive end
    assert _dates(result)[0] == (reference - timedelta(days=4)).isoformat()  # Monday; previous Friday (-7) excluded
    assert result["count"] == 5


def test_month_is_30_calendar_days_ending_on_reference_date(client, yahoo, daily_history):
    reference = TODAY - timedelta(days=400)

    result = _only(_get(client, "MONTH", reference).json())

    expected = _window(daily_history, reference, 30)
    assert result["count"] == len(expected)
    assert 20 <= result["count"] <= 23
    assert _dates(result) == [ts.date().isoformat() for ts in expected.index]


@pytest.mark.parametrize("period,days,expected_records", [("HALF_YEAR", 182, 13), ("YEAR", 365, 26)])
def test_fortnightly_periods_ending_on_reference_date(client, yahoo, daily_history, period, days, expected_records):
    reference = TODAY - timedelta(days=300)

    body = _get(client, period, reference).json()
    result = _only(body)

    assert body["interval"] == "2wk"
    assert expected_records <= result["count"] <= expected_records + 1
    stamps = [pd.Timestamp(d) for d in _dates(result)]
    gaps = [(b - a).days for a, b in zip(stamps, stamps[1:])]
    assert all(12 <= g <= 16 for g in gaps[1:]), gaps

    window = _window(daily_history, reference, days)
    assert _dates(result)[0] >= (reference - timedelta(days=days - 1)).isoformat()
    # bins are anchored at the newest bar: the last record is the last close on/before referenceDate
    assert _dates(result)[-1] == window.index[-1].date().isoformat()
    assert result["data"][-1]["price"] == round(float(window["Close"].iloc[-1]), 4)
    closes_by_date = {ts.date().isoformat(): round(float(c), 4) for ts, c in window["Close"].items()}
    assert all(closes_by_date[p["datetime"]] == p["price"] for p in result["data"])


def test_five_year_is_one_record_per_calendar_month_ending_on_reference_date(client, yahoo, daily_history):
    reference = TODAY - timedelta(days=3 * 365)

    body = _get(client, "FIVE_YEAR", reference).json()
    result = _only(body)

    assert body["interval"] == "1mo"
    window = _window(daily_history, reference, 5 * 365 + 2)
    last_per_month = window.groupby(window.index.strftime("%Y-%m")).tail(1)
    assert _dates(result) == [ts.date().isoformat() for ts in last_per_month.index]
    assert 59 <= result["count"] <= 62
    assert _dates(result)[-1] == window.index[-1].date().isoformat()


def test_reference_date_today_matches_the_rolling_window_length(client, yahoo, daily_history):
    result = _only(_get(client, "WEEK", TODAY).json())

    expected = _window(daily_history, TODAY, 7)
    assert _dates(result) == [ts.date().isoformat() for ts in expected.index]
    assert _dates(result)[-1] == daily_history.index[-1].date().isoformat()


# --------------------------------------------------------------------------
# DAY: the hourly bars of that one exchange-local trading date
# --------------------------------------------------------------------------
def test_day_returns_the_hourly_bars_of_that_date(client, yahoo, hourly_history):
    reference = TODAY - timedelta(days=45)

    resp = _get(client, "DAY", reference)

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    result = _only(body)
    assert body["interval"] == "1h"
    assert result["count"] == 24
    assert {_day(d) for d in _dates(result)} == {reference.isoformat()}
    expected = _window(hourly_history, reference, 1)
    assert _dates(result) == [ts.isoformat() for ts in expected.index]
    assert [p["price"] for p in result["data"]] == [round(float(c), 4) for c in expected["Close"]]
    assert yahoo.calls == [{"symbol": "AAPL", "start": reference, "end": reference + timedelta(days=1), "interval": "60m"}]


def test_day_window_is_an_exchange_local_day_even_across_dst(client, yahoo):
    """The most recent DST change day has 23 or 25 local hours; the window
    must follow the local calendar day, not 24 absolute hours."""
    dst_day = None
    for back in range(2, 560):
        d = TODAY - timedelta(days=back)
        length = pd.Timestamp(d + timedelta(days=1)).tz_localize(EXCHANGE_TZ) - pd.Timestamp(d).tz_localize(EXCHANGE_TZ)
        if length != pd.Timedelta(hours=24):
            dst_day, local_hours = d, int(length.total_seconds() // 3600)
            break
    assert dst_day is not None and local_hours in (23, 25)

    result = _only(_get(client, "DAY", dst_day).json())

    assert result["count"] == local_hours
    assert {_day(d) for d in _dates(result)} == {dst_day.isoformat()}


def test_day_on_a_non_trading_date_is_404_not_last_24_bars(client, fake_yfinance):
    """Plain DAY falls back to the newest 24 bars on a long weekend; an
    explicit date the market was closed must not pretend otherwise."""
    frame = make_ohlc_frame(periods=24 * 100, freq="h")
    frame = frame[frame.index.weekday < 5]
    fake_yfinance.window_responses = lambda start, end, interval: slice_like_yahoo(frame, start, end)
    sunday = TODAY - timedelta(days=30)
    sunday -= timedelta(days=(sunday.weekday() - 6) % 7)
    assert sunday.weekday() == 6

    resp = _get(client, "DAY", sunday)

    assert resp.status_code == 404
    assert sunday.isoformat() in resp.json()["detail"]


def test_day_skips_nan_close_bars(client, fake_yfinance, hourly_history):
    reference = TODAY - timedelta(days=20)
    df = hourly_history.copy()
    df.loc[_window(df, reference, 1).index[:3], "Close"] = float("nan")
    fake_yfinance.window_responses = lambda start, end, interval: slice_like_yahoo(df, start, end)

    result = _only(_strict_json(_get(client, "DAY", reference).text))

    assert result["count"] == 21


def test_bars_outside_the_window_returned_by_upstream_are_dropped(client, fake_yfinance, daily_history):
    """If Yahoo ever returns more than asked for, the contract still holds."""
    reference = TODAY - timedelta(days=100)
    fake_yfinance.window_responses = lambda start, end, interval: daily_history  # ignores start/end

    result = _only(_get(client, "WEEK", reference).json())

    expected = _window(daily_history, reference, 7)
    assert _dates(result) == [ts.date().isoformat() for ts in expected.index]


# --------------------------------------------------------------------------
# No caching: every request is a live call
# --------------------------------------------------------------------------
def test_every_reference_date_request_is_a_live_call(client, yahoo):
    before = stock_router._limiter.remaining()
    reference = TODAY - timedelta(days=30)

    bodies = [_get(client, "WEEK", reference).json() for _ in range(3)]

    assert bodies[0] == bodies[1] == bodies[2]
    assert len(yahoo.calls) == 3
    assert before - stock_router._limiter.remaining() == 3


def test_reference_date_and_rolling_window_are_independent_calls(client, yahoo, daily_history):
    yahoo.responses = lambda period, interval: daily_history if interval == "1d" else pd.DataFrame()

    rolling = client.get("/stock/AAPL/historic?period=WEEK")
    anchored = _get(client, "WEEK", TODAY - timedelta(days=30))

    assert rolling.status_code == anchored.status_code == 200
    assert [c.get("period", "window") for c in yahoo.calls] == ["1mo", "window"]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
def test_unknown_symbol_is_404(client, fake_yfinance):
    resp = _get(client, "WEEK", TODAY - timedelta(days=30), ticker="NOPE")
    assert resp.status_code == 404
    assert "ending on" in resp.json()["detail"]


def test_local_budget_exhausted_is_429(client, yahoo, monkeypatch):
    monkeypatch.setattr(stock_router._limiter, "max_calls", 0)
    assert _get(client, "WEEK", TODAY - timedelta(days=30)).status_code == 429


def test_yahoo_rate_limit_maps_to_429(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFRateLimitError()
    assert _get(client, "WEEK", TODAY - timedelta(days=30)).status_code == 429


def test_yfinance_error_maps_to_502(client, fake_yfinance):
    fake_yfinance.raise_on_history = YFException("Yahoo is down")
    resp = _get(client, "MONTH", TODAY - timedelta(days=30))
    assert resp.status_code == 502
    assert "Yahoo is down" in resp.json()["detail"]


def test_unexpected_exception_maps_to_502_not_500(client, fake_yfinance):
    fake_yfinance.raise_on_history = KeyError("Close")
    assert _get(client, "DAY", TODAY - timedelta(days=30)).status_code == 502


def test_dotted_share_class_falls_back_to_dash(client, fake_yfinance, daily_history):
    class DashOnly(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if self.symbol == "BRK-B":
                return slice_like_yahoo(daily_history, start, end)
            empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
            empty.index.name = "Date"
            return empty

    stock_router.yf.Ticker = DashOnly
    resp = _get(client, "WEEK", TODAY - timedelta(days=30), ticker="BRK.B")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "BRK.B"
    assert resp.json()["symbol"] == "BRK-B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B"]


def test_dotted_share_class_falls_back_to_dash_when_yfinance_raises(client, fake_yfinance, daily_history):
    from yfinance.exceptions import YFPricesMissingError

    class RaisesForDotted(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if "." in self.symbol:
                raise YFPricesMissingError(self.symbol, "no data")
            return slice_like_yahoo(daily_history, start, end)

    stock_router.yf.Ticker = RaisesForDotted
    resp = _get(client, "WEEK", TODAY - timedelta(days=30), ticker="BRK.B")

    assert resp.status_code == 200, resp.text
    assert resp.json()["symbol"] == "BRK-B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B"]


def test_unknown_dotted_symbol_that_raises_for_both_forms_is_404(client, fake_yfinance):
    from yfinance.exceptions import YFTzMissingError

    class AlwaysMissing(fake_yfinance):
        def history(self, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol})
            raise YFTzMissingError(self.symbol)

    stock_router.yf.Ticker = AlwaysMissing
    resp = _get(client, "WEEK", TODAY - timedelta(days=30), ticker="NO.PE")

    assert resp.status_code == 404
    assert [c["symbol"] for c in fake_yfinance.calls] == ["NO.PE", "NO-PE"]
