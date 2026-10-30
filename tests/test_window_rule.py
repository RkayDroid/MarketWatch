"""The window rule of GET /stock/{ticker}/historic.

    D' = the last trading day on or before `referenceDate` (default: today)
    DAY = the session on D', WEEK = the 5 sessions ending on D',
    MONTH / HALF_YEAR / YEAR / FIVE_YEAR = 30 / 182 / 365 / 1827 calendar
    days ending on D'.

The synthetic market (`conftest.FakeMarket`) has weekday sessions, a
Wednesday holiday (`HOLIDAY`) a few weeks back and a 9-day closure
(`CLOSURE_START..END`) ~300 days back. What the records contain (OHLC,
downsampling, serialisation) is covered in test_historic_endpoint.py.
"""

import json
from datetime import date, timedelta

import pandas as pd
import pytest
from yfinance.exceptions import YFException, YFRateLimitError

import stock_router
from tests.conftest import (
    CLOSURE_END,
    CLOSURE_START,
    EXCHANGE_TZ,
    HOLIDAY,
    TODAY,
    FakeMarket,
    make_market,
)

PERIODS = ["DAY", "WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"]
CALENDAR_SPAN = {"MONTH": 30, "HALF_YEAR": 182, "YEAR": 365, "FIVE_YEAR": 5 * 365 + 2}
NOMINAL_SPAN = {"DAY": 1, "WEEK": 7, **CALENDAR_SPAN}
INTERVAL = {"DAY": "60m", "WEEK": "1d", "MONTH": "1d", "HALF_YEAR": "1d", "YEAR": "1d", "FIVE_YEAR": "1d"}
HEADER_KEYS = {"ticker", "symbol", "period", "interval"}
LOOKBACK = stock_router.RESOLUTION_LOOKBACK_DAYS


def _strict_json(text: str):
    def _reject(token):
        raise ValueError(f"invalid JSON token: {token}")

    return json.loads(text, parse_constant=_reject)


def get(client, period, *reference_dates, ticker="AAPL"):
    url = f"/stock/{ticker}/historic?period={period}"
    if reference_dates:
        url += "&referenceDate=" + ",".join(d if isinstance(d, str) else d.isoformat() for d in reference_dates)
    return client.get(url)


def only(body: dict) -> dict:
    """The single per-date result of a one-date request."""
    (result,) = body["results"].values()
    return result


def window(result: dict):
    return date.fromisoformat(result["window"]["start"]), date.fromisoformat(result["window"]["end"])


def served_dates(result: dict):
    """Exchange-local dates of the records."""
    return [date.fromisoformat(p["datetime"][:10]) for p in result["data"]]


def weekday_back(days_back: int, weekday: int) -> date:
    d = TODAY - timedelta(days=days_back)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


FRIDAY = weekday_back(40, 4)
SATURDAY = FRIDAY + timedelta(days=1)
SUNDAY = FRIDAY + timedelta(days=2)
MONDAY = FRIDAY + timedelta(days=3)


# --------------------------------------------------------------------------
# D' resolution
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reference,expected_end",
    [
        (FRIDAY, FRIDAY),  # a trading day is its own D'
        (SATURDAY, FRIDAY),
        (SUNDAY, FRIDAY),
        (HOLIDAY, HOLIDAY - timedelta(days=1)),  # Wednesday holiday -> Tuesday
        (CLOSURE_START + timedelta(days=4), CLOSURE_START - timedelta(days=1)),  # inside the 9-day closure -> the Friday before it
        (CLOSURE_END, CLOSURE_START - timedelta(days=1)),
    ],
)
def test_window_ends_on_the_last_trading_day_on_or_before_the_reference_date(client, market, reference, expected_end):
    assert expected_end in market.sessions
    resp = get(client, "WEEK", reference)

    assert resp.status_code == 200, resp.text
    result = only(resp.json())
    assert window(result)[1] == expected_end
    assert served_dates(result)[-1] == expected_end


def test_reference_date_is_echoed_as_the_results_key_not_resolved(client, market):
    body = get(client, "DAY", SUNDAY).json()

    assert list(body["results"]) == [SUNDAY.isoformat()]
    assert window(only(body)) == (FRIDAY, FRIDAY)


# --------------------------------------------------------------------------
# Window definitions
# --------------------------------------------------------------------------
@pytest.mark.parametrize("reference", [FRIDAY, SUNDAY, HOLIDAY])
def test_day_is_the_single_session_on_d_prime(client, market, reference):
    result = only(get(client, "DAY", reference).json())

    start, end = window(result)
    assert start == end == market.last_session_on_or_before(reference)
    assert set(served_dates(result)) == {end}


