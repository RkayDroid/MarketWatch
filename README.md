# Market Watch Lite

A small FastAPI service exposing stock data from Yahoo Finance (via
[yfinance](https://github.com/ranaroussi/yfinance)), plus **registered
users** and their **watchlists** (named, ordered lists of symbols), both
kept in in-memory stores. Every stock request is served by a live yfinance call — **there is no caching in this version**
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

### 2. `GET /stock/{symbol}/historic?period=DAY[&referenceDate=…]` — OHLC price series

Default `period=DAY`. The window is defined by one rule:

> **D′** = the last trading day on or before `referenceDate` (default:
> today, in the exchange's time zone).

| period | window ending on D′ | bars fetched | one record per | `datetime` format | reach |
|---|---|---|---|---|---|
| `DAY` | the session on D′ | hourly | hour (Yahoo's hourly bars start at the open: 09:30, 10:30 … 15:30) | ISO-8601 with exchange UTC offset | ~730 days (Yahoo's hourly retention) |
| `WEEK` | the **5 sessions** ending on D′ | daily | trading day | `YYYY-MM-DD` | decades |
| `MONTH` | the **30 calendar days** ending on D′ | daily | trading day | `YYYY-MM-DD` | decades |
| `HALF_YEAR` | 182 calendar days ending on D′ | daily | fortnight (14-day bin) | `YYYY-MM-DD` | decades |
| `YEAR` | 365 calendar days ending on D′ | daily | fortnight (14-day bin) | `YYYY-MM-DD` | decades |
| `FIVE_YEAR` | 5 years (1827 days) ending on D′ | daily | calendar month | `YYYY-MM-DD` | decades |

That is what "1D / 5D" (sessions) and "1M / 6M / 1Y / 5Y" (calendar time
ending on the last session) mean on any chart.

`GET /stock/AAPL/historic?period=DAY` (Wednesday, 14:00 New York)

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
    {"datetime": "2026-10-21T09:30:00-04:00", "open": 221.54, "high": 221.56, "low": 221.54, "close": 221.55},
    {"datetime": "2026-10-21T10:30:00-04:00", "open": 221.55, "high": 221.56, "low": 221.54, "close": 221.55},
    "...",
    {"datetime": "2026-10-21T13:30:00-04:00", "open": 221.55, "high": 221.56, "low": 221.54, "close": 221.55}
  ]
}
```

* **Each record is one bar's Open / High / Low / Close** (`high`/`low` are
  the true traded extremes of the bar, not closes). The window's high and
  low are therefore `max(high)` and `min(low)` over `data`, and the record
  they sit in says when — to the hour for `DAY`, to the day otherwise.
  Fortnight/month records are **aggregates of their bin**: first open,
  highest high, lowest low, last close, stamped with the bin's last
  trading day; bins are anchored at the newest bar, so the final record
  always ends on the window's last close. `DAY` includes the running
  hourly bar while the market is open. A bar without a close is skipped;
  a missing open/high/low is `null`.
* `interval` is the record granularity: `1h`, `1d`, `2wk` or `1mo`.
* **A weekend or holiday never comes back empty**: `referenceDate=2026-10-18`
  (a Sunday) is served as the window ending Friday 2026‑10‑16, and the
  response says so. Without `referenceDate`, "today" follows the same rule:
  during the session the window ends on today's partial session; before
  the open, or on a weekend, on the previous session.
* Every response carries **`referenceDate`** (the date asked for, or today)
  and **`window: {start, end}`** — exchange-local dates, both inclusive.
  `end` is D′. `start` is the first session of a `DAY`/`WEEK` window, and
  `end − 29 / 181 / 364 / 1826 days` for the calendar periods whether or
  not that day traded.
* `ticker` echoes the request; `symbol` is the symbol Yahoo actually
  answered for. They differ only for US share classes typed with a dot
  (`/stock/BRK.B/historic` → `"ticker": "BRK.B", "symbol": "BRK-B"`).

#### `referenceDate` — end the window on given date(s)

One `YYYY-MM-DD`, or up to **5** unique dates separated by commas
(whitespace tolerated). The response is then keyed by the dates **as
given**, in the order given, under `results`:

`GET /stock/AAPL/historic?period=WEEK&referenceDate=2026-10-18` (a Sunday;
Wednesday 2026‑10‑14 was a holiday, so the 5 sessions span 8 days)

```json
{
  "ticker": "AAPL",
  "symbol": "AAPL",
  "period": "WEEK",
  "interval": "1d",
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

* A date on or before which nothing ever traded (before the listing) is
  `null`; the request is `404` only if **every** date is null.
* Rejected with `422`, **before any upstream call** (the whole list is
  validated first, the message names the item): more than 5 dates,
  duplicates, empty items (`a,,b`, trailing comma), malformed or
  impossible dates (`2026-02-30`), dates in the future, and `DAY` windows
  older than Yahoo's ~730-day hourly retention. A date one day ahead of
  UTC is accepted so exchanges east of UTC can ask for their current local
  date.
* **Upstream cost: 1 request per window** (one per date), a `start/end`
  call covering the window plus a 14‑day look-back from which D′ is
  resolved. If the hourly budget runs out part-way through a list the
  whole request is `429`.

---

### 3. Users — `POST /user`, `GET /user/{userId}/watchlists`

The lightest possible user management: a user is a **registered id**, no
authentication or profile. It exists so a watchlist can only be created
for a user the service knows, and so a user's watchlists can be listed.
Ids are trimmed and matched **case-insensitively** (`alice` / `Alice` are
one user); responses echo the spelling used at registration. Neither
route makes upstream calls.

#### `POST /user` → `201`

```json
{"userId": "alice"}
```
```json
{"userId": "alice"}
```

`409` if already registered; `422` for a blank id, one containing
whitespace or `/`, or longer than 100 characters.

#### `GET /user/{userId}/watchlists`

Ids and names only, oldest first — use `GET /watchlist/{id}` for the live
view. `404` for an unregistered user.

```json
{
  "userId": "alice",
  "count": 2,
  "watchlists": [
    {"watchListId": "k3Zp9QaB", "watchListName": "Tech"},
    {"watchListId": "Qw8rT2vM", "watchListName": "ETFs"}
  ]
}
```

---

### 4. Watchlists — `POST` / `GET` / `PUT` / `DELETE /watchlist`

A watchlist is a **named, ordered list of symbols owned by a registered
user** (`POST /user` first; an unknown `userId` is `422` and nothing is
stored). It lives in an in-memory store (`watchlist_store.py`): process-local, empty
after a restart — the project's first piece of stored state, put behind a
small `WatchlistStore` interface so a database can replace it later.

| route | what | upstream cost |
|---|---|---|
| `POST /watchlist` | create | 0 |
| `GET /watchlist/{id}` | the list with a live `/stock/{symbol}` snapshot per position | 2 per symbol |
| `PUT /watchlist` | rename and/or **replace** the ordered symbols (add / remove / re-order) | 2 per symbol |
| `DELETE /watchlist/{id}` | delete | 0 |

Rules, applied the same way everywhere:

* symbols are normalised like `/stock/{symbol}` (trimmed, upper-cased),
  must be unique within a list, and a list holds at most **50** symbols
  (`MAX_SYMBOLS`; a `GET` costs 2 upstream calls per symbol);
* symbols are **not** looked up on Yahoo when written (that would spend
  2 calls per symbol of the hourly budget on every save); an unknown one
  shows up in `GET` as an `{"ticker", "error"}` entry instead of a snapshot;
* **a user cannot have two watchlists with the same name** → `409`. The
  name is compared case-insensitively and trimmed (`Tech`/`tech`), on
  create and on rename, and `userId` is matched the way `/user` defines
  it; the rule is an index in the store, so no write path can bypass it. An unknown
  `watchListId` → `404`; invalid input → `422` — all before any upstream
  call;
* `watchListId` is a **short random id** (8 alphanumerics, case-sensitive,
  e.g. `k3Zp9QaB`) — easy to read and paste into a URL; collisions are
  checked on allocation;
* `GET`/`PUT` answer `429` if the hourly budget runs out part-way and
  `502` on any other upstream failure — for the whole request, as
  multi-date `/historic` does.

#### `POST /watchlist` → `201`

```json
{"userId": "alice", "watchListName": "Tech", "symbols": ["aapl", "MSFT", "SAP.DE"]}
```
```json
{"userId": "alice", "watchListName": "Tech", "watchListId": "k3Zp9QaB"}
```

`symbols` is optional (default `[]`). `userId` must be registered and is
echoed as registered (`"ALICE"` in the request → `"alice"` in the
response).

#### `GET /watchlist/{id}`

`symbols` is keyed by **position** (1-based, in the list's order); every
value is exactly the `/stock/{symbol}` response:

```json
{
  "watchListId": "k3Zp9QaB",
  "userId": "alice",
  "watchListName": "Tech",
  "count": 3,
  "symbols": {
    "1": {"ticker": "AAPL", "sector": "Technology", "fulldayPrice": 347.725, "currency": "USD", "symbol": "AAPL",
          "language": "en-US", "longName": "Apple Inc.", "exchange": "NMS", "market": "us_market", "displayName": "Apple"},
    "2": {"ticker": "MSFT", "...": "..."},
    "3": {"ticker": "NOPE", "error": "Ticker NOPE not found."}
  }
}
```

#### `PUT /watchlist` → the `GET` view of the new state

Send the new name and/or the **complete** symbol list in the wanted order;
a field that is left out keeps its current value (at least one must be
given, else `422`). `symbols` is `{position: symbol}` — taken in position
order, positions need not be contiguous, the response renumbers them
1..N — or a plain list. `{}` / `[]` empties the list.

```json
{"watchListId": "k3Zp9QaB", "watchListName": "Tech & ETFs"}                                      rename only
{"watchListId": "k3Zp9QaB", "symbols": {"1": "MSFT", "2": "AAPL", "3": "VTI"}}                   symbols only
{"watchListId": "k3Zp9QaB", "watchListName": "Tech & ETFs", "symbols": ["MSFT", "AAPL", "VTI"]}  both
```

#### `DELETE /watchlist/{id}` → `200`

```json
{"watchListId": "k3Zp9QaB", "deleted": true}
```

---

### Utility

* `GET /status` — liveness, plus how much of the hourly upstream budget
  is left: `{"status": "online", ..., "upstream_budget": {"per_hour": 1800, "remaining": 1793}}`.

## Upstream request budget

Yahoo's chart endpoint takes **one symbol per request** and retains intraday
bars for a limited time only (~60 days for sub-hourly bars, 730 days for
hourly), so there is no way to batch symbols or to get long windows at
intraday resolution. What each API call costs:

| API call | Yahoo call(s) |
|---|---|
| `/stock/{symbol}` | `quoteSummary` + `quote` (= yfinance `.info`) → **2** |
| `/historic` | `start/end @ 60m` (DAY) or `@ 1d` → **1 per window** (one per `referenceDate`, ≤ 5) |
| `GET`/`PUT /watchlist` | one `/stock/{symbol}` snapshot per symbol → **2 per symbol** (≤ 50) |
| `POST`/`DELETE /watchlist`, `/user` routes | **0** |

Every window is one `start/end` call: the period's nominal span plus a
14-day look-back (so D′ can be resolved from the bars that come back),
clamped to Yahoo's retention for the bar size. Dotted symbols that need
the dash retry pay once more. yfinance itself adds a one-off, uncounted
timezone-lookup request the first time a process sees a symbol.

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
  call). This applies to both endpoints - snapshot and historic go
  through the same retry - and fires whether yfinance signals "unknown symbol" with an empty frame (its
  default) or by raising (`yf.config.debug.hide_exceptions = False`). The
  `ticker` field echoes the symbol the client asked for; the `symbol` field
  shows Yahoo's canonical (dashed) form so the translation is visible.

## Tests

```
pip install -r requirements-dev.txt
pytest                                            # 238 offline tests, ~10s
LIVE_YF=1 pytest tests/test_live_yfinance.py -v   # 24 opt-in tests against real Yahoo
```

The offline suite patches `yfinance.Ticker` with a fake backed by a
synthetic exchange (`tests/conftest.py: FakeMarket`): weekday sessions
09:30–16:00 minus a holiday and a 9-day closure, with daily and hourly
frames that are true aggregations of one price path (so the two bar
sizes agree exactly, including the partial running session), shaped like
the real library's frames (tz-aware `Date`/`Datetime` index, `int64`
Volume, `Dividends`/`Stock Splits` columns). `test_window_rule.py` covers
the window rule, `test_historic_endpoint.py` the records (OHLC per bar,
bin aggregation, NaN handling), `test_watchlist.py` the watchlist CRUD
(validation, snapshots per position, budget accounting, 404/409/422/429/502)
and its store, `test_users.py` user registration, the per-user listing
and the user store. The live tests need network access and a
handful of real requests.

## Layout

```
main.py            FastAPI app (+ /status)
stock_router.py    the /stock endpoints and all yfinance access
user_router.py     POST /user, GET /user/{id}/watchlists
user_store.py      UserStore interface + InMemoryUserStore (stdlib only)
watchlist_router.py  the /watchlist endpoints (CRUD; GET/PUT embed /stock snapshots)
watchlist_store.py   WatchlistStore interface + InMemoryWatchlistStore (stdlib only)
stores.py          the two in-memory store singletons shared by the routers
rate_limit.py      SlidingWindowRateLimiter (stdlib only)
tests/             offline suite + opt-in live tests
EVALUATION.md      original project evaluation, yfinance option analysis, change history
prior-changes/     before/after notes per round; start with SUMMARY-baseline-to-now.md,
                   then watchlist-users-changes.md (users, watchlists, dead code removed)
```
