# Market Watch Lite — Evaluation & New Endpoint

## 1. Project as found

```
main.py           FastAPI app, timezone endpoints + mounts stock_router
stock_router.py   /stock/{ticker}            -> yf.Ticker(ticker).info
                  /stock/{ticker}/history    -> yf.Ticker(ticker).history(period=...)
mainxx.py         Leftover PyCharm scaffold file, unused, dead code
requirements.txt  Broken: pinned exact versions of *transitive* packages,
                  including a non-existent certifi version, so
                  `pip install -r requirements.txt` failed outright.
```

Issues identified:

1. **No caching.** Every single HTTP request to `/stock/...` triggers a brand
   new call to Yahoo Finance via `yfinance`, even if the previous request was
   a second ago for the same ticker. Any real traffic (a dashboard
   auto-refreshing, several users watching the same symbol, a retry loop on
   the client, etc.) multiplies 1:1 into Yahoo requests.
2. **No rate limiting / no backoff.** There is nothing to stop the app from
   exceeding Yahoo's informal free-tier ceiling (~2000 requests/hour per
   source IP), after which Yahoo starts returning HTTP 429s and `yfinance`
   raises `YFRateLimitError`. The existing code treats that the same as any
   other error (generic HTTP 400), so a caller has no idea they've been
   throttled or when to retry.
3. **No endpoint for "last 24h hourly" data** — this is the feature requested.
4. **Broken `requirements.txt`** (see above) — could not `pip install` at all
   in a clean environment.
5. `stock_router.py` and `main.py` catch broad `Exception` and always return
   `400`, even for upstream outages (should be `502`/`429`/`404` depending on
   cause).

## 2. yfinance options evaluated

I inspected the installed `yfinance` source directly (no outbound internet
in this sandbox) to confirm behaviour rather than relying on memory of the
docs. Key findings, with file references from the installed package:

| Need | API | Notes |
|---|---|---|
| Historical / intraday OHLC bars | `Ticker.history(period=, interval=)` | Backed by Yahoo's `GET /v8/finance/chart/{symbol}` (`yfinance/scrapers/history.py:276`). **One symbol per HTTP call — no batch/multi-symbol support.** |
| Convenience multi-ticker history | `yf.download([...])` / `yf.Tickers("A B C").history()` | Still fans out to **one chart-endpoint HTTP request per ticker**, just run concurrently via a thread pool (`yfinance/multi.py`, `yfinance/tickers.py`). It reduces *wall-clock time*, not the number of requests counted against Yahoo's per-IP quota. |
| Full fundamentals snapshot | `Ticker.info` | Most expensive single-symbol call: internally hits `quoteSummary` (`/v10/finance/quoteSummary/{symbol}`) **and** the quote endpoint (`/v7/finance/quote`) to backfill a couple of fields (`yfinance/scrapers/quote.py:660,671`). Two upstream requests per call. |
| Lightweight "latest price" | `Ticker.fast_info` | Cheaper alternative to `.info` when only the latest price/previous close/day range is needed — avoids the heavy `quoteSummary` payload. Still one symbol per call. |
| Multi-symbol *quotes* (snapshot, not history) | Yahoo's `GET /v7/finance/quote?symbols=A,B,C` | This is the **one Yahoo endpoint that genuinely supports many tickers in a single HTTP call** (confirmed in `yfinance/scrapers/quote.py:671`, where yfinance itself calls it with a single symbol, but the endpoint accepts a comma-separated list). It only returns a point-in-time snapshot (price, bid/ask, day high/low, market cap, etc.) — **not** a time series, so it cannot serve the "last 24h hourly" requirement, but it is the right tool for a future "current price for a watchlist" endpoint, since N tickers would cost 1 request instead of N. |
| Built-in 429 handling | `yfinance.exceptions.YFRateLimitError` | yfinance already detects Yahoo's HTTP 429 responses and raises this dedicated exception (`yfinance/data.py:276,370,506`) — the app should catch it explicitly and return a clean `429`, rather than treating it like any other error. |
| Interval/period limits | Docstring of `Ticker.history` / `yf.download` | Valid intervals: `1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo`. `1m` bars: last 7 days only. All other sub-daily intervals (including the `60m`/`1h` bars we need): last 60 days only. This comfortably covers a rolling 24h window. |
| Min/Max over a period | No separate endpoint needed | `history()` already returns `High`/`Low`/`Close` columns for every bar in the requested window, so day/period max & min can be derived client-/server-side (`df['High'].max()`, `df['Low'].min()`) from data you already fetched — **no extra API call** is required just to get a min/max, unlike re-querying `.info` for `fiftyTwoWeekHigh`/`fiftyTwoWeekLow` which is a second round trip. Prefer deriving min/max from an already-fetched `history()` window over issuing a second call. |