@pytest.mark.parametrize(
    "reference,expected_sessions",
    [
        (FRIDAY, [FRIDAY - timedelta(days=n) for n in (4, 3, 2, 1, 0)]),  # Mon..Fri
        (SUNDAY, [FRIDAY - timedelta(days=n) for n in (4, 3, 2, 1, 0)]),  # same week, resolved from the weekend
        (MONDAY, [FRIDAY - timedelta(days=n) for n in (3, 2, 1, 0)] + [MONDAY]),  # Tue..Fri + Mon
        # the Friday after the Wednesday holiday: Fri, Mon, Tue, Thu, Fri -> 8 calendar days
        (HOLIDAY + timedelta(days=2), [HOLIDAY + timedelta(days=n) for n in (-5, -2, -1, 1, 2)]),
    ],
)
def test_week_is_the_five_sessions_ending_on_d_prime(client, market, reference, expected_sessions):
    assert all(s in market.sessions for s in expected_sessions)
    result = only(get(client, "WEEK", reference).json())

    assert window(result) == (expected_sessions[0], expected_sessions[-1])
    assert served_dates(result) == expected_sessions  # one record per session


@pytest.mark.parametrize("period,days", CALENDAR_SPAN.items())
@pytest.mark.parametrize("reference", [FRIDAY, SUNDAY, HOLIDAY])
def test_calendar_periods_are_n_days_ending_on_d_prime(client, market, period, days, reference):
    result = only(get(client, period, reference).json())

    end = market.last_session_on_or_before(reference)
    start = end - timedelta(days=days - 1)
    assert window(result) == (start, end)  # start is a calendar date, whether or not it traded
    dates = served_dates(result)
    assert all(start <= d <= end for d in dates)
    assert dates[-1] == end


def test_calendar_window_start_is_a_calendar_date_even_on_a_weekend(client, market):
    # MONDAY - 29 days is a Sunday
    assert (MONDAY - timedelta(days=29)).weekday() == 6
    result = only(get(client, "MONTH", MONDAY).json())
    assert window(result)[0] == MONDAY - timedelta(days=29)


@pytest.mark.parametrize("period", PERIODS)
@pytest.mark.parametrize("reference", [FRIDAY, SUNDAY, HOLIDAY, CLOSURE_END])
def test_the_windows_extremes_are_the_bars_extremes(client, market, period, reference):
    """max(high) / min(low) over the records are the true extremes of the
    window at the period's bar size, whatever the downsampling."""
    if period == "DAY" and reference == CLOSURE_END:
        pytest.skip("hourly bars do not reach 300 days back in the fixture")
    result = only(get(client, period, reference).json())

    start, end = window(result)
    bars = market.bars(INTERVAL[period], start, end)
    assert max(p["high"] for p in result["data"]) == round(float(bars["High"].max()), 4)
    assert min(p["low"] for p in result["data"]) == round(float(bars["Low"].min()), 4)
    assert result["data"][-1]["close"] == round(float(bars["Close"].iloc[-1]), 4)


# --------------------------------------------------------------------------
# Rolling window = the same rule with referenceDate = today
# --------------------------------------------------------------------------
def _frozen_market(fake_yfinance, clock, when: str, tz: str = EXCHANGE_TZ) -> FakeMarket:
    now = clock(pd.Timestamp(when, tz=tz))
    m = make_market(now=now, tz=tz)
    fake_yfinance.window_responses = m.respond
    return m


# a Wednesday at least a week back, so no holiday or closure is near
WEDNESDAY = weekday_back(10, 2)


def test_rolling_window_during_the_session_ends_on_todays_partial_session(client, fake_yfinance, clock):
    market = _frozen_market(fake_yfinance, clock, f"{WEDNESDAY} 11:07")

    body = get(client, "DAY").json()

    assert body["referenceDate"] == WEDNESDAY.isoformat()
    assert window(body) == (WEDNESDAY, WEDNESDAY)
    assert [p["datetime"][11:16] for p in body["data"]] == ["09:30", "10:30"]  # the running hourly bar included
    assert body["data"][-1]["close"] == round(float(market.hourly["Close"].iloc[-1]), 4)


def test_rolling_window_before_the_open_ends_on_the_previous_session(client, fake_yfinance, clock):
    _frozen_market(fake_yfinance, clock, f"{WEDNESDAY} 08:00")

    body = get(client, "WEEK").json()

    assert body["referenceDate"] == WEDNESDAY.isoformat()  # today...
    assert window(body)[1] == WEDNESDAY - timedelta(days=1)  # ...but the last session is yesterday


