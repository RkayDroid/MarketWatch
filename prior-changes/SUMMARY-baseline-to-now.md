# Before / after — baseline `d473f3e` → now

> Continued in `watchlist-users-changes.md` (3.0.0 → 3.3.0: users,
> watchlists, `/time` and the orphaned `caching.py` / `market_data.py`
> removed).

**Every "before" below is real output of the baseline code, every "after"
real output of the current code**, both run against the same synthetic
market with the clock frozen at **Wednesday 2026‑10‑21 14:00 New York**
(market open, session half done) and **Wednesday 2026‑10‑14 a holiday**.
Prices are synthetic; shapes, keys, counts, dates and messages are exactly
what each version emits. `compare_baseline.py` next to this file
reproduces them (it extracts the baseline from git); the three round notes
— `history-api-changes.md`, `window-rule-changes.md`,
`ohlc-records-highlow-removed.md` — keep the step-by-step history.

## 1. In one paragraph

The API went from two endpoints that defined "a period" differently
(`/historic`: 24 absolute hours or N calendar days from a Yahoo range;
`/highlow`: the last session or 5 sessions; `referenceDate`: a calendar
date that could be empty) to **one endpoint, one window rule, richer
records**: `GET /stock/{ticker}/historic` returns the **OHLC bars** of
the window that ends on the **last trading day on or before
`referenceDate`** (default today) — `DAY` = that session, `WEEK` = 5
sessions, `MONTH`/`HALF_YEAR`/`YEAR`/`FIVE_YEAR` = 30/182/365 days/5 years
of calendar time. The response says which dates it served (`window`).
`/highlow` is gone: its answer is `max(high)` / `min(low)` over the same
records. One upstream call per window, no fallbacks, no caching.

## 2. Endpoints

| route | before | after |
|---|---|---|
| `GET /stock/{t}` | 10-field snapshot | **unchanged** |
| `GET /stock/{t}/historic?period=&referenceDate=` | `{datetime, price}` series; `price` = close; window = Yahoo range or N calendar days; `referenceDate` = calendar date, may be empty | `{datetime, open, high, low, close}` series; window by the rule below; `referenceDate` resolves to the last session on or before it; `referenceDate` + `window` in every response |
| `GET /stock/{t}/highlow?period=` | bar High/Low + bar-start timestamp, 5‑min/hourly/daily precision; **no `referenceDate`** (the parameter was silently ignored) | **removed (404)** — see §5 for the replacement |
| `GET /status`, `GET /time` | | unchanged |
| app version | 1.1.0 | 3.0.0 |

### The window rule (new, applies to rolling and anchored alike)

