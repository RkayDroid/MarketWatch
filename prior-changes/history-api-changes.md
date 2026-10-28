# Before / after: `referenceDate` on `/historic`, cache removal, clean-up

Baseline = git commit `8ef7eae`. "After" = the working tree at the time of
writing. Sample responses below were produced by running the actual app
against the test double (prices are synthetic; shapes, keys, ordering and
error messages are exactly what the code emits).

## 1. Scope

| asked | delivered |
|---|---|
| `GET /stock/{ticker}/historic` accepts an optional date parameter for the existing `period` | `referenceDate=YYYY-MM-DD` — the `period`-long window **ends on that date, inclusive** |
| …accepting up to 5 comma-separated unique values, response keyed by date | `referenceDate=d1,d2,…` (≤ 5, unique, validated up front), `results: {date: {count, data}}` |
| minimise yfinance hits → later *descoped*: remove caching entirely to level-set | cache (memory + SQLite), `/cache` routes, `X-Cache`, `cached` field all removed; limiter kept |
| delete dead code | `market_data.py`, `mainxx.py`, `.DS_Store`, 3 stale test files (46 permanently failing tests) |

## 2. At a glance

| | before | after |
|---|---|---|
| Application code (lines) | 1 464 across 6 modules | **837 across 3 modules** (−43 %) |
| Modules | `main`, `stock_router`, `caching`, `rate_limit`, `market_data` (orphaned), `mainxx` (scaffold) | `main`, `stock_router`, `rate_limit` |
| Routes | `/stock/{t}`, `/stock/{t}/historic`, `/stock/{t}/highlow`, `DELETE /cache`, `GET /cache/stats`, `/status`, `/time` | `/stock/{t}`, `/stock/{t}/historic`, `/stock/{t}/highlow`, `/status`, `/time` |
| Offline test run (`pytest`) | **46 failed**, 133 passed, 23 skipped | **0 failed, 135 passed**, 28 skipped (live, opt-in) |
| Test code (lines) | 2 527 | 1 923 |
| Config surface | `MWL_CACHE_TTL_SECONDS`, `MWL_CACHE_SHORT_TTL_SECONDS`, `MWL_MEMORY_CACHE_MAX_ENTRIES`, `MWL_DISK_CACHE_MAX_MB`, `MWL_CACHE_DIR` | none (limiter constant `UPSTREAM_CALL_BUDGET_PER_HOUR = 1800`) |
| State on disk | `.cache/cache.sqlite` | nothing |
| Global side effects at import | `market_data.py` set `yf.config.debug.hide_exceptions = False` (if ever imported) | none |

## 3. `GET /stock/{ticker}/historic`

### 3.1 Parameters

| | before | after |
|---|---|---|
| `period` | `DAY` (default) `WEEK` `MONTH` `HALF_YEAR` `YEAR` `FIVE_YEAR`, case-insensitive | unchanged |
| `referenceDate` | — | optional; `YYYY-MM-DD` or up to 5 comma-separated unique dates; window of `period`'s length **ending on each date inclusive**, in exchange-local calendar days |

### 3.2 Rolling window (no `referenceDate`) — shape change only

`GET /stock/AAPL/historic?period=WEEK`

<table><tr><th>before</th><th>after</th></tr><tr><td>

