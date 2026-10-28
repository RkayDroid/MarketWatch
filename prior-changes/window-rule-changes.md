# Before / after: one window rule for `/historic` and `/highlow`

Baseline = git commit `d473f3e`. Sample responses below were produced by
running the actual app against the test double with the clock frozen at
**Wednesday 2026‑10‑21, 14:00 New York**, with Wednesday 2026‑10‑14 as a
holiday (prices are synthetic; shapes, keys, ordering and messages are
exactly what the code emits).

## 1. Scope

| asked | delivered |
|---|---|
| Both endpoints must tell the same story: historic records for `DAY` … `FIVE_YEAR` ending on `referenceDate`, and `/highlow` = the high/low of that same period | One window rule, shared by both endpoints and by the rolling case: **D′ = last trading day on or before `referenceDate` (default today)**; `DAY` = the session on D′, `WEEK` = the 5 sessions ending on D′, `MONTH`/`HALF_YEAR`/`YEAR`/`FIVE_YEAR` = 30/182/365 days/5 years of calendar time ending on D′ |
| (from the previous round) `referenceDate` on `/highlow` | kept — same parameter, list rules and validation on both endpoints |

## 2. At a glance

| | before | after |
|---|---|---|
| Meanings of `DAY` | three (24 absolute hours on `/historic`; latest session on `/highlow`; a calendar date with `referenceDate`) | **one**: the session on D′ |
| Meanings of `WEEK` | two (Yahoo `5d` rolling on `/highlow`; 7 calendar days elsewhere) | **one**: 5 sessions ending on D′ |
| `referenceDate` on a weekend / holiday | empty (`{count: 0, data: []}` / `null`), 404 if alone | resolves to the session before; reported in `window` |
| Rolling vs anchored | different code paths (Yahoo ranges + fallbacks vs `start/end`) | the same path: rolling = `referenceDate` omitted = today |
| Response header | `/historic`: `ticker, symbol, period, interval`; `/highlow`: `symbol, period` | both: `ticker, symbol, period, interval, referenceDate, window` |
| Upstream calls per request | 1–2 (fallbacks) | **1 per window**, always |
| Router | 826 lines, 5 fetchers, 2 slicers, 2 loaders, 2 request flows | 760 lines (a third of them docstrings), 1 fetcher, `_resolve_window` + `_load_window` + `_serve`, each endpoint = a bar-size table + a `view` |
| Offline tests | 206 | **243** (178 of them run through both endpoints) |
| App version | 1.1.0 | **2.0.0** (response shapes changed) |

## 3. The rule, on real requests

### 3.1 Rolling `DAY` during the session — the same session on both endpoints

<table><tr><th><code>GET /stock/AAPL/historic?period=DAY</code></th><th><code>GET /stock/AAPL/highlow?period=DAY</code></th></tr><tr><td>

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "DAY",
  "interval": "1h",
  "referenceDate": "2026-10-21",
  "window": {"start": "2026-10-21", "end": "2026-10-21"},
  "count": 5,
  "data": [
    {"datetime": "2026-10-21T09:30:00-04:00", "price": 221.55},
    {"datetime": "2026-10-21T10:30:00-04:00", "price": 221.55},
    "...",
    {"datetime": "2026-10-21T13:30:00-04:00", "price": 221.55}
  ]
}
```
</td><td>

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "DAY",
  "interval": "5m",
  "referenceDate": "2026-10-21",
  "window": {"start": "2026-10-21", "end": "2026-10-21"},
  "high": 221.56,
  "highOccured": "2026-10-21T09:30:00-04:00",
  "low": 221.54,
  "lowOccured": "2026-10-21T09:30:00-04:00"
}
```
</td></tr></table>

Before: `/historic DAY` was "the last 24 hours" (at 14:00 Wednesday: half
of Tuesday plus Wednesday so far), `/highlow DAY` was "the latest
session". Now both are today's session so far, and say so.

### 3.2 A Sunday `referenceDate` resolves to Friday; a holiday stretches `WEEK`

`GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-18` → **200**
```json
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "WEEK", "interval": "1d",
  "results": {
    "2026-10-18": {
      "window": {"start": "2026-10-09", "end": "2026-10-16"},
      "count": 5,
      "data": [
        {"datetime": "2026-10-09", "price": 221.21},
        {"datetime": "2026-10-12", "price": 221.3},
        "...",
        {"datetime": "2026-10-16", "price": 221.41}
      ]
    }
  }
}
```

`GET /stock/AAPL/highlow?period=WEEK&referenceDate=2026-10-18` → **200**
```json
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "WEEK", "interval": "1h",
  "results": {
    "2026-10-18": {
      "window": {"start": "2026-10-09", "end": "2026-10-16"},
      "high": 221.42,
      "highOccured": "2026-10-16T09:30:00-04:00",
      "low": 221.19,
      "lowOccured": "2026-10-09T09:30:00-04:00"
    }
  }
}
```

The key is the date as asked (`2026-10-18`); `window.end` is D′ (Friday
the 16th); the 5 sessions are Fri 9, Mon 12, Tue 13, Thu 15, Fri 16
because Wednesday the 14th was closed. Both endpoints report the
identical window; the high (a bar High) is ≥ every price in the series.
Before: `/historic` answered `{count: 0, data: []}`-style for the
Sunday's 7 calendar days only if nothing traded — here it would have
returned 4 sessions (10‑12 … 10‑16) and `/highlow` the same 4, i.e. a
"week" of four days.

### 3.3 Several dates; before the listing is `null`

`GET /stock/AAPL/historic?period=MONTH&referenceDate=2026-10-18,2015-03-31,1970-01-01` → **200**
```json
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "MONTH", "interval": "1d",
  "results": {
    "2026-10-18": {"window": {"start": "2026-09-17", "end": "2026-10-16"}, "count": 21, "data": ["..."]},
    "2015-03-31": {"window": {"start": "2015-03-02", "end": "2015-03-31"}, "count": 22, "data": ["..."]},
    "1970-01-01": null
  }
}
```

`window.start` of a calendar period is `end − 29` days whether or not it
traded (2026‑09‑17 is a Thursday here, 2015‑03‑02 a Monday). The request
is 404 only if every entry is `null`.

### 3.4 Validation — unchanged messages, retention on the window's start

| request | response |
|---|---|
| `highlow?period=DAY&referenceDate=2026-07-01` | `422` `referenceDate 2026-07-01 is too old for period DAY: Yahoo Finance keeps 5-minute bars for ~60 days only. Use a more recent referenceDate or a longer period (WEEK, MONTH...).` |
| `highlow?period=WEEK&referenceDate=2024-09-01` | `422` `… too old for period WEEK: Yahoo Finance keeps hourly bars for ~730 days only. Use a more recent referenceDate or a daily period (MONTH, HALF_YEAR...).` |
| `historic?period=DAY&referenceDate=2019-01-02` | `422` `… too old for period DAY: Yahoo Finance keeps hourly bars for ~730 days only. …` |
| list problems (6 dates, duplicates, empty items, malformed, impossible, future) | as before, on both endpoints |

## 4. What changes for existing clients

| | before | after |
|---|---|---|
| rolling `/historic DAY` | last 24 absolute hours of hourly bars; "last 24 bars" on a long weekend | today's session so far, or the last session (one session, ≤ 7 hourly bars + the running one) |
| rolling `/historic WEEK` | 7 calendar days | 5 sessions (same thing except across a holiday) |
| rolling `/highlow DAY` | latest session (`range=1d`, `5d` fallback) | the same, via the rule |
| rolling `/highlow WEEK` | Yahoo `5d` = 5 sessions | the same, via the rule |
| anchored `WEEK` (both) | 7 calendar days ending on the date | 5 sessions ending on D′ |
| anchored `DAY` on a closed day | `{count: 0, data: []}` / `null`, 404 if alone | the previous session |
| anchored calendar periods | N days ending on the date | N days ending on D′ (= the date when it traded) |
| `/historic` empty entry | `{"count": 0, "data": []}` | `null` (only before the listing now) |
| `/historic` flat response | 6 keys | + `referenceDate`, `window` |
| `/highlow` flat response | 6 keys, `symbol` = the requested symbol | + `ticker`, `interval`, `referenceDate`, `window`; `symbol` = Yahoo's form (`BRK-B`), `ticker` = as requested |
| `/highlow` keyed entry | `{high, highOccured, low, lowOccured}` | + `window` |
| 404 / 422 / 429 / 502 mapping, dotted-symbol retry, limiter | unchanged | unchanged |