def test_rolling_window_on_a_sunday_ends_on_friday(client, fake_yfinance, clock):
    sunday = WEDNESDAY + timedelta(days=4)
    _frozen_market(fake_yfinance, clock, f"{sunday} 12:00")

    body = get(client, "DAY").json()

    assert body["referenceDate"] == sunday.isoformat()
    assert window(body) == (sunday - timedelta(days=2), sunday - timedelta(days=2))


@pytest.mark.parametrize("period", PERIODS)
def test_rolling_window_equals_reference_date_today(client, fake_yfinance, clock, period):
    _frozen_market(fake_yfinance, clock, f"{WEDNESDAY} 14:00")

    rolling = get(client, period).json()
    anchored = only(get(client, period, WEDNESDAY).json())

    assert rolling["referenceDate"] == WEDNESDAY.isoformat()
    assert {k: v for k, v in rolling.items() if k not in HEADER_KEYS | {"referenceDate"}} == anchored


def test_rolling_window_uses_the_exchanges_local_today(client, fake_yfinance, clock):
    """At 23:30 UTC on Tuesday, Tokyo is on Wednesday 08:30 - before its
    open - so today is Wednesday and the last session is Tuesday."""
    tuesday_utc = pd.Timestamp(f"{WEDNESDAY - timedelta(days=1)} 23:30", tz="UTC")
    _frozen_market(fake_yfinance, clock, tuesday_utc.tz_convert("Asia/Tokyo").isoformat(), tz="Asia/Tokyo")

    body = get(client, "DAY", ticker="7203.T").json()

    assert body["referenceDate"] == WEDNESDAY.isoformat()
    assert window(body) == (WEDNESDAY - timedelta(days=1), WEDNESDAY - timedelta(days=1))


def test_rolling_response_is_flat_with_reference_date_and_window(client, market):
    resp = get(client, "WEEK")

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert HEADER_KEYS | {"referenceDate", "window"} < set(body)
    assert "results" not in body
    assert set(body["window"]) == {"start", "end"}


# --------------------------------------------------------------------------
# The upstream call: one per window, covering the span plus the look-back
# --------------------------------------------------------------------------
@pytest.mark.parametrize("period", PERIODS)
def test_one_call_covering_the_nominal_span_plus_the_resolution_lookback(client, market, period):
    reference = weekday_back(20, 2)
    before = stock_router._limiter.remaining()

    assert get(client, period, reference).status_code == 200

    assert stock_router.yf.Ticker.calls == [
        {
            "symbol": "AAPL",
            "start": reference - timedelta(days=NOMINAL_SPAN[period] - 1 + LOOKBACK),
            "end": reference + timedelta(days=1),  # exclusive
            "interval": INTERVAL[period],
        }
    ]
    assert before - stock_router._limiter.remaining() == 1


def test_rolling_call_asks_up_to_utc_tomorrow(client, market):
    get(client, "MONTH")

    (call,) = stock_router.yf.Ticker.calls
    assert call["end"] == TODAY + timedelta(days=2)  # tomorrow inclusive, for exchanges ahead of UTC
    assert call["start"] == TODAY + timedelta(days=1) - timedelta(days=29 + LOOKBACK)


def test_lookback_is_clamped_to_the_hourly_retention_horizon(client, market):
    reference = TODAY - timedelta(days=stock_router.HOURLY_RETENTION_DAYS - 3)

    get(client, "DAY", reference)

    (call,) = stock_router.yf.Ticker.calls
    assert call["start"] == TODAY - timedelta(days=stock_router.HOURLY_RETENTION_DAYS)  # not reference - 14


def test_bars_outside_the_window_returned_by_upstream_are_dropped(client, fake_yfinance, market):
    """If Yahoo ever returns more than asked for, the rule still holds."""
    fake_yfinance.window_responses = lambda start, end, interval: market.frame(interval)  # ignores start/end

    result = only(get(client, "MONTH", FRIDAY).json())

    assert window(result) == (FRIDAY - timedelta(days=29), FRIDAY)
    assert all(FRIDAY - timedelta(days=29) <= d <= FRIDAY for d in served_dates(result))


# --------------------------------------------------------------------------
# Multiple reference dates
# --------------------------------------------------------------------------
def test_multiple_dates_are_keyed_in_the_order_given_and_equal_the_single_date_results(client, market):
    dates = [SUNDAY, HOLIDAY, FRIDAY - timedelta(days=200)]  # deliberately unsorted

    resp = get(client, "MONTH", *dates)

    assert resp.status_code == 200, resp.text
    body = _strict_json(resp.text)
    assert set(body) == HEADER_KEYS | {"results"}
    assert list(body["results"]) == [d.isoformat() for d in dates]
    for d in dates:
        assert body["results"][d.isoformat()] == only(get(client, "MONTH", d).json())


