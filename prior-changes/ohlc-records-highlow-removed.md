# Before / after: OHLC records on `/historic`, `/highlow` removed

Follows `window-rule-changes.md` (same baseline commit `d473f3e`; "before"
here is the state after that note). Samples were produced by running the
actual app against the test double with the clock frozen at **Wednesday
2026‑10‑21, 14:00 New York**, Wednesday 2026‑10‑14 a holiday (prices are
synthetic; shapes, keys, ordering and messages are exactly what the code
emits).

## 1. Scope

| asked | delivered |
|---|---|
| `/historic` records include high / low / open alongside close | every record is `{datetime, open, high, low, close}`; fortnight/month records are OHLC aggregates of their bin |
| remove `/highlow` and all the code around it | endpoint, its bar-size table, extremes computation, 5-minute retention, the `view` indirection, the 5-minute test frames and its test files are gone; `/stock/{ticker}/highlow` is 404 |

## 2. At a glance

| | before | after |
|---|---|---|
| Stock routes | `/stock/{t}`, `/stock/{t}/historic`, `/stock/{t}/highlow` | `/stock/{t}`, `/stock/{t}/historic` |
| `/historic` record | `{"datetime", "price"}` (`price` = Close) | `{"datetime", "open", "high", "low", "close"}` |
| Fortnight / month record | the Close of the bin's last session | first open, **highest high, lowest low**, last close of the bin (same `datetime`: the bin's last session) |
| "What was the high over the period, and when?" | `/highlow` (5‑min / hourly / daily precision) | `max(high)` over `/historic`'s `data`, in the record it sits in (hourly / daily precision) |
| Router | 760 lines | **685 lines** |
| Offline tests | 243 | **145** (nothing lost: the highlow suite's bar-edge tests moved into the historic suite; the both-endpoint parametrisation is gone) |
| App version | 2.0.0 | **3.0.0** |

## 3. The records

### 3.1 `DAY` — the session's hourly OHLC bars

`GET /stock/AAPL/historic?period=DAY` → **200**
```json
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "DAY", "interval": "1h",
  "referenceDate": "2026-10-21",
  "window": {"start": "2026-10-21", "end": "2026-10-21"},
  "count": 5,
  "data": [
    {"datetime": "2026-10-21T09:30:00-04:00", "open": 221.54, "high": 221.56, "low": 221.54, "close": 221.55},
    {"datetime": "2026-10-21T10:30:00-04:00", "open": 221.55, "high": 221.56, "low": 221.54, "close": 221.55},
    "...",
    {"datetime": "2026-10-21T13:30:00-04:00", "open": 221.55, "high": 221.56, "low": 221.54, "close": 221.55}
  ]
}
```

### 3.2 `WEEK` ending on a Sunday, across a holiday — one daily OHLC bar per session

`GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-18` → **200**
```json
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "WEEK", "interval": "1d",
  "results": {
    "2026-10-18": {
      "window": {"start": "2026-10-09", "end": "2026-10-16"},
      "count": 5,
      "data": [
        {"datetime": "2026-10-09", "open": 221.2,  "high": 221.22, "low": 221.19, "close": 221.21},
        {"datetime": "2026-10-12", "open": 221.28, "high": 221.31, "low": 221.28, "close": 221.3},
        {"datetime": "2026-10-13", "open": 221.31, "high": 221.34, "low": 221.31, "close": 221.33},
        {"datetime": "2026-10-15", "open": 221.37, "high": 221.39, "low": 221.37, "close": 221.38},
        {"datetime": "2026-10-16", "open": 221.4,  "high": 221.42, "low": 221.4,  "close": 221.41}
      ]
    }
  }
}
```

The week's high is `max(high)` = 221.42 on 2026‑10‑16, its low
`min(low)` = 221.19 on 2026‑10‑09 — the numbers `/highlow?period=WEEK`
returned for this window before, now with day precision instead of hour.

### 3.3 `YEAR` — fortnight records are OHLC aggregates of their bin

`GET /stock/AAPL/historic?period=YEAR` → **200**
```json
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "YEAR", "interval": "2wk",
  "referenceDate": "2026-10-21",
  "window": {"start": "2025-10-22", "end": "2026-10-21"},
  "count": 27,
  "data": [
    {"datetime": "2025-10-22", "open": 211.06, "high": 211.08, "low": 211.06, "close": 211.07},
    {"datetime": "2025-11-05", "open": 211.09, "high": 211.49, "low": 211.08, "close": 211.48},
    "...",
    {"datetime": "2026-10-21", "open": 221.17, "high": 221.56, "low": 221.16, "close": 221.55}
  ]
}
```

Before, the 2025‑11‑05 record was only `{"price": 211.48}` — the close of
that one day; a high on, say, 2025‑10‑30 was invisible. Now the record
spans its whole bin (2025‑10‑23 … 11‑05): `open` of its first session,
`high`/`low` over all of them, `close` of its last. `max(high)` over
the 27 records is the year's true daily high.

## 4. What changes for existing clients

| | before | after |
|---|---|---|
| `/historic` record fields | `datetime`, `price` | `datetime`, `open`, `high`, `low`, `close` (`price` is `close`) |
| Fortnight / month record meaning | last session's close | bin aggregate (close unchanged, open/high/low added) |
| `/highlow` | `{ticker, symbol, period, interval, referenceDate, window, high, highOccured, low, lowOccured}` | **404** — use `max(high)` / `min(low)` over `/historic` |
| Precision of "when" for the extreme | DAY 5 min, WEEK 1 h, longer 1 day | DAY 1 h, longer 1 day |
| NaN | a bar without a close was skipped | same; a missing open/high/low is `null` |
| Window rule, `referenceDate`, `window`, validation, errors, dotted symbols, budget | unchanged | unchanged |

## 5. Internals

| | before | after |
|---|---|---|
| `_serve` | `(ticker, period, referenceDate, Bars, view)` — `view` chosen per endpoint | `(ticker, period, referenceDate, Bars, render)` — one caller |
| Downsampling | `_last_per_bin`, `_last_per_calendar_month` (keep one bar) | `_fortnightly`, `_monthly` → `_aggregate_bins` (first/max/min/last per bin, stamped with the bin's last bar) |
| Serialisation | `_to_points` (`price` = Close) | `_to_records` (OHLC, `null` for a missing field) |
| Removed | `SESSION_INTERVAL`, `SESSION_RETENTION_DAYS`, `_HIGHLOW_BARS`, `_HISTORIC_BARS`/`_HISTORIC_DOWNSAMPLE` (→ `_BARS`/`_DOWNSAMPLE`), `_compute_extremes`, `_format_occurrence`, `get_high_low`; `FakeMarket.five_minute` | |

## 6. Tests

| file | before | after |
|---|---|---|
| `test_window_rule.py` | 178 (every test × 2 endpoints, cross-endpoint invariants) | 102 — single endpoint; the invariant became "`max(high)` / `min(low)` over the records equal the window's bar extremes"; `/highlow` is 404 |
| `test_historic_endpoint.py` | 23 | 27 — OHLC shape, per-bar equality with the market's bars, bin aggregation (mid-bin spike surfaces, DST/closure-safe), bar-level window edges (from the old highlow suite), NaN → `null` |
| `test_highlow_endpoint.py` | 26 | **deleted** |
| `test_dotted_tickers.py` | 4 | 4 (highlow call → anchored historic call) |
| `test_snapshot_endpoint.py` | 9 | 9 (route list: two stock routes) |
| `test_live_yfinance.py` | 30 | 24 — **not run** (no network); adds an OHLC sanity check per record and a YEAR-vs-MONTH aggregation cross-check |
| **total offline** | 243 | **145 passed, 0 failed** (~9 s) |