```json
{
  "ticker": "AAPL",
  "period": "WEEK",
  "interval": "1d",
  "count": 5,
  "cached": true,
  "data": [
    {"datetime": "2026-10-21", "price": 151.29},
    {"datetime": "2026-10-22", "price": 151.21},
    {"datetime": "2026-10-23", "price": 152.01},
    {"datetime": "2026-10-26", "price": 153.74},
    {"datetime": "2026-10-27", "price": 152.93}
  ]
}
```
+ header `X-Cache: MEMORY | DISK | MISS`
</td><td>

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "WEEK",
  "interval": "1d",
  "count": 5,
  "data": [
    {"datetime": "2026-10-21", "price": 151.29},
    {"datetime": "2026-10-22", "price": 151.21},
    {"datetime": "2026-10-23", "price": 152.01},
    {"datetime": "2026-10-26", "price": 153.74},
    {"datetime": "2026-10-27", "price": 152.93}
  ]
}
```
no cache header
</td></tr></table>

* `cached` removed (there is no cache). `X-Cache` header removed.
* `symbol` added: the symbol Yahoo actually answered for. Equals `ticker`
  except for US share classes typed with a dot — `/stock/BRK.B/historic`
  → `"ticker": "BRK.B", "symbol": "BRK-B"` — the same convention the
  snapshot endpoint already used. Makes the dot→dash retry visible.
* `data`, `count`, `interval`, window semantics, downsampling (fortnight /
  month bins), `DAY` 24-hour rolling window with "last 24 bars" fallback:
  **unchanged**.

### 3.3 Single `referenceDate` — new

`GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-23` (a Friday)

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "WEEK",
  "interval": "1d",
  "results": {
    "2026-10-23": {
      "count": 5,
      "data": [
        {"datetime": "2026-10-19", "price": 150.7},
        {"datetime": "2026-10-20", "price": 150.47},
        {"datetime": "2026-10-21", "price": 151.29},
        {"datetime": "2026-10-22", "price": 151.21},
        {"datetime": "2026-10-23", "price": 152.01}
      ]
    }
  }
}
```

The window is `2026-10-17 … 2026-10-23` (7 calendar days ending on the
reference date, inclusive) → trading days Mon 19th – Fri 23rd. The
reference date itself is the last record.

### 3.4 Multiple `referenceDate`s — new

`GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-23,2026-10-16`

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "WEEK",
  "interval": "1d",
  "results": {
    "2026-10-23": {
      "count": 5,
      "data": [
        {"datetime": "2026-10-19", "price": 150.7},
        {"datetime": "2026-10-20", "price": 150.47},
        {"datetime": "2026-10-21", "price": 151.29},
        {"datetime": "2026-10-22", "price": 151.21},
        {"datetime": "2026-10-23", "price": 152.01}
      ]
    },
    "2026-10-16": {
      "count": 5,
      "data": [
        {"datetime": "2026-10-12", "price": 148.04},
        {"datetime": "2026-10-13", "price": 150.44},
        {"datetime": "2026-10-14", "price": 151.36},
        {"datetime": "2026-10-15", "price": 149.92},
        {"datetime": "2026-10-16", "price": 150.01}
      ]
    }
  }
}
```

* Keys appear **in the order given**; each value is exactly what the
  single-date request for that date returns.
* One upstream call per date (here: 2).

### 3.5 `DAY` with a closed day in the list — partial emptiness is data

`GET /stock/AAPL/historic?period=DAY&referenceDate=2026-10-23,2026-10-25` (Friday, Sunday)

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "DAY",
  "interval": "1h",
  "results": {
    "2026-10-23": {
      "count": 7,
      "data": [
        {"datetime": "2026-10-23T09:30:00-04:00", "price": 169.87},
        {"datetime": "2026-10-23T10:30:00-04:00", "price": 169.53},
        {"datetime": "2026-10-23T11:30:00-04:00", "price": 169.92},
        {"datetime": "2026-10-23T12:30:00-04:00", "price": 170.0},
        {"datetime": "2026-10-23T13:30:00-04:00", "price": 170.03},
        {"datetime": "2026-10-23T14:30:00-04:00", "price": 169.8},
        {"datetime": "2026-10-23T15:30:00-04:00", "price": 169.75}
      ]
    },
    "2026-10-25": {
      "count": 0,
      "data": []
    }
  }
}
```

`DAY` + `referenceDate` is **that exchange-local trading date** (hourly
bars), not "24 hours back from now"; a DST-change day correctly has 23 or
25 bars. A closed day is `count: 0`; the request is `404` only if every
date is empty:

`GET /stock/AAPL/historic?period=DAY&referenceDate=2026-10-25` → **404**
```json
{"detail": "No price data found for AAPL over period DAY ending on 2026-10-25."}
```

### 3.6 Validation — all `422`, all **before any upstream call**, message names the item