**Conclusion used for the design below:** there is no way to fetch
*historical/intraday time-series* data for multiple tickers in a single
Yahoo request — it is fundamentally one request per symbol. The only
lever we have is to avoid *repeating* those per-symbol requests
unnecessarily (caching) and to cap how many we're willing to issue overall
(rate limiting), which is what the new endpoint implements.

## 3. Rate-limit budget design

Given the stated free-tier ceiling of **2000 requests/hour**, the app now:

1. **Caches** every upstream (`yfinance`) response in-process for
   `CACHE_TTL_SECONDS = 300` (5 minutes). Hourly bars only update about once
   a minute upstream and the free data feed itself is delayed, so a 5-minute
   cache costs no real freshness while collapsing repeat requests for a
   popular ticker into one upstream call every 5 minutes.
2. **Rate-limits** real upstream calls with a sliding 60-minute window capped
   at `UPSTREAM_CALL_BUDGET_PER_HOUR = 1800` (a 10% safety margin under
   Yahoo's informal 2000/hour ceiling, leaving headroom for other traffic on
   the same egress IP and for yfinance's own incidental calls). Once the
   budget for the hour is spent, the API immediately returns
   `429 Too Many Requests` with a `Retry-After`-style message instead of
   continuing to hammer Yahoo (which risks a temporary IP-level ban).
3. Distinguishes `429` (rate limited — either locally or by Yahoo),
   `404` (symbol/ticker has no data), and `502` (unexpected `yfinance`
   error), instead of flattening everything to `400`.

**Worst-case math:** with a 5-minute cache TTL, each cached *source frame*
costs at most `60 / 5 = 12` upstream calls/hour no matter how often it is
requested. A symbol has 5 source frames (see README "Upstream request
budget") = 6 requests, so ~72 requests/hour if every view of it is polled
continuously, or 12/hour for a single view. At the 1800/hour budget that is
~25 symbols across every view or ~150 symbols on one view before the
limiter ever engages. If more is needed, increase `CACHE_TTL_SECONDS` or
move to a scheduled background refresher (see "Future work").

Both primitives (`TTLCache`, `SlidingWindowRateLimiter`) live in
`rate_limit.py`, are dependency-free (stdlib only), and are process-local —
see the docstring there for how to swap in a shared store (e.g. Redis) if
this is ever scaled to multiple workers/instances, since the budget should
then be tracked globally rather than per-process.

## 4. API (current state)

The full reference lives in **README.md**. The API was consolidated into
three buckets; this section records the design decisions and what changed.

| endpoint | status |
|---|---|
| `GET /stock/{symbol}` | **reshaped** to a fixed 10-field snapshot (`ticker, sector, fulldayPrice, currency, symbol, language, longName, exchange, market, displayName`) instead of dumping the whole `.info` dict |
| `GET /stock/{symbol}/historic?period=` | **replaces** `/hourly` (now `period=DAY`) and the raw `/history` pass-through; adds WEEK/MONTH (daily), HALF_YEAR/YEAR (fortnightly), FIVE_YEAR (monthly) |
| `GET /stock/{symbol}/historic?period=&referenceDate=` | **added later**: optional `referenceDate=YYYY-MM-DD[,…]` (up to 5 unique dates) makes the `period`-long window end on each date, inclusive; response keyed by date (see "referenceDate" below) |
| `GET /stock/{symbol}/highlow?period=` | unchanged contract |
| `GET /stock/hourly/batch` | **removed** (was a bonus; not in the agreed surface — and it never saved Yahoo quota, see §2) |
| `mainxx.py` | deleted (PyCharm scaffold, dead code) |

### Snapshot decisions
* `.info` is the only yfinance call that yields `sector` (from
  `quoteSummary/assetProfile`); `language/market/displayName/exchange` come
  from the `v7/finance/quote` half. It is **two** HTTP requests and is
  charged as such to the limiter.
* `fulldayPrice` → `regularMarketPrice` (live during the session, official
  close after), falling back to `currentPrice` then `previousClose`. This is
  an interpretation of the field name — easy to remap if a different Yahoo
  field was intended.
* `displayName`/`longName` fall back to `shortName` because ETFs and
  many non-US listings have no `displayName`; `sector` is null for
  ETFs/indices/funds rather than a 404.
* A symbol is "not found" (404) when Yahoo returns none of
  `regularMarketPrice / currentPrice / longName / shortName` — unknown
  symbols come back from yfinance as a near-empty dict, not an error.

### Historic decisions
* **One shared 5-year daily frame per symbol** (`5y @ 1d`, ~1260 rows)
  serves `WEEK`, `MONTH`, `HALF_YEAR`, `YEAR`, `FIVE_YEAR` *and* the four
  long `/highlow` periods. Yahoo has no fortnightly interval and retains
  intraday bars for only ~60 days, so daily bars are the only honest source
  for the long windows anyway; slicing/downsampling locally means nine views
  cost one request.
* Fortnight = 14-calendar-day bins **anchored at the newest bar**, each
  represented by the close of its last trading day. Anchoring at the end
  guarantees the final record is the latest close (what a chart wants),
  at the cost of a possible short partial bin at the old end.
* Month = calendar month, represented by its month-end close (or the latest
  bar for the current month).
* The requirement text said "last six months" for `YEAR` and `FIVE_YEAR`
  too; that was read as a copy-paste slip and implemented as 12 months and
  5 years respectively — **please confirm**.
* Records are `{datetime, price}` as before; `price` is the bar Close.
  Day-or-coarser records use `YYYY-MM-DD` (consistent with `/highlow`),
  hourly records keep the full ISO-8601 timestamp with exchange offset.
* `window_hours` was dropped from the response in favour of `period` +
  `interval` (`1h`, `1d`, `2wk`, `1mo`).

### Highlow decisions (unchanged)
* `high`/`low` are bar High/Low (true traded extremes), numbers not strings.
* `DAY` = last session at 5-minute bars (fallback `5d`, last session only);
  `WEEK` = `5d @ 60m`; longer periods slice the shared daily frame.

### Two-tier cache (added later, simplified on review — **since removed, see "Reset" below**)

First iteration had a pluggable single backend (memory *or* disk) with a
5-minute "fresh" window inside a 7-day "retained stale" window. Review
found the two horizons ambiguous, so it was simplified to:

* `caching.py`: `MemoryLRUCache` (L1, bounded by entries) +
  `DiskLRUCache` (L2, SQLite, bounded by bytes) composed by `TieredCache`:
  **read memory → disk → live; write-through to both; one TTL (default
  1 day) for everything; `clear()` empties both.** The disk tier exists
  only to survive restarts; promotions from disk keep the entry's original
  expiry, and expiry is persisted so restarts cannot revive expired data.
* All bounds and the TTL are env-configured (`MWL_CACHE_TTL_SECONDS`,
  `MWL_MEMORY_CACHE_MAX_ENTRIES`, `MWL_DISK_CACHE_MAX_MB`, `MWL_CACHE_DIR`).
* `X-Cache` now reports the tier: `MEMORY | DISK | MISS`.
* A flat 1-day TTL made "as of now" views stale, so the TTL is chosen
  per cached frame: **5 minutes** for the snapshot and the two frames that
  only serve `period=DAY` (hourly bars → `/historic`, session bars →
  `/highlow`); **1 day** for the 5-day hourly frame and the shared 5-year
  daily frame. Because cache keys are per source frame, the rule
  "5 min for DAY, 1 day otherwise" falls out naturally with no
  per-endpoint special-casing. Both TTLs are env-configurable
  (`MWL_CACHE_SHORT_TTL_SECONDS`, `MWL_CACHE_TTL_SECONDS`).
* Kept from the first iteration: LRU recency via a monotonic counter (a
  frozen-clock test showed timestamp ties made SQLite eviction arbitrary)
  and the limiter guard for `max_calls = 0` (`IndexError` → 502).

### Symbols containing dots (added later)

* There were **no test cases** covering dotted tickers; routing itself was
  verified fine (Starlette path params match dots, `%2E` included).
* Yahoo semantics: dots are native for exchange suffixes (`SAP.DE`,
  `0700.HK`, `BARC.L` — pass-through) but US share classes are dashed on
  Yahoo (`BRK-B`), so `BRK.B` alone returns nothing. All fetch paths
  (chart choke point `_fetch_history` + snapshot `.info`) now retry the
  dash form once when a dotted symbol comes back empty; the retry is
  charged to the budget and the two spellings cache separately.
* Hardened later: the chart retry only fired on an *empty* frame, but
  yfinance reports an unknown symbol by **raising** `YFTickerMissingError`
  (tz / prices missing) when `yf.config.debug.hide_exceptions` is False -
  which the orphaned `market_data.py` sets at import. In that mode
  `BRK.B` on `/historic` became a 502 with no retry. `_fetch_chart` now
  treats the exception and the empty frame identically. The historic
  response also gained `symbol` (the form Yahoo answered for, carried on
  the frame via `DataFrame.attrs` so it survives the disk cache), matching
  the snapshot, so the translation is visible rather than implicit.

### `referenceDate` on `/historic` (added later)

*(The caching remarks in this section describe the state before the
cache was removed; the semantics are unchanged.)*

Requirement: `historic?period=X&referenceDate=2026-10-23` returns the
window of `X`'s length that **ends on that date, inclusive**.

**Semantics.** `[referenceDate − (N−1) days, referenceDate]` in calendar
days of the exchange's time zone, where N is the period's length in days
(DAY 1, WEEK 7, MONTH 30, HALF_YEAR 182, YEAR 365, FIVE_YEAR 5y). So a
`DAY` window is one local trading date (not 24 absolute hours — DST days
and non-US exchanges behave sensibly). Granularity/downsampling is
identical to the rolling window; bins are anchored at the newest bar of
the window so the last record is the last close on or before
`referenceDate`. The response gains one nullable field, `referenceDate`;
nothing existing moved.

**Implementation — deliberately simple, no caching.** The first cut of
this feature reused cached source frames and added two "archive" frames
to keep Yahoo traffic flat. That was judged confusing before the window
semantics were even settled, so it was replaced by the most direct thing
that can be correct: one `yf.Ticker(symbol).history(start=first_day,
end=referenceDate + 1 day, interval=60m|1d)` call per request, bypassing
the cache entirely (`_run_upstream_uncached`: nothing read, nothing
stored; `cached: false`, `X-Cache: MISS`). yfinance interprets naive
dates as exchange-local midnight, `start` inclusive / `end` exclusive
(`yfinance/utils.py:_parse_user_dt`), which is exactly the window; the
result is still sliced locally to the same bounds as a guarantee against
upstream quirks. The rate limiter, the error mapping (404/429/502) and
the `BRK.B`→`BRK-B` retry apply unchanged because the call goes through
the same `_fetch_chart` choke point as every other chart request. The
rolling-window path and its cache are untouched.

**Rejected before any upstream call (422):** malformed dates (FastAPI
`date` parsing), dates more than one day in the future (the one-day slack
is for exchanges east of UTC, which are already on "tomorrow" for part of
every UTC day), and `DAY` more than 730 days back (Yahoo has no hourly
bars that old; the message points at the daily periods). `DAY` on a
non-trading date is an honest 404 — the rolling window's "last 24 bars"
fallback is deliberately not applied to an explicit date.

**Cost and what to do about it later.** Every `referenceDate` request is
one Yahoo request, so a client scrubbing through dates spends budget
linearly. When the semantics are confirmed, the obvious next step is to
cache by window (key `src:{symbol}:{interval}:{first_day}:{day_after}`,
long TTL for windows that end before today, short TTL otherwise), and/or
serve windows inside the already-cached 5-year daily frame from it. Both
fit the existing `_run_upstream` pattern without touching the endpoint.

### Reset: cache removed, dead code deleted (latest)

To level-set before designing a caching layer around a complete set of
use cases, **all caching was removed** — the memory tier, the SQLite disk
tier, the `/cache` admin routes, the `X-Cache` header, the `cached`
response field and the `MWL_*` environment variables. `caching.py` is
gone. What remains between the API and Yahoo is the rate limiter only.

Consequences, and what was changed to keep the code honest without a
cache:

* **Every request is one live chart call** (two for `.info`). The
  "shared 5-year daily frame" design only made sense when that frame was
  cached and reused by nine views; without a cache, fetching five years of
  daily bars to answer `WEEK` is pure waste. Each daily period now fetches
  the **smallest standard Yahoo range that covers it** (`WEEK`→`1mo`,
  `MONTH`→`3mo`, `HALF_YEAR`→`1y`, `YEAR`→`2y`, `FIVE_YEAR`→`5y`) and is
  sliced to the exact look-back locally, exactly as before. Same request
  count, far smaller payloads. `/highlow` uses the same rule.
* `_run_upstream` / `_run_upstream_uncached` collapsed into one
  `_upstream()` that only does error mapping; the per-frame getters became
  plain fetch functions.
* The remaining budget moved from `/cache/stats` to `GET /status`
  (`upstream_budget.remaining`), since it is the only operational signal
  left and the thing to watch for when the cache becomes necessary.
* The `yahoo_symbol` mark on frames (`DataFrame.attrs`) stays — it is how
  `/historic` reports `symbol`; it no longer needs to survive pickling.

Dead code deleted in the same pass:

* `market_data.py` — an orphaned earlier implementation of the same layer
  (its own `TTLCache`, `RequestBudget`, single-flight `fetch()`, loaders,
  and the old `/hourly` response builder). Nothing imported it, but it set
  `yf.config.debug.hide_exceptions = False` at import, a global side
  effect that would have switched yfinance to raising on unknown symbols
  for the whole process the moment anything imported it. Two of its ideas
  are worth revisiting when caching returns — serving stale data when the
  budget is exhausted, and negative-caching "symbol not found" — but
  against the new design, not by resurrecting the file.
* `mainxx.py` (PyCharm scaffold; §6 already claimed it was gone).
* `tests/test_hourly_endpoint.py`, `tests/test_minmax_endpoint.py`,
  `tests/test_history_and_info_endpoints.py` — 46 tests for routes removed
  in §4, failing ever since. `tests/test_caching.py`,
  `tests/test_ttl_policy.py` and the cache half of
  `test_cache_endpoints_and_dotted_tickers.py` (now
  `test_dotted_tickers.py`) went with the cache.
* `.DS_Store`.

**Caching options recorded for the next design round** (from the review
that preceded the reset; the numbers assume the current 1:1 mapping):

| option | idea | pros | cons |
|---|---|---|---|
| A. per-window cache | key = symbol + interval + window bounds; long TTL for windows that end before today, short TTL otherwise | smallest change; closed windows are immutable so they could be cached for days | only identical requests benefit; scrubbing through dates is still one call per step |
| B. reuse a cached wide daily frame | keep one 5-year daily frame per symbol and slice any daily-period window out of it; fall back to A | common case (recent daily windows) costs zero extra calls; responses unchanged | two code paths; the frame is up to a TTL stale so windows touching today need the short-TTL route |
| C. per-symbol archives | `max@1d` (full history) and `max@60m` (Yahoo's whole 730-day hourly retention, ~3.5k rows) per symbol, 1-day TTL; everything is a local slice | hard ceiling of 2 calls/symbol/day whatever clients do | most moving parts; large entries (a 40-year daily frame ≈ 0.7 MB) so the memory bound must become byte-based |
| D. remember symbol resolution | memoise `BRK.B → BRK-B` once learned | removes the wasted dotted attempt (which on the `start/end` path is several unmetered requests) | alias lost on restart; orthogonal to the others, cheap |
| E. no cache (current) | rely on the limiter | simplest, always fresh | linear in request volume; ~30 API calls/minute sustained before `429` |

The recommendation on record was B + D with A as the fallback inside B;
C only if dense date-scrubbing on `DAY` turns out to be a real use case.
Whatever is chosen should also decide: stale-on-exhausted-budget,
negative caching of unknown symbols, and whether multi-worker deployments
need a shared store (the limiter has the same question).

### `referenceDate` accepts up to 5 dates (latest)

`referenceDate=2026-10-23,2026-10-16` — comma-separated, at most
`MAX_REFERENCE_DATES = 5`, unique. Decisions:

* **Keyed response whenever `referenceDate` is present**, even for one
  date: `results: {date: {count, data}}` in the order given, common fields
  (`ticker`, `symbol`, `period`, `interval`) at the top level. A shape
  that flips between flat and keyed depending on how many dates were
  passed would be a trap for clients. The rolling window (no
  `referenceDate`) keeps the flat shape and lost its vestigial
  `referenceDate: null`.
* **Validate the whole list before the first upstream call.** Parsing is
  done by hand (`Optional[str]` + a strict `YYYY-MM-DD` regex +
  `date.fromisoformat`) rather than FastAPI's `date` type, so the 422s
  name the offending item: count > 5, empty item, malformed, impossible
  calendar date, duplicate (after whitespace stripping), future, DAY
  beyond retention. Nothing reaches Yahoo for a request that is going to
  fail on input.
* **Partial emptiness is data, not an error.** `DAY` on a Sunday next to
  a trading day gives `{"count": 0, "data": []}` for the Sunday and 200
  overall; 404 only when every date is empty. Upstream failures
  (429/502) still fail the whole request — there is no partial-success
  for those, because the client could not tell which dates were served.
* **Cost is one call per date**, sequential; the limiter is checked per
  call, so a list can be cut off by a 429 part-way (documented).
* `symbol` is taken from any non-empty frame (all dates are the same
  ticker, so they resolve the same way).

## 5. Testing

```
pip install -r requirements-dev.txt
pytest                                            # 135 offline tests, ~15s
LIVE_YF=1 pytest tests/test_live_yfinance.py -v   # 27 opt-in tests against real Yahoo
```

**Caveat:** the sandbox this was built in has no outbound internet (DNS to
`query2.finance.yahoo.com` fails), so the live tests were written but could
not be executed here — run them locally. The offline suite patches
`yfinance.Ticker` at the `stock_router.yf.Ticker` seam with a fake whose
DataFrames replicate the real library's shape (tz-aware index named
`Date`/`Datetime`, `int64` Volume, `Dividends`/`Stock Splits` columns,
NaNs) and whose `.info` is a real-world subset with noise keys. Requests run
through the full FastAPI stack including JSON rendering.

### Bug found earlier: `/history` crashed with a 500 on real data

Any `NaN` in a yfinance frame (the `Capital Gains` column on ETFs,
partially-filled bars, gaps) made Starlette's JSON encoder raise
`ValueError: Out of range float values are not JSON compliant: nan`. The
original code had this bug; its broad `except Exception` could not catch it
because the failure happens during response rendering. The raw `/history`
endpoint is gone now, but the lesson is baked into every serialiser
(`_to_points`, `_compute_extremes`): NaN bars are skipped, never emitted,
and every endpoint test parses the body with a strict JSON parser that
rejects `NaN` tokens.

### Offline coverage (`tests/`)

`test_snapshot_endpoint.py` — exact 10-key shape from a realistic `.info`
with noise keys excluded; 2 budget slots per fetch, paid again on every
call (no cache); price
fallbacks (`regularMarketPrice` → `currentPrice` → `previousClose`); ETF
with null sector and `shortName` fallback; 404 for the near-empty dict
yfinance returns for unknown symbols; 429 when the budget can't cover both
halves of `.info`; removed routes return 404; OpenAPI lists exactly the
three `/stock` routes.

`test_historic_endpoint.py` — default `DAY`; case-insensitive period; 422
with zero upstream calls; DAY rolling-24h slicing/order/tz, `2d→5d` fallback
charged twice, stale-market fallback, NaN closes skipped; WEEK/MONTH exact
trading-day records vs the source frame; HALF_YEAR/YEAR fortnight records
(~13/~26, ~14 days apart, each a real trading-day close, latest record is
the latest close, all inside the window); FIVE_YEAR exactly one record per
calendar month equal to the month-end close; each daily period fetches
the smallest covering Yahoo range (`1mo/3mo/1y/2y/5y`); twelve requests =
twelve live calls (no cache); 404/429/502 mapping, including 429 with no
cached fallback.

`test_historic_reference_date.py` — `referenceDate`: keyed shape for a
single date, flat shape without; **multiple dates**: keyed in the order
given with each result equal to the single-date result, one upstream call
per date with its own window, five accepted, whitespace tolerated, DAY
with several dates, a closed day next to a trading day is `count: 0` not
an error, 404 only when all are empty, 429 part-way fails the whole
request, dotted symbol reported once; **list validation without upstream
call**: six dates, duplicates (also with whitespace), empty items
(`a,,b`, trailing/leading comma, empty, blank), one malformed / future /
beyond-retention item anywhere in the list, invalid item last after four
valid ones; **the yfinance call is made with
`start = referenceDate − (N−1)` and `end = referenceDate + 1` for every
period** (the one test that pins the inclusive-end contract at the
upstream boundary); WEEK/MONTH exact records vs the source frame, the
reference date itself included and the day before the window excluded;
HALF_YEAR/YEAR fortnights and FIVE_YEAR months confined to the window with
the window's last close as the last record; `referenceDate=today` equals
the rolling window; **DAY**: exactly that exchange-local date's bars, DST
day has 23/25 bars, closed day = 404 (no 24-bar fallback), NaN closes
skipped; bars outside the window returned by upstream are dropped; **no
caching**: three identical requests = three live calls, rolling and
anchored windows are independent calls; **422 without
upstream call** for future/too-old/malformed dates, `tomorrow` accepted
for exchanges ahead of UTC; 404/429/502 mapping; `BRK.B` → `BRK-B` retry.

`test_highlow_endpoint.py` — exact 6-key shape; High/Low selection with bar
timestamps; DAY session fallback; planted extremes 100/200/400 days back
resolve differently per window; each daily period = one call for its
covering range, six periods = six calls; NaN handling; 429s (no cached
fallback); route isolation.

`test_rate_limit.py` — `SlidingWindowRateLimiter` (cap, sliding window,
`retry_after`, zero-budget guard, thread-safety) with a fake clock.

`test_dotted_tickers.py` — dotted symbols through the API: exchange
suffixes direct and reported unchanged, `BRK.B`→`BRK-B` fallback on
historic / highlow / snapshot with budget accounting and `symbol`
reporting the dashed form, the fallback also firing when yfinance raises
instead of returning empty, 404 after both forms fail.

`test_live_yfinance.py` — opt-in: snapshot for AAPL/VTI/unknown, historic
for all six periods, one upstream call per request against real Yahoo,
highlow for all six periods; `referenceDate`:
a WEEK ending on a past weekday (last record == referenceDate, one live
call), past-date DAY sessions (6–8 hourly bars, all on
that date), 422 beyond hourly retention with no call, a MONTH ending
2015-03-31.

## 6. Other clean-ups made

* `requirements.txt` rewritten: it pinned exact versions of transitive
  dependencies (including a non-existent `certifi==2026.10.19`) so
  `pip install -r requirements.txt` failed in a clean environment. Now
  floor-pinned top-level dependencies only.
* App title/description in `main.py` updated from "Timezone API".
* `mainxx.py` (PyCharm scaffold) removed.

## 7. Suggested future work

* If/when a "watchlist snapshot" feature is needed: 9 of the 10 snapshot
  fields (everything except `sector`) come from Yahoo's multi-symbol
  `v7/finance/quote?symbols=...` endpoint (§2), which returns N symbols in
  a single request. A batch snapshot could fetch those in one call and
  only spend a `quoteSummary` call per symbol for `sector` (which is
  static and could be cached for hours) — a large quota saving versus
  `.info` per ticker. This would
  need to bypass `yfinance`'s per-ticker wrapper and call the documented
  query pattern directly (acceptable since it's the same endpoint yfinance
  itself uses, just with one parameter generalized).
* Design the caching layer (see the options table under "Reset" in §4)
  once the use cases are complete; decide at the same time on
  stale-on-exhausted-budget and negative caching of unknown symbols.
* For horizontal scaling (multiple app instances/workers), move the
  `SlidingWindowRateLimiter` (and the future cache) to a shared backend
  (e.g. Redis) so the 2000/hour budget is enforced globally, not
  per-process.
* Consider a scheduled background refresher for a known watchlist (e.g.
  every 5 minutes) so inbound API traffic is served entirely from a cache
  and decoupled from Yahoo call volume, which only then scales with
  `number_of_tracked_tickers`, not with client request rate.