def test_each_date_is_one_upstream_call(client, market):
    dates = [FRIDAY - timedelta(days=n) for n in (0, 7, 14, 21, 28)]
    before = stock_router._limiter.remaining()

    assert get(client, "WEEK", *dates).status_code == 200  # 5 dates accepted

    assert [c["end"] for c in stock_router.yf.Ticker.calls] == [d + timedelta(days=1) for d in dates]
    assert before - stock_router._limiter.remaining() == 5


def test_date_before_the_listing_is_null_beside_a_served_date(client, market):
    before_listing = market.sessions[0] - timedelta(days=30)

    resp = get(client, "MONTH", FRIDAY, before_listing)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][before_listing.isoformat()] is None
    assert body["results"][FRIDAY.isoformat()]["window"]["end"] == FRIDAY.isoformat()


def test_404_when_every_date_is_empty(client, fake_yfinance):
    a, b = FRIDAY, SUNDAY

    resp = get(client, "WEEK", a, b, ticker="NOPE")

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert a.isoformat() in detail and b.isoformat() in detail and "WEEK" in detail


def test_budget_exhausted_midway_fails_the_whole_request(client, market, monkeypatch):
    monkeypatch.setattr(stock_router._limiter, "max_calls", 1)

    resp = get(client, "WEEK", FRIDAY, SUNDAY)

    assert resp.status_code == 429
    assert len(stock_router.yf.Ticker.calls) == 1  # the second date never got to Yahoo


# --------------------------------------------------------------------------
# Validation: rejected before any upstream call
# --------------------------------------------------------------------------
def _assert_422_without_upstream(client, query: str, *fragments: str):
    before = stock_router._limiter.remaining()
    resp = client.get(f"/stock/AAPL/historic?{query}")
    assert resp.status_code == 422, resp.text
    detail = json.dumps(resp.json()["detail"])
    for fragment in fragments:
        assert fragment in detail, f"{fragment!r} not in {detail}"
    assert stock_router.yf.Ticker.calls == []
    assert stock_router._limiter.remaining() == before


def test_list_problems_are_422_naming_the_item(client, market):
    d, other = FRIDAY.isoformat(), SUNDAY.isoformat()
    six = ",".join((TODAY - timedelta(days=n)).isoformat() for n in range(10, 70, 10))
    _assert_422_without_upstream(client, f"period=WEEK&referenceDate={six}", "at most 5", "got 6")
    _assert_422_without_upstream(client, f"period=WEEK&referenceDate={d},{other},{d}", "more than once", d)
    _assert_422_without_upstream(client, f"period=WEEK&referenceDate={d}, {d}", "more than once")
    for raw in (f"{d},,{other}", f"{d},", f",{d}", "", " "):
        _assert_422_without_upstream(client, f"period=WEEK&referenceDate={raw}", "empty item")
    _assert_422_without_upstream(client, f"period=WEEK&referenceDate={d},23-10-2026", "23-10-2026")


@pytest.mark.parametrize(
    "query",
    [
        "period=DAY&referenceDate=" + (TODAY + timedelta(days=2)).isoformat(),  # future
        "period=YEAR&referenceDate=" + (TODAY + timedelta(days=365)).isoformat(),
        "period=MONTH&referenceDate=23-10-2026",  # wrong format
        "period=MONTH&referenceDate=2026/10/23",
        "period=MONTH&referenceDate=20261023",
        "period=MONTH&referenceDate=2026-13-01",  # impossible month
        "period=MONTH&referenceDate=2026-02-30",  # impossible day
        "period=MONTH&referenceDate=yesterday",
        "period=DECADE&referenceDate=2024-01-01",  # invalid period still 422
        "period=DECADE",
    ],
)
def test_invalid_input_is_422_without_upstream_call(client, market, query):
    _assert_422_without_upstream(client, query)


def test_tomorrow_utc_is_accepted_for_exchanges_ahead_of_utc(client, market):
    resp = get(client, "MONTH", TODAY + timedelta(days=1))
    assert resp.status_code == 200, resp.text