| request | response |
|---|---|
| `…&referenceDate=2026-10-23,2026-10-16,2026-10-23` | `{"detail": "referenceDate contains '2026-10-23' more than once."}` |
| `…&referenceDate=d1,d2,d3,d4,d5,d6` | `{"detail": "referenceDate accepts at most 5 dates, got 6."}` |
| `…&referenceDate=2026-10-23,,2026-10-16` (also trailing comma, blank) | `{"detail": "referenceDate contains an empty item; expected comma-separated YYYY-MM-DD dates."}` |
| `…&referenceDate=23/10/2026` (also `23-10-2026`, `20261023`, `yesterday`) | `{"detail": "referenceDate '23/10/2026' is not a valid YYYY-MM-DD date."}` |
| `…&referenceDate=2026-02-30` | `{"detail": "referenceDate '2026-02-30' is not a valid calendar date."}` |
| `…&referenceDate=2030-01-01` | `{"detail": "referenceDate 2030-01-01 is in the future (today is 2026-10-27 UTC)."}` |
| `period=DAY&referenceDate=2019-01-02` | `{"detail": "referenceDate 2019-01-02 is too old for period DAY: Yahoo Finance keeps hourly bars for ~730 days only. Use a more recent referenceDate or a daily period (WEEK, MONTH...)."}` |
| `period=DECADE&…` | `422` (unchanged from before) |

Whitespace around items is tolerated (`2026-10-23, 2026-10-16`). A date
one day ahead of UTC is accepted so exchanges east of UTC (Tokyo, Sydney)
can ask for their current local date.

## 4. Other endpoints

| endpoint | before | after |
|---|---|---|
| `GET /stock/{ticker}` (snapshot) | 10-field body, `X-Cache` header, `.info` cached 5 min | same 10-field body, no header, live `.info` on every call (2 requests) |
| `GET /stock/{ticker}/highlow` | 6-field body, `X-Cache` header; `MONTH…FIVE_YEAR` sliced from a cached 5-year daily frame | same 6-field body, no header; each period fetches the smallest covering Yahoo range (`3mo / 1y / 2y / 5y`) |
| `GET /status` | `{"status": "online", "message": "..."}` | adds `"upstream_budget": {"per_hour": 1800, "remaining": 1793}` (moved here from `/cache/stats`) |
| `DELETE /cache`, `GET /cache/stats` | existed | **removed** |
| `GET /time` | unchanged | unchanged |

## 5. Upstream (Yahoo) cost per API call

| API call | before (cache warm / cold) | after (always) |
|---|---|---|
| `/stock/{t}` | 0 / 2 | **2** |
| `/historic DAY` | 0 / 1–2 | **1–2** (5d fallback on a long weekend) |
| `/historic WEEK…FIVE_YEAR` | 0 / 1 (`5y @ 1d`, ~1 260 rows, shared by 9 views for a day) | **1** (`1mo / 3mo / 1y / 2y / 5y @ 1d`, sized to the period) |
| `/historic …&referenceDate=` | — | **1 per date** (≤ 5), `start`/`end` for exactly that window |
| `/highlow DAY` | 0 / 1–2 | **1–2** |
| `/highlow WEEK` | 0 / 1 | **1** |
| `/highlow MONTH…FIVE_YEAR` | 0 / 1 (shared frame) | **1** |

Dotted symbols that need the dash retry pay once more, as before. The
consequence of removing the cache is that API volume maps 1:1 onto Yahoo
traffic: ~30 API calls/minute sustained before the limiter answers `429`.
`GET /status` exposes the remaining budget so this can be watched.
Caching options (A–E, with pros/cons) are recorded in EVALUATION.md for
the next design round.

## 6. Internals

