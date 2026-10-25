# Market Watch Lite

A small FastAPI service exposing stock data from Yahoo Finance (via
[yfinance](https://github.com/ranaroussi/yfinance)), built to stay inside
yfinance's informal free-tier budget of **~2000 requests/hour** through a
two-tier (memory + disk) cache, a hard upstream rate limiter, and sharing
one upstream fetch across as many API views as possible.

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
* report where the data came from in an `X-Cache: MEMORY | DISK | MISS`
  response header;
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

Upstream cost: **2 requests** (`.info` is `quoteSummary` + `quote`). **TTL: 5 minutes.**

---

### 2. `GET /stock/{symbol}/historic?period=DAY` — price series

Default `period=DAY`.

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
  "interval": "1h",
  "count": 7,
  "cached": false,
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

Upstream cost: `DAY` = 1 request (hourly bars); **`WEEK`…`FIVE_YEAR` share a
single cached 5-year daily request per symbol**, which is also the frame
`/highlow` uses for its longer periods.
**TTL: 5 minutes for `DAY`, 1 day for every other period.**

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
| `MONTH`, `HALF_YEAR`, `YEAR`, `FIVE_YEAR` | daily bars from the shared 5-year frame | `YYYY-MM-DD` |

Upstream cost: `DAY` and `WEEK` = 1 request each; the four longer periods
share the cached 5-year daily request.
**TTL: 5 minutes for `DAY`, 1 day for every other period.**

---

### Cache administration

* `DELETE /cache` — drop every cached entry from **both** tiers; returns
  `{"cleared": {"memory": N, "disk": M}, ...stats}`. The next request for
  anything is a live upstream call again.
* `GET /cache/stats` — per-tier entry counts and bounds, the TTL, and how
  much of the hourly upstream budget remains (purges expired entries
  first).

### Utility

* `GET /status` — liveness.
* `GET /time` — current time in major time zones.

## Caching

```
request ──► memory (L1) ──miss──► disk (L2) ──miss──► live yfinance call
               ▲                    ▲                      │
               └──── write-through on every store ─────────┘
```

* **Read order:** memory first, then disk. A disk hit is promoted back into
  memory (keeping its original expiry) so the next read is served from L1.
* **Write order:** every live result is stored in memory and persisted to
  disk immediately (write-through).
* **Two TTLs, chosen per cached frame** and applied identically in both
  tiers. "As of now" data gets the **short TTL (5 minutes)**; everything
  else the **long TTL (1 day)**:

  | cached entry | serves | TTL |
  |---|---|---|
  | snapshot (`.info`) | `GET /stock/{symbol}` | 5 min |
  | hourly bars | `historic?period=DAY` | 5 min |
  | 5-minute session bars | `highlow?period=DAY` | 5 min |
  | 5-day hourly bars | `highlow?period=WEEK` | 1 day |
  | 5-year daily bars | historic `WEEK`…`FIVE_YEAR`, highlow `MONTH`…`FIVE_YEAR` | 1 day |

  Expired entries are dropped on read, swept on writes, and removed by
  `GET /cache/stats` / `DELETE /cache`. Expiry times are persisted, so a
  restart never revives an entry that had already expired, and a disk hit
  promoted to memory keeps its original expiry.
* **Bounded tiers, LRU eviction:** memory by entry count, disk by total
  bytes. When a tier is full its least-recently-used entries go first.
* **Disk exists to survive restarts**, which empty the memory tier. After a
  restart the first request for a still-valid entry is served from disk
  (`X-Cache: DISK`) with no upstream call.

| variable | default | meaning |
|---|---|---|
| `MWL_CACHE_TTL_SECONDS` | `86400` (1 day) | long TTL |
| `MWL_CACHE_SHORT_TTL_SECONDS` | `300` (5 min) | short TTL |
| `MWL_MEMORY_CACHE_MAX_ENTRIES` | `512` | memory tier bound (entries) |
| `MWL_DISK_CACHE_MAX_MB` | `256` | disk tier bound (megabytes) |
| `MWL_CACHE_DIR` | `.cache` | directory of the SQLite file |

The disk tier is SQLite (WAL mode, pickled values, stdlib-only) and tracks
LRU recency with a monotonic access counter so eviction order is
deterministic even for accesses within the same clock tick.

## Upstream request budget

Yahoo's chart endpoint takes **one symbol per request** and retains intraday
bars for only ~60 days, so there is no way to batch symbols or to get long
windows at intraday resolution. What the service does instead:

| source frame (per symbol) | Yahoo call | serves |
|---|---|---|
| hourly | `2d @ 60m` (fallback `5d`) | `/historic DAY` |
| session | `1d @ 5m` (fallback `5d`) | `/highlow DAY` |
| week-intraday | `5d @ 60m` | `/highlow WEEK` |
| **daily5y** | `5y @ 1d` (~1260 rows) | `/historic WEEK MONTH HALF_YEAR YEAR FIVE_YEAR`, `/highlow MONTH HALF_YEAR YEAR FIVE_YEAR` |
| snapshot | `quoteSummary` + `quote` | `/stock/{symbol}` |

So the whole API surface for one symbol (13 views) costs **6 upstream
requests**, then nothing until the entries expire.

* **Cache**: every source frame goes through the two-tier cache above
  (5-minute TTL for the three "as of now" frames, 1 day for the rest).
* **Limiter**: real upstream calls are counted in a sliding 60-minute window
  capped at `UPSTREAM_CALL_BUDGET_PER_HOUR = 1800` (10% headroom under 2000
  for other traffic on the same IP). Beyond that the API returns `429`
  immediately instead of hammering Yahoo and risking an IP block. Cache hits
  never count; every real HTTP request does, including fallbacks and both
  halves of `.info`.
* Worst case per symbol, polled continuously on every view: the three
  short-TTL frames (4 requests: hourly, session, `.info`×2) refresh up to
  12×/hour = **48 requests/hour**; the two long-TTL frames add 2/day. So
  ~37 symbols can be hammered across every view, or ~150 on a single
  short-TTL view, before the limiter engages.
* Dotted-symbol dash retries and the 2d→5d window fallbacks each count as
  separate upstream calls.

The limiter lives in `rate_limit.py`, caching in `caching.py`. Both are
designed for one process (threads are fine). For multiple workers or
instances, replace the memory tier/limiter with Redis so the budget and
cache are global.

## Symbols with dots

FastAPI/Starlette path parameters accept dots, so `/stock/SAP.DE/historic`
routes fine. Yahoo's own convention matters more:

* **Exchange suffixes use dots** — `SAP.DE`, `0700.HK`, `BARC.L`,
  `RELIANCE.NS` — and are passed through unchanged.
* **US share classes use a dash on Yahoo** — `BRK-B`, `BF-B` — while most
  people type `BRK.B`. When a dotted symbol returns nothing, the request is
  retried once with dots replaced by dashes (a second, budget-charged
  call). The response echoes the symbol the client asked for; the
  snapshot's `symbol` field shows Yahoo's canonical (dashed) form.

## Tests

```
pip install -r requirements-dev.txt
pytest                                            # 129 offline tests, ~5s
LIVE_YF=1 pytest tests/test_live_yfinance.py -v   # 23 opt-in tests against real Yahoo
```

The offline suite patches `yfinance.Ticker` with a fake whose frames match
the real library's shape (tz-aware `Date`/`Datetime` index, `int64` Volume,
`Dividends`/`Stock Splits` columns, NaNs) and runs requests through the full
FastAPI stack including JSON rendering. The live tests need network access
and a handful of real requests.

## Layout

```
main.py            FastAPI app (+ /status, /time)
stock_router.py    the /stock endpoints, source-frame logic, /cache admin
caching.py         MemoryLRUCache (L1) + DiskLRUCache (L2) + TieredCache, get_or_fetch
rate_limit.py      SlidingWindowRateLimiter (stdlib only)
tests/             offline suite + opt-in live tests
EVALUATION.md      original project evaluation, yfinance option analysis, change history
```