def test_day_windows_starting_before_the_hourly_retention_horizon_are_422(client, fake_yfinance, clock):
    days = stock_router.HOURLY_RETENTION_DAYS
    monday = weekday_back(7, 0)
    _frozen_market(fake_yfinance, clock, f"{monday} 12:00")
    oldest_ok = monday - timedelta(days=days)

    resp = get(client, "DAY", oldest_ok)
    assert resp.status_code != 422, resp.text  # accepted; at the very edge a weekend can't resolve -> 404
    if oldest_ok.weekday() < 5:
        assert resp.status_code == 200, resp.text
        assert window(only(resp.json()))[1] == oldest_ok

    resp = get(client, "DAY", oldest_ok - timedelta(days=1))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "DAY" in detail and "hourly" in detail and str(days) in detail and "WEEK" in detail  # suggests the next period up


@pytest.mark.parametrize("period", ["WEEK", "MONTH", "HALF_YEAR", "YEAR", "FIVE_YEAR"])
def test_daily_periods_reach_back_decades(client, market, period):
    resp = get(client, period, "2015-03-31")
    assert resp.status_code == 200, resp.text
    assert window(only(resp.json()))[1] == date(2015, 3, 31)


# --------------------------------------------------------------------------
# No caching, error mapping, dotted symbols
# --------------------------------------------------------------------------
def test_every_request_is_a_live_call(client, market):
    before = stock_router._limiter.remaining()

    bodies = [get(client, "WEEK", FRIDAY).json() for _ in range(3)]

    assert bodies[0] == bodies[1] == bodies[2]
    assert len(stock_router.yf.Ticker.calls) == 3
    assert before - stock_router._limiter.remaining() == 3
    assert "X-Cache" not in get(client, "WEEK").headers


def test_unknown_symbol_is_404(client, fake_yfinance):
    assert get(client, "WEEK", ticker="NOPE").status_code == 404
    resp = get(client, "WEEK", FRIDAY, ticker="NOPE")
    assert resp.status_code == 404
    assert "ending on" in resp.json()["detail"]


def test_error_mapping(client, fake_yfinance, market, monkeypatch):
    monkeypatch.setattr(stock_router._limiter, "max_calls", 0)
    assert get(client, "WEEK", FRIDAY).status_code == 429
    assert get(client, "WEEK").status_code == 429
    monkeypatch.setattr(stock_router._limiter, "max_calls", 100)

    fake_yfinance.raise_on_history = YFRateLimitError()
    assert get(client, "WEEK", FRIDAY).status_code == 429

    fake_yfinance.raise_on_history = YFException("Yahoo is down")
    resp = get(client, "MONTH", FRIDAY)
    assert resp.status_code == 502 and "Yahoo is down" in resp.json()["detail"]

    fake_yfinance.raise_on_history = KeyError("High")
    assert get(client, "DAY", FRIDAY).status_code == 502  # never a raw 500


def test_dotted_share_class_falls_back_to_dash_and_reports_both(client, fake_yfinance, market):
    class DashOnly(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if self.symbol == "BRK-B":
                return market.respond(start, end, interval)
            return pd.DataFrame()

    stock_router.yf.Ticker = DashOnly
    resp = get(client, "MONTH", FRIDAY, ticker="BRK.B")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "BRK.B" and resp.json()["symbol"] == "BRK-B"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["BRK.B", "BRK-B"]

    rolling = get(client, "MONTH", ticker="BRK.B").json()
    assert rolling["ticker"] == "BRK.B" and rolling["symbol"] == "BRK-B"


def test_dotted_share_class_falls_back_to_dash_when_yfinance_raises(client, fake_yfinance, market):
    from yfinance.exceptions import YFPricesMissingError

    class RaisesForDotted(fake_yfinance):
        def history(self, period=None, interval="1d", start=None, end=None, **kwargs):
            fake_yfinance.calls.append({"symbol": self.symbol, "start": start, "end": end, "interval": interval})
            if "." in self.symbol:
                raise YFPricesMissingError(self.symbol, "no data")
            return market.respond(start, end, interval)

    stock_router.yf.Ticker = RaisesForDotted
    resp = get(client, "WEEK", FRIDAY, ticker="BRK.B")

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
    resp = get(client, "WEEK", FRIDAY, ticker="NO.PE")

    assert resp.status_code == 404
    assert [c["symbol"] for c in fake_yfinance.calls] == ["NO.PE", "NO-PE"]


def test_dotted_exchange_suffix_is_passed_through(client, fake_yfinance, market):
    resp = get(client, "WEEK", FRIDAY, ticker="sap.de")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "SAP.DE" and resp.json()["symbol"] == "SAP.DE"
    assert [c["symbol"] for c in fake_yfinance.calls] == ["SAP.DE"]


def test_highlow_endpoint_is_gone(client, market):
    assert client.get("/stock/AAPL/highlow").status_code == 404
    assert client.get("/stock/AAPL/highlow?period=WEEK").status_code == 404
    assert stock_router.yf.Ticker.calls == []