| | before | after |
|---|---|---|
| Upstream access | `_run_upstream(key, fetch, ttl)` → `get_or_fetch(TieredCache)`; five `_get_*_frame()` getters with cache keys and per-frame TTLs | `_upstream(fetch)` — error mapping only; plain `_fetch_*_frame()` functions |
| Chart calls | `_fetch_history(ticker, period, interval)` only; dash retry on empty frame | `_fetch_chart()` choke point used by `_fetch_history(period)` and `_fetch_window(start, end)`; dash retry on empty frame **or** `YFTickerMissingError` (yfinance raises instead of returning empty when `hide_exceptions=False`) |
| Daily-period source | one `5y @ 1d` frame, sliced per period (designed for cache sharing) | `_DAILY_YAHOO_RANGE` per period, sliced per period |
| Symbol reporting | snapshot only (`.info["symbol"]`) | also `/historic`, via `df.attrs["yahoo_symbol"]` set by `_fetch_chart` |
| Reference-date parsing | — | `_parse_reference_dates()` (strict regex + `date.fromisoformat`, ≤ 5, unique) + `_validate_reference_date()` (future / hourly retention), both before any call |
| Error mapping | 404 / 422 / 429 (local budget or Yahoo) / 502, never 500 | unchanged |
| Rate limiter | `rate_limit.py`, 1 800/h sliding window | unchanged |

## 7. Files

| removed | why |
|---|---|
| `caching.py` (291 lines) | cache removed to level-set before a use-case-driven design |
| `market_data.py` (493 lines) | orphaned parallel implementation (own cache, budget, loaders, old `/hourly` builder); nothing imported it; set a yfinance global at import |
| `mainxx.py` | PyCharm scaffold |
| `.DS_Store` | noise |
| `tests/test_hourly_endpoint.py`, `tests/test_minmax_endpoint.py`, `tests/test_history_and_info_endpoints.py` | tests for routes removed long before the baseline — the 46 permanent failures |
| `tests/test_caching.py` (36), `tests/test_ttl_policy.py` (25) | cache tests |

| renamed / added | |
|---|---|
| `tests/test_cache_endpoints_and_dotted_tickers.py` → `tests/test_dotted_tickers.py` | kept the dotted-symbol tests, dropped the cache half |
| `tests/test_historic_reference_date.py` (new, 67 tests) | the feature |
| `CHANGES.md` (this file) | |

## 8. Tests

| file | before | after |
|---|---|---|
| `test_historic_endpoint.py` | 22 (shared-frame + TTL guarantees) | 25 (per-period range, every request is a live call, 429 with no cached fallback) |
| `test_historic_reference_date.py` | — | **67**: keyed shape; multi-date order, per-date equality with single-date, one call per date, 5 ok / 6 rejected, duplicates (incl. whitespace-disguised), every empty-item variant, one bad item anywhere in the list with zero calls, closed day beside a trading day, all-empty → 404, 429 mid-list, dotted symbol; start-inclusive/end-exclusive at the yfinance boundary for every period; window contents per period; DAY = exchange-local date incl. DST day; NaN handling; 404/429/502 mapping |
| `test_highlow_endpoint.py` | 19 | 22 (range per period, six periods = six calls) |
| `test_snapshot_endpoint.py` | 9 | 9 (pays twice on repeat) |
| `test_dotted_tickers.py` | 14 (as `test_cache_endpoints_and_dotted_tickers.py`, incl. cache) | 8 (dotted only; + retry when yfinance raises) |
| `test_rate_limit.py` | 4 | 4 (unchanged) |
| `test_live_yfinance.py` (opt-in, `LIVE_YF=1`) | 23 | 28 (per-request cost, `referenceDate` single / multi / DAY / beyond retention / 2015 window) |
| stale files | 46 failing | deleted |
| **total offline** | **133 passed / 46 failed** | **135 passed / 0 failed** |

The live tests could not be run in the sandbox (no network to Yahoo);
they encode the assumptions worth confirming once: a past `WEEK` ending
on a weekday has that weekday as its last record; three `referenceDate`s
cost three calls; a past `DAY` is 6–8 hourly bars on that date.

## 9. Behaviour deliberately preserved

* All `period` semantics, record granularity and downsampling.
* `DAY` rolling window (24 h, "last 24 bars" fallback on long weekends) —
  the fallback intentionally does **not** apply to an explicit date.
* Status code mapping: 404 no data, 422 bad input without upstream spend,
  429 local budget or Yahoo limiter, 502 any other upstream failure.
* Dotted symbols: exchange suffixes pass through, US share classes retry
  in dash form, retry charged to the budget.
* The 1 800/h rate limiter.
