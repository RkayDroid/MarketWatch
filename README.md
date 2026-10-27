# Market Watch Lite

A small FastAPI service exposing stock data from Yahoo Finance (via
[yfinance](https://github.com/ranaroussi/yfinance)). Every API request is
served by a live yfinance call — **there is no caching in this version**
(deliberately, until the use cases are settled; see *Upstream request
budget*). A hard upstream rate limiter keeps the service inside yfinance's
informal free-tier budget of **~2000 requests/hour**.

```
pip install -r requirements.txt
uvicorn main:app --reload
open http://127.0.0.1:8000/docs
```

## API

All stock endpoints:

* upper-case the symbol (`/stock/aapl` == `/stock/AAPL`);
* accept `period` case-insensitively (`week`, `Week`, `WEEK`);
* return `404` when Yahoo has no data for the symbol/period, `422` for an
  invalid `period` (without spending any upstream budget), `429` when either
  our local hourly budget or Yahoo's own limiter is hit, and `502` for any
  other upstream failure — never a raw `500`;
* accept Yahoo-native dotted symbols (`SAP.DE`, `0700.HK`, `BARC.L`) as-is,
  and automatically retry US share classes typed with a dot (`BRK.B`) in
  Yahoo's dash form (`BRK-B`).

`period` values: `DAY`, `WEEK`, `MONTH`, `HALF_YEAR`, `YEAR`, `FIVE_YEAR`.

---

### 1. `GET /stock/{symbol}` — snapshot

```json
{
  "ticker": "AAPL",
  "sector": "Technology",
  "fulldayPrice": 347.725,
  "currency": "USD",
  "symbol": "AAPL",
  "language": "en-US",
  "longName": "Apple Inc.",
  "exchange": "NMS",
  "market": "us_market",
  "displayName": "Apple"
}
```

| field | source (yfinance `.info`) |
|---|---|
| `fulldayPrice` | `regularMarketPrice` — the live price during the session, the official close after it; falls back to `currentPrice`, then `previousClose` |
| `sector` | company profile; **null for ETFs, indices and funds** |
| `displayName` | `displayName`, falling back to `shortName` (ETFs have no display name) |
| `longName` | `longName`, falling back to `shortName` |
| others | passed through as-is |

Upstream cost: **2 requests per call** (`.info` is `quoteSummary` + `quote`).

---

### 2. `GET /stock/{symbol}/historic?period=DAY[&referenceDate=YYYY-MM-DD]` — price series

Default `period=DAY`. Without `referenceDate` the window is the last
`period` ending now; with `referenceDate` the same window length **ends on
that date (inclusive)** — see below.

| period | window | one record per | `datetime` format |
|---|---|---|---|
| `DAY` | last 24 hours | hour | ISO-8601 with exchange UTC offset |
| `WEEK` | last 7 days | trading day | `YYYY-MM-DD` |
| `MONTH` | last 30 days | trading day | `YYYY-MM-DD` |
| `HALF_YEAR` | last 6 months | fortnight (14-day bin) | `YYYY-MM-DD` |
| `YEAR` | last 12 months | fortnight (14-day bin) | `YYYY-MM-DD` |
| `FIVE_YEAR` | last 5 years | calendar month | `YYYY-MM-DD` |

```json
{
  "ticker": "AAPL",
  "period": "DAY",
  "symbol": "AAPL",
  "interval": "1h",
  "referenceDate": null,
  "count": 7,
  "data": [
    { "datetime": "2024-05-01T09:30:00-04:00", "price": 172.34 },
    { "datetime": "2024-05-01T10:30:00-04:00", "price": 172.91 }
  ]
}
```

* `price` is the bar's **Close**. For fortnight/month records it is the close
  of the last trading day in that bin and `datetime` is that day. Bins are
  anchored at the newest bar, so the final record is always the latest close.
* `interval` is `1h`, `1d`, `2wk` or `1mo`.
* Day-or-coarser records carry a date only (there is no meaningful time for
  a daily close) — the same convention as `/highlow`.
* `DAY` during a long weekend: if nothing traded in the last 24h the most
  recent 24 hourly bars are returned rather than an empty list.
* Records exist only for trading days; weekends/holidays are absent.

Upstream cost: **1 request per call.** `DAY` fetches 2 days of hourly bars
(a second request falls back to 5 days on a long weekend); each daily
period fetches the smallest standard Yahoo range that covers it
(`WEEK`→`1mo`, `MONTH`→`3mo`, `HALF_YEAR`→`1y`, `YEAR`→`2y`,
`FIVE_YEAR`→`5y`) and slices it to the exact look-back locally.

#### `referenceDate` — end the window on a given date

`GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-23`

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "WEEK",
  "interval": "1d",
  "referenceDate": "2026-10-23",
  "count": 5,
  "data": [
    { "datetime": "2026-10-19", "price": 172.34 },
    { "datetime": "2026-10-20", "price": 173.10 },
    { "datetime": "2026-10-21", "price": 171.80 },
    { "datetime": "2026-10-22", "price": 174.02 },
    { "datetime": "2026-10-23", "price": 175.55 }
  ]
}
```

* Format `YYYY-MM-DD`. The window is the `period`'s length **ending on
  `referenceDate`, inclusive**, in calendar days of the exchange's time
  zone: `DAY` → the hourly bars of that trading date, `WEEK` → the 7 days
  up to and including it (`2026-10-17`…`2026-10-23` above), `MONTH` → 30
  days, `HALF_YEAR` → 182, `YEAR` → 365, `FIVE_YEAR` → 5 years. Record
  granularity and downsampling are exactly as in the table above; bins are
  anchored at the newest bar of the window, so the last record is the last
  close on or before `referenceDate`.
* `referenceDate` is echoed in the response and is `null` for the default
  rolling window.
* `ticker` echoes the request; `symbol` is the symbol Yahoo actually
  answered for. They differ only for US share classes typed with a dot
  (`/stock/BRK.B/historic` → `"ticker": "BRK.B", "symbol": "BRK-B"`), the
  same rule the snapshot endpoint applies.
* `DAY` with a `referenceDate` on which the market was closed is a `404`
  (there is nothing to show for that day) — the "last 24 bars" fallback
  only applies to the rolling window.
* Rejected with `422`, **without any upstream call**: malformed dates,
  dates in the future, and `DAY` more than ~730 days back (Yahoo keeps
  hourly bars for 730 days only; daily periods go back decades).

Upstream cost: **1 request per call**, for exactly the requested window
(`start`/`end`, hourly bars for `DAY`, daily bars otherwise).

---

### 3. `GET /stock/{symbol}/highlow?period=WEEK` — highest / lowest traded price

Default `period=WEEK`.

```json
{
  "symbol": "AAPL",
  "period": "WEEK",
  "high": 195.12,
  "highOccured": "2024-05-01T10:00:00-04:00",
  "low": 188.4,
  "lowOccured": "2024-04-29T15:00:00-04:00"
}
```

* `high`/`low` are the bar **High/Low** (true traded extremes, not closes).
* `*Occured` precision follows the bar size:

| period | bars | `*Occured` |
|---|---|---|
| `DAY` | last session @ 5-minute bars | 5-min bar start, ISO-8601 w/ offset |
| `WEEK` | last 5 trading days @ hourly bars | hourly bar start, ISO-8601 w/ offset |
| `MONTH`, `HALF_YEAR`, `YEAR`, `FIVE_YEAR` | daily bars (smallest covering Yahoo range, sliced) | `YYYY-MM-DD` |

Upstream cost: **1 request per call** (`DAY` may need a second one on a
weekend).

---

### Utility

* `GET /status` — liveness, plus how much of the hourly upstream budget
  is left: `{"status": "online", ..., "upstream_budget": {"per_hour": 1800, "remaining": 1793}}`.
* `GET /time` — current time in major time zones.

## Upstream request budget

Yahoo's chart endpoint takes **one symbol per request** and retains intraday
bars for a limited time only (~60 days for sub-hourly bars, 730 days for
hourly), so there is no way to batch symbols or to get long windows at
intraday resolution. What each API call costs:

| API call | Yahoo call(s) |
|---|---|
| `/stock/{symbol}` | `quoteSummary` + `quote` (= yfinance `.info`) → **2** |
| `/historic DAY` | `2d @ 60m` (+ `5d @ 60m` fallback on a long weekend) → **1–2** |
| `/historic WEEK … FIVE_YEAR` | `1mo / 3mo / 1y / 2y / 5y @ 1d` → **1** |
| `/historic … &referenceDate=` | `start/end @ 60m` (DAY) or `@ 1d` → **1** |
| `/highlow DAY` | `1d @ 5m` (+ `5d` fallback) → **1–2** |
| `/highlow WEEK` | `5d @ 60m` → **1** |
| `/highlow MONTH … FIVE_YEAR` | `3mo / 1y / 2y / 5y @ 1d` → **1** |

Dotted symbols that need the dash retry pay once more. yfinance itself
adds a one-off, uncounted timezone-lookup request the first time a process
sees a symbol via `start`/`end`.

* **No cache**: request volume on the API maps 1:1 onto Yahoo traffic. This
  is intentional for now — the earlier two-tier cache (memory + SQLite)
  was removed to level-set before designing a caching layer around a
  complete set of use cases. EVALUATION.md records the options considered.
* **Limiter**: upstream calls are counted in a sliding 60-minute window
  capped at `UPSTREAM_CALL_BUDGET_PER_HOUR = 1800` (10% headroom under 2000
  for other traffic on the same IP). Beyond that the API returns `429`
  immediately instead of hammering Yahoo and risking an IP block. Every
  real HTTP request counts, including fallbacks, dash retries and both
  halves of `.info`. `GET /status` shows the remaining budget.
* Practical consequence: 1800 requests/hour is ~30 API calls/minute
  sustained. A dashboard polling several symbols every few seconds will hit
  the limiter; that is the signal that the caching layer is needed.

The limiter lives in `rate_limit.py` and is per-process (threads are
fine). For multiple workers or instances, back it with Redis so the budget
is global.

## Symbols with dots

FastAPI/Starlette path parameters accept dots, so `/stock/SAP.DE/historic`
routes fine. Yahoo's own convention matters more:

* **Exchange suffixes use dots** — `SAP.DE`, `0700.HK`, `BARC.L`,
  `RELIANCE.NS` — and are passed through unchanged.
* **US share classes use a dash on Yahoo** — `BRK-B`, `BF-B` — while most
  people type `BRK.B`. When a dotted symbol returns nothing, the request is
  retried once with dots replaced by dashes (a second, budget-charged
  call). This applies to every endpoint - snapshot, historic (with or
  without `referenceDate`) and highlow all go through the same retry - and
  fires whether yfinance signals "unknown symbol" with an empty frame (its
  default) or by raising (`yf.config.debug.hide_exceptions = False`). The
  `ticker` field echoes the symbol the client asked for; the snapshot's and
  historic's `symbol` field shows Yahoo's canonical (dashed) form so the
  translation is visible.

## Tests

```
pip install -r requirements-dev.txt
pytest                                            # 110 offline tests, ~7s
LIVE_YF=1 pytest tests/test_live_yfinance.py -v   # 27 opt-in tests against real Yahoo
```

The offline suite patches `yfinance.Ticker` with a fake whose frames match
the real library's shape (tz-aware `Date`/`Datetime` index, `int64` Volume,
`Dividends`/`Stock Splits` columns, NaNs) and runs requests through the full
FastAPI stack including JSON rendering. The live tests need network access
and a handful of real requests.

## Layout

```
main.py            FastAPI app (+ /status, /time)
stock_router.py    the /stock endpoints and all yfinance access
rate_limit.py      SlidingWindowRateLimiter (stdlib only)
tests/             offline suite + opt-in live tests
EVALUATION.md      original project evaluation, yfinance option analysis, change history
```