## 5. Internals

| | before | after |
|---|---|---|
| Window definition | `_LOOKBACK_DAYS` (calendar) for anchored + `/historic` daily; Yahoo ranges (`1d`, `5d`, `2d`, `1mo`…) for the rest; `_slice_lookback` in UTC; `_last_24h`; session fallbacks | `_SESSIONS` (`DAY` 1, `WEEK` 5) + `_CALENDAR_DAYS`; `_resolve_window(df, period, D)` → (bars, `Window(start, end)`) |
| Fetch | `_fetch_history(period, interval)` + `_fetch_window(start, end)` + five frame loaders | `_load_window(ticker, period, date|None, interval, retention)`: one `_fetch_window` over `[D − span + 1 − 14, D + 1)`, clamped to the retention horizon; rolling asks up to UTC tomorrow |
| Request flow | two per endpoint (rolling / keyed), duplicated | `_serve(ticker, period, referenceDate, Bars, view)` shared; `/historic` = `_HISTORIC_BARS` + downsample/`_to_points`; `/highlow` = `_HIGHLOW_BARS` + `_compute_extremes` |
| Validation | `_validate_reference_date(d, period, retention_table)` | `_validate_reference_date(d, period, Retention|None)`, on the nominal window start |
| `_last_per_bin` | tz-aware timestamp age (off by one across DST) | exchange-local calendar-day age |
| Clock seams | `_today_utc()` | `_today_utc()`, `_today_local(tz)` (monkeypatched by the tests' frozen clock) |
| Removed | `_DAILY_YAHOO_RANGE`, `PRIMARY_HOURLY_PERIOD`, `FALLBACK_PERIOD`, `_fetch_history`, `_fetch_hourly_frame`, `_fetch_last_session_frame`, `_fetch_week_intraday_frame`, `_fetch_daily_frame`, `_slice_lookback`, `_localize_date`, `_window_bounds`, `_slice_window`, `_last_24h`, `_historic_points*`, `_load_highlow_frame*`, `_highlow_fields` | |
| New constants | | `RESOLUTION_LOOKBACK_DAYS = 14` |

## 6. Tests

| file | before | after |
|---|---|---|
| `conftest.py` | `make_ohlc_frame` (one frame at a time, unrelated across bar sizes) | `FakeMarket`: one synthetic exchange (sessions, holiday, 9-day closure, freezable clock); daily/hourly/5-minute frames are true aggregations of one price path |
| `test_historic_reference_date.py` (67), `test_highlow_reference_date.py` (71) | per-endpoint `referenceDate` suites | **replaced** by `test_window_rule.py` (178): every test parametrised over both endpoints, plus the cross-endpoint invariants (same `window`, `high ≥ max(price)`, `low ≤ min(price)`, occurrences inside the window) and frozen-clock rolling tests |
| `test_historic_endpoint.py` | 25 | 23 — series view only (granularity, downsampling across the closure and DST, serialisation) |
| `test_highlow_endpoint.py` | 22 | 26 — extremes view only (columns, precision, bar-level window edges, NaN) |
| `test_dotted_tickers.py` | 8 | 4 (the endpoint cases moved to `test_window_rule.py`, run for both) |
| `test_live_yfinance.py` | 33 | 30 — rewritten around the rule; **not run** (no network in the sandbox) |
| **total offline** | 206 | **243 passed, 0 failed** (~15 s) |

## 7. To confirm / next

* The live tests encode what to check once against real Yahoo: the two
  endpoints report the same `window` for every period; a Sunday resolves
  to Friday with a 5-minute `highOccured`; `DAY` during the session has
  the running bar; 12 windows cost 12 calls.
* If a deterministic session calendar is wanted (no 14-day look-back, no
  edge cases at the retention horizon), add `exchange_calendars` and
  compute D′ and the window before the call — `_resolve_window` is the
  only function that would change.
* A cache keyed by `(symbol, period, D′, interval)` now serves both
  endpoints' rolling and anchored requests from the same entries; past
  windows (D′ < today) never change.