> D′ = the last trading day on or before `referenceDate` (default: the
> exchange's today). `DAY` = the session on D′ · `WEEK` = the 5 sessions
> ending on D′ · `MONTH` / `HALF_YEAR` / `YEAR` / `FIVE_YEAR` = 30 / 182 /
> 365 / 1827 calendar days ending on D′.

## 3. Same request, before and after

### 3.1 `GET /stock/AAPL/historic?period=DAY` — Wednesday 14:00, market open

<table><tr><th>before</th><th>after</th></tr><tr><td>

```json
{
  "ticker": "AAPL", "symbol": "AAPL",
  "period": "DAY", "interval": "1h",
  "count": 7,
  "data": [
    {"datetime": "2026-10-20T14:30:00-04:00", "price": 221.53},
    {"datetime": "2026-10-20T15:30:00-04:00", "price": 221.53},
    "...",
    {"datetime": "2026-10-21T13:30:00-04:00", "price": 221.55}
  ]
}
```
</td><td>

```json
{
  "ticker": "AAPL", "symbol": "AAPL",
  "period": "DAY", "interval": "1h",
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
</td></tr></table>

**Notice:** before, "DAY" at 14:00 was *the last 24 hours* — two bars of
Tuesday afternoon followed by Wednesday morning (7 bars, two sessions).
After, it is *today's session so far* (5 bars, the running 13:30 bar
included), and the response says so. Upstream: `range=2d` → one
`start/end` call.

### 3.2 `GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-18` — a Sunday, after a holiday week

<table><tr><th>before</th><th>after</th></tr><tr><td>

```json
{
  "ticker": "AAPL", "symbol": "AAPL",
  "period": "WEEK", "interval": "1d",
  "results": {
    "2026-10-18": {
      "count": 4,
      "data": [
        {"datetime": "2026-10-12", "price": 221.3},
        {"datetime": "2026-10-13", "price": 221.33},
        {"datetime": "2026-10-15", "price": 221.38},
        {"datetime": "2026-10-16", "price": 221.41}
      ]
    }
  }
}
```
</td><td>

```json
{
  "ticker": "AAPL", "symbol": "AAPL",
  "period": "WEEK", "interval": "1d",
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
</td></tr></table>

**Notice:** before, a "week" was the 7 calendar days ending on the
Sunday — 4 sessions because of the holiday. After, it is 5 sessions,
reaching back to Friday the 9th, and `window` tells the client the
Sunday was served as the window ending Friday the 16th.

### 3.3 `GET /stock/AAPL/historic?period=DAY&referenceDate=2026-10-18` — a Sunday

<table><tr><th>before</th><th>after</th></tr><tr><td>

```json
404
{"detail": "No price data found for AAPL over period DAY ending on 2026-10-18."}
```
</td><td>

```json
200
{
  "ticker": "AAPL", "symbol": "AAPL", "period": "DAY", "interval": "1h",
  "results": {
    "2026-10-18": {
      "window": {"start": "2026-10-16", "end": "2026-10-16"},
      "count": 7,
      "data": [
        {"datetime": "2026-10-16T09:30:00-04:00", "open": 221.4, "high": 221.42, "low": 221.4, "close": 221.41},
        "...",
        {"datetime": "2026-10-16T15:30:00-04:00", "open": 221.41, "high": 221.42, "low": 221.4, "close": 221.41}
      ]
    }
  }
}
```
</td></tr></table>

**Notice:** a non-trading reference date used to be an error; now it is
the session before it (the universal "as of" convention), reported in
`window`.

### 3.4 `GET /stock/AAPL/historic?period=YEAR` — fortnight records

<table><tr><th>before</th><th>after</th></tr><tr><td>

```json
{
  "ticker": "AAPL", "symbol": "AAPL",
  "period": "YEAR", "interval": "2wk",
  "count": 27,
  "data": [
    {"datetime": "2025-10-22", "price": 211.07},
    {"datetime": "2025-11-04", "price": 211.45},
    "...",
    {"datetime": "2026-10-21", "price": 221.55}
  ]
}
```
</td><td>

```json
{
  "ticker": "AAPL", "symbol": "AAPL",
  "period": "YEAR", "interval": "2wk",
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
</td></tr></table>

**Notice two things.** (1) A fortnight record used to be one day's close;
now it is the whole bin — first open, highest high, lowest low, last
close — so nothing that happened mid-bin is invisible. (2) The second
record moved from `2025-11-04` to `2025-11-05`: the baseline measured bin
age with timestamp arithmetic, which is off by one across the November
DST change; bins are now counted in exchange-local calendar days.

### 3.5 `GET /stock/AAPL/highlow?period=WEEK` (and with `&referenceDate=2026-10-18`)

<table><tr><th>before</th><th>after</th></tr><tr><td>

```json
{
  "symbol": "AAPL", "period": "WEEK",
  "high": 221.56, "highOccured": "2026-10-21T09:30:00-04:00",
  "low": 221.37,  "lowOccured":  "2026-10-15T09:30:00-04:00"
}
```
(identical with `&referenceDate=2026-10-18` — the parameter did not exist
and was ignored)
</td><td>

```json
404
{"detail": "Not Found"}
```
Replacement, from `/historic?period=WEEK` in §3.1's shape:
`max(high)` = **221.56** on `2026-10-21`, `min(low)` = **221.37** on
`2026-10-15` — the same numbers, located to the day instead of the hour.
</td></tr></table>

### 3.6 `GET /stock/AAPL/historic?period=MONTH&referenceDate=2015-03-31,1970-01-01`

| | before | after |
|---|---|---|
| `2015-03-31` | `{"count": 22, "data": [{"datetime": "2015-03-02", "price": 99.13}, …]}` | `{"window": {"start": "2015-03-02", "end": "2015-03-31"}, "count": 22, "data": [{"datetime": "2015-03-02", "open": 99.12, "high": 99.14, "low": 99.11, "close": 99.13}, …]}` |
| `1970-01-01` (before the listing) | `{"count": 0, "data": []}` | `null` |

## 4. Behaviour matrix

| situation | before | after |
|---|---|---|
| rolling `DAY` while the market is open | last 24 absolute hours (spans two sessions) | today's session so far |
| rolling `DAY` before the open / on a weekend | 24 h back; on a long weekend "the last 24 bars" (~3½ sessions) | the previous session |
| rolling `WEEK` | 7 calendar days (`1mo` range sliced in UTC) | the last 5 sessions |
| rolling `MONTH` … `FIVE_YEAR` | N calendar days back from *UTC now*, from a Yahoo range | N calendar days ending on the last session, exchange-local |
| `referenceDate` on a Saturday/Sunday/holiday | window ends on that date: `DAY` empty (404 if alone), `WEEK` one session short | window ends on the session before it; `window.end` says which |
| `referenceDate` before the listing | `{"count": 0, "data": []}` | `null` (404 only if every date is null) |
| fortnight / month record | close of the bin's last session | OHLC of the whole bin, same stamp |
| fortnight bins across a DST change | shifted by one day | correct |
| "highest / lowest traded price in the window, and when" | `/highlow` at 5‑min (`DAY`), hourly (`WEEK`), daily precision | `max(high)` / `min(low)` over `/historic`, at hourly (`DAY`) / daily precision |
| `referenceDate` on `/highlow` | silently ignored | n/a (endpoint removed) |
| response tells you what window was served | no | `referenceDate` + `window {start, end}` |
| upstream calls per request | 1–2 (`2d→5d`, `1d→5d` fallbacks) | exactly 1 per window |
| `422` before any upstream call | bad `period`; bad/duplicate/too many/future `referenceDate`; `DAY` > 730 days | unchanged |
| 404 / 429 / 502 mapping, `BRK.B`→`BRK-B` retry, 1 800/h limiter, no cache | | unchanged |

## 5. Client migration

1. **Records:** `price` → `close`; `open`, `high`, `low` added. Every
   field is a number, or `null` if Yahoo had no value for it (a bar with
   no close is omitted, as before).
2. **`/highlow` → `/historic`:** for the same `period` and
   `referenceDate`,
   ```js
   const hi = data.reduce((a, r) => r.high > a.high ? r : a);   // hi.high, hi.datetime
   const lo = data.reduce((a, r) => r.low  < a.low  ? r : a);   // lo.low,  lo.datetime
   ```
   `datetime` locates the extreme to the hour (`DAY`) or the day
   (everything else). If sub-hour timing is ever needed, the extension is
   an `interval` parameter on `/historic`.
3. **Non-trading dates no longer error** — read `window.end` for the
   session actually served. If you relied on a 404 to detect "market
   closed on that date", compare `window.end` with the key instead.
4. **Keyed results:** an empty entry is `null`, not `{count: 0, data: []}`.
5. **`DAY` and `WEEK` mean sessions now**; counts are stable (≤ 8 hourly
   bars, exactly 5 daily records) instead of depending on the clock and
   the calendar.

## 6. Upstream (Yahoo) cost

| request | before | after |
|---|---|---|
| `/historic DAY` | `2d @ 60m`, + `5d @ 60m` on a long weekend → 1–2 | `start/end @ 60m` → **1** |
| `/historic WEEK … FIVE_YEAR` | `1mo / 3mo / 1y / 2y / 5y @ 1d` → 1 | `start/end @ 1d` → **1** |
| `/historic … &referenceDate=` | 1 per date | 1 per date |
| `/highlow` | 1–2 | — |
| `/stock/{t}` | 2 | 2 |

The `start/end` call covers the window plus a 14‑day look-back (so D′ can
be read from the bars that come back) and is clamped to Yahoo's 730‑day
hourly retention. Slightly more rows per call than before, never more
calls.

## 7. Code, tests, docs

| | before | after |
|---|---|---|
| `stock_router.py` | 713 lines: 2 endpoints, Yahoo-range table, 5 frame fetchers with fallbacks, 2 slicers (one in UTC) | 685 lines (a third docstrings): 1 endpoint, `_resolve_window` / `_load_window` / `_serve`, OHLC aggregation; `SESSION_*`, `_DAILY_YAHOO_RANGE`, `_fetch_history`, `_last_24h`, `_slice_lookback`, `_compute_extremes`… removed |
| test double | per-test hand-built frames, unrelated across bar sizes | `FakeMarket`: a synthetic exchange (sessions, a holiday, a 9-day closure, freezable clock) whose hourly and daily bars are aggregations of one price path |
| offline tests | 135 | **145** (`test_window_rule.py` 102, `test_historic_endpoint.py` 27, snapshot 9, dotted 4, limiter 4) — all passing, ~9 s |
| live tests (opt-in) | 28 | 24, rewritten — **not run** (no network in the sandbox) |
| README | per-endpoint window descriptions | one window rule, OHLC records, real samples |
| EVALUATION.md | | decision records for each round, test inventory |

## 8. Known limits / to confirm

* **Precision of "when"** for an extreme is hourly (`DAY`) / daily — the
  old `/highlow` gave 5‑minute / hourly.
* **No exchange calendar**: D′ is inferred from the bars Yahoo returns
  within a 14‑day look-back. A closure longer than 14 days, or a weekend
  date sitting exactly on the 730‑day hourly horizon, resolves to nothing
  (`null` / 404). `exchange_calendars` would make it exact; only
  `_resolve_window` would change.
* The live suite encodes what to check once against real Yahoo: OHLC
  sanity per record, a `YEAR` fortnight's extremes equal the daily bars'
  over the same days, a Sunday resolving to Friday's hourly bars, one call
  per window.
