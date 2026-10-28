# Before / after — watchlists, users, clean-up (3.0.0 → 3.3.0)

Follows `SUMMARY-baseline-to-now.md` (the "before" here is that document's
"after": commit `f9d684d`, app version 3.0.0). **Every "after" sample
below is real output of the current code**, produced by driving the app
through FastAPI's `TestClient` with yfinance replaced by the test double
(`tests/conftest.py: FakeTicker`, `.info` for `AAPL` and `MSFT` only), and
every "before" is the same request against the baseline checked out from
git. Prices are the fixtures'; shapes, keys, status codes and messages are
exactly what each version emits.

## 1. In one paragraph

The service gained its first **stored state**: registered **users** and
their **watchlists** (named, ordered symbol lists), held in in-memory
stores behind small repository interfaces. A watchlist is created for a
registered user, viewed as one `/stock/{symbol}` snapshot per position,
updated by sending the new name and/or the complete ordered symbol list,
and deleted; a user's watchlists can be listed by id and name. Names are
unique per user and ids are short (`EHmDhwJ4`). Nothing about the stock
endpoints changed except that the snapshot builder became a reusable
function. Two orphaned modules (`caching.py`, `market_data.py`, 784
lines) that the project's own notes already described as deleted were
actually deleted, and the unrelated `GET /time` went with its `pytz`
dependency. Offline tests 145 → 238; statement coverage 77% → 93%.

## 2. At a glance

| | before (3.0.0) | after (3.3.0) |
|---|---|---|
| Routes | `/stock/{t}`, `/stock/{t}/historic`, `/status`, `/time` | `/stock/{t}`, `/stock/{t}/historic`, `/status`, **`POST /user`, `GET /user/{id}/watchlists`, `POST /watchlist`, `GET /watchlist/{id}`, `PUT /watchlist`, `DELETE /watchlist/{id}`** |
| Stored state | none | users + watchlists, in-memory (`stores.py`), process-local, empty after restart |
| Modules | `main`, `stock_router`, `rate_limit`, + orphaned `caching`, `market_data` | `main`, `stock_router`, `rate_limit`, **`user_store`, `user_router`, `watchlist_store`, `watchlist_router`, `stores`** |
| Python in repo (excl. tests) | 1 593 lines, 784 of them dead | 1 537 lines, none dead |
| Dependencies | fastapi, uvicorn, yfinance, pandas, pytz | same minus `pytz` |
| Offline tests | 145 | **238** (+56 watchlist, +24 user, +4 historic, 9 adjusted) |
| Statement coverage (`pytest --cov`) | 77 % (the two dead files at 0 %) | **93 %**; new modules 95–100 %, the only misses being abstract-interface stubs |
| App version | 3.0.0 | 3.3.0 |

## 3. The new endpoints

| route | what | upstream cost | errors |
|---|---|---|---|
| `POST /user` `{userId}` | register an id | 0 | 409 taken, 422 blank / whitespace / `/` / >100 chars |
| `GET /user/{userId}/watchlists` | `[{watchListId, watchListName}]`, oldest first | 0 | 404 unknown user |
| `POST /watchlist` `{userId, watchListName, symbols?}` | create → `{userId, watchListName, watchListId}` (201) | 0 | 422 unregistered user / bad input, 409 name taken |
| `GET /watchlist/{id}` | `{watchListId, userId, watchListName, count, symbols: {"1": snapshot, ...}}` | 2 per symbol | 404; 429 / 502 for the whole request |
| `PUT /watchlist` `{watchListId, watchListName?, symbols?}` | rename and/or replace the ordered list → the `GET` view | 2 per symbol | 404, 409, 422 (incl. neither field given) |
| `DELETE /watchlist/{id}` | `{watchListId, deleted: true}` | 0 | 404 |

Rules shared by all of them:

* **User identity** is trimmed and case-insensitive (`alice` / `Alice` /
  `ALICE ` are one user); responses echo the spelling used at
  registration.
* **Watchlist names are unique per user**, case-insensitively, on create
  and on rename — kept as a `(user, name) → id` index inside the store so
  no write path can bypass it.
* **Symbols** are normalised like `/stock/{symbol}` (trimmed,
  upper-cased), must be unique, at most 50 per list, and are **not**
  looked up on Yahoo when written. An unknown symbol shows in the view as
  `{"ticker", "error"}` instead of a snapshot.
* `watchListId` is 8 random alphanumerics (62⁸ ≈ 2·10¹⁴), collision-
  checked on allocation.
* Every 404 / 409 / 422 is raised **before any upstream call**.

## 4. The same requests, before and after

Before 3.3.0 every request below was `404 {"detail": "Not Found"}` — the
routes did not exist — so only the "after" is shown, in the order a client
would use them. The budget readout at the end is the proof of the cost
table above.

### register a user
`POST /user`
```json
{"userId": "alice"}
```
→ **201**
```json
{"userId": "alice"}
```

### register the same user again
`POST /user`
```json
{"userId": "Alice"}
```
→ **409**
```json
{"detail": "User Alice already exists."}
```

### create a watchlist for an unknown user
`POST /watchlist`
```json
{"userId": "bob", "watchListName": "Tech", "symbols": ["AAPL"]}
```
→ **422**
```json
{"detail": "userId 'bob' does not exist; register it with POST /user first."}
```

### create a watchlist for an unknown user
`POST /watchlist`
```json
{"userId": "bob", "watchListName": "Tech", "symbols": ["AAPL"]}
```
→ **422**
```json
{"detail": "userId 'bob' does not exist; register it with POST /user first."}
```

### duplicate name for the same user
`POST /watchlist`
```json
{"userId": "alice", "watchListName": "tech", "symbols": []}
```
→ **409**
```json
{"detail": "User alice already has a watchlist named 'tech'."}
```

### list a user's watchlists
`GET /user/alice/watchlists`
→ **200**
```json
{
  "userId": "alice",
  "count": 2,
  "watchlists": [
    {"watchListId": "EHmDhwJ4", "watchListName": "Tech"},
    {"watchListId": "iN1vVauz", "watchListName": "ETFs"}
  ]
}
```

### view a watchlist
`GET /watchlist/EHmDhwJ4`
→ **200**
```json
{
  "watchListId": "EHmDhwJ4",
  "userId": "alice",
  "watchListName": "Tech",
  "count": 3,
  "symbols": {
    "1": {"ticker": "AAPL", "sector": "Technology", "fulldayPrice": 347.725, "currency": "USD", "symbol": "AAPL", "language": "en-US", "longName": "Apple Inc.", "exchange": "NMS", "market": "us_market", "displayName": "Apple"},
    "2": {"ticker": "MSFT", "sector": "Technology", "fulldayPrice": 410.5, "currency": "USD", "symbol": "MSFT", "language": "en-US", "longName": "Microsoft Corporation", "exchange": "NMS", "market": "us_market", "displayName": "Microsoft"},
    "3": {"ticker": "NOPE", "error": "Ticker NOPE not found."}
  }
}
```

### rename only
`PUT /watchlist`
```json
{"watchListId": "EHmDhwJ4", "watchListName": "Tech & more"}
```
→ **200**
```json
{
  "watchListId": "EHmDhwJ4",
  "userId": "alice",
  "watchListName": "Tech & more",
  "count": 3,
  "symbols": {
    "1": {"ticker": "AAPL", "sector": "Technology", "fulldayPrice": 347.725, "currency": "USD", "symbol": "AAPL", "language": "en-US", "longName": "Apple Inc.", "exchange": "NMS", "market": "us_market", "displayName": "Apple"},
    "2": {"ticker": "MSFT", "sector": "Technology", "fulldayPrice": 410.5, "currency": "USD", "symbol": "MSFT", "language": "en-US", "longName": "Microsoft Corporation", "exchange": "NMS", "market": "us_market", "displayName": "Microsoft"},
    "3": {"ticker": "NOPE", "error": "Ticker NOPE not found."}
  }
}
```

### symbols only: drop NOPE, re-order
`PUT /watchlist`
```json
{
  "watchListId": "EHmDhwJ4",
  "symbols": {
    "1": "MSFT",
    "2": "AAPL"
  }
}
```
→ **200**
```json
{
  "watchListId": "EHmDhwJ4",
  "userId": "alice",
  "watchListName": "Tech & more",
  "count": 2,
  "symbols": {
    "1": {"ticker": "MSFT", "sector": "Technology", "fulldayPrice": 410.5, "currency": "USD", "symbol": "MSFT", "language": "en-US", "longName": "Microsoft Corporation", "exchange": "NMS", "market": "us_market", "displayName": "Microsoft"},
    "2": {"ticker": "AAPL", "sector": "Technology", "fulldayPrice": 347.725, "currency": "USD", "symbol": "AAPL", "language": "en-US", "longName": "Apple Inc.", "exchange": "NMS", "market": "us_market", "displayName": "Apple"}
  }
}
```

### nothing to update
`PUT /watchlist`
```json
{"watchListId": "EHmDhwJ4"}
```
→ **422**
```json
{"detail": [{"loc": ["body"], "msg": "Value error, nothing to update: provide watchListName and/or symbols"}, "..."]}
```

### delete
`DELETE /watchlist/EHmDhwJ4`
→ **200**
```json
{"watchListId": "EHmDhwJ4", "deleted": true}
```

### view after delete
`GET /watchlist/EHmDhwJ4`
→ **404**
```json
{"detail": "Watchlist EHmDhwJ4 not found."}
```

### `GET /time`

| before | after |
|---|---|
| `200 {"current_times": {"UTC": "2026-10-28 16:49:02 UTC+0000", "America/New_York": ..., 8 more}}` | `404 {"detail": "Not Found"}` |

### Upstream budget over the flow above

`GET /status` → `"upstream_budget": {"per_hour": 1800, "remaining": 1784}`:
16 calls = the view (3 symbols × 2) + the rename `PUT` (3 × 2, it returns
the view) + the re-order `PUT` (2 × 2). `POST`/`DELETE /watchlist` and
both `/user` routes cost nothing.

## 5. Code changes

| file | change |
|---|---|
| `watchlist_store.py` (new, 234 lines) | `WatchlistStore` interface + `InMemoryWatchlistStore`: dict + lock, frozen `Watchlist` records (callers can't mutate stored state), `(user, name)` uniqueness index maintained by create / update / delete, `update(name=None, symbols=None)` = keep, `new_short_id()`, injectable id/clock for tests. |
| `watchlist_router.py` (new, 282 lines) | The four routes. Pydantic models own request-shape rules (trim / upper-case, ≤ 50 symbols, positive positions, `{position: symbol}` or list, "nothing to update"); the view is built from `stock_router.fetch_snapshot`. |
| `user_store.py` (new, 98 lines) | `UserStore` interface + `InMemoryUserStore`: a user is a registered id; `user_key()` defines identity. |
| `user_router.py` (new, 101 lines) | `POST /user`, `GET /user/{id}/watchlists`; `clean_user_id()` is also what `POST /watchlist` validates with. |
| `stores.py` (new, 13 lines) | The two singletons, so both routers share them without importing each other. |
| `stock_router.py` | The body of `GET /stock/{ticker}` became `fetch_snapshot(ticker) -> Optional[dict]` (None = unknown symbol; 429/502 raised as before). The route calls it and maps None to 404. Same budget accounting and `BRK.B → BRK-B` retry for both consumers; behaviour of `/stock` unchanged (its 145 tests untouched). |
| `main.py` | Mounts the two routers; `/time`, `MAJOR_TIMEZONES` and the `pytz` import removed; version 3.3.0. |
| `requirements.txt` | `pytz` removed (only `/time` used it; pandas handles exchange time zones). |
| `caching.py` (−291), `market_data.py` (−493) | **Deleted.** Both were orphans: nothing imported them (0 % coverage under the full suite) and `EVALUATION.md` / `history-api-changes.md` already recorded them as removed. `caching.py` was the two-tier memory + SQLite cache that `stock_router` used until the "Reset" round; `market_data.py` an earlier parallel draft of the same layer (own cache, budget, loaders, the old `/hourly` builder) that also set `yf.config.debug.hide_exceptions = False` at import — a process-wide side effect that would have switched yfinance to raising on unknown symbols for anyone who imported it. |
| `tests/conftest.py` | Autouse fixture resets both stores per test and pre-registers `u1` / `u2`. |
| `tests/test_watchlist.py` (new, 618 lines, 59 tests) | CRUD flows; every 404 / 409 / 422 path asserted to make no upstream call; budget accounting (2 per symbol, 4 with the dash retry); unknown-symbol entries; 429 mid-list; 502; rename-only / symbols-only / both / neither; uniqueness across user-id spellings, rename and delete; store unit tests incl. collision retry and a thread-safety check. |
| `tests/test_users.py` (new, 150 lines, 24 tests) | Registration, 409 across spellings, id validation, listing (empty, order, other users excluded, follows rename/delete, case-insensitive path), 404, store unit tests. |
| `tests/test_snapshot_endpoint.py` | `/time` assertion → asserts it is gone. |
| `tests/test_live_yfinance.py` | `test_live_aggregated_records_contain_the_daily_extremes` failed whenever it ran during US market hours: it compared a **rolling** `YEAR` (one live call) with an anchored `MONTH` (a second live call), and Yahoo's bar for the running session changes between the two (reproduced offline by advancing the synthetic market between the requests: `high`/`close` mismatch). The aggregation itself was right in every stable state. Now both windows are anchored on the same past weekday, the bin is derived from the documented rule (14 calendar days ending on D′) instead of the previous record's stamp, `null` highs/lows are ignored, and prices are compared to the cent because the two windows come from two different range queries. |
| `tests/test_historic_endpoint.py` | Offline twin of that live test (+4, parametrised over a Friday, a Monday, a holiday and the day after), so the invariant is checked on every run, not only with `LIVE_YF=1`. |
| `README.md` | New sections 3 (Users) and 4 (Watchlists), budget table rows, layout, test counts. |

## 6. Decisions the outline left open

| question | decision | why |
|---|---|---|
| validate symbols on Yahoo at create/update? | no — syntactic checks only; unknown symbols become `{"ticker", "error"}` entries in the view | a lookup costs 2 of the 1 800 hourly calls per symbol on every save |
| partial failure in the view | unknown symbol → per-entry error; budget exhausted / upstream broken → 429 / 502 for the whole request | the rule multi-date `/historic` already follows (`null` entry vs. whole-request 429) |
| `PUT` semantics | name and/or the complete ordered symbol list; omitted = keep; neither → 422 | rename alone, symbols alone (add / remove / sort = send the whole list in order), or both, with one endpoint |
| `PUT symbols` shape | `{position: symbol}` as outlined (ordered by position, renumbered 1..N) or a plain list | the map is the contract; the list is a convenience |
| unregistered `userId` on `POST /watchlist` | 422, nothing stored | the problem is a body field, not the URL — a 404 on `POST /watchlist` would read as "no such route" |
| user identity | trimmed + case-insensitive; canonical spelling echoed | `u1` / `U1` typed by hand are the same person; the watchlist-name rule needs one answer to "same user?" |
| id format | 8 random alphanumerics instead of a 36-char UUID | readable and pasteable; ample for a per-process store with a collision check |
| response extras | `GET`/`PUT` also carry `userId` and `count`; `DELETE` returns `{"watchListId", "deleted": true}` | additive; `count` matches `/historic` |
| limits | 50 symbols per list, names / user ids ≤ 100 chars, symbols ≤ 20 | a view of 50 symbols = 100 upstream calls |
| state commits before the `PUT` view is fetched | yes | the client's intent succeeded; only the live view failed (429/502); `GET` shows the new state |

## 7. Not done (candidates for the next round)

* `DELETE /user/{id}` and a `GET /user/{id}` profile — and what happens to
  a deleted user's watchlists.
* No authentication: a registered `userId` is trusted as sent, and
  `PUT` / `DELETE /watchlist` don't check ownership.
* Batch snapshots: the watchlist view is the "watchlist snapshot" use case
  EVALUATION.md §7 anticipated — Yahoo's multi-symbol `quote` endpoint
  would turn 2 × N calls into ~1 + N (or 1 with `sector` cached). Today a
  dashboard polling a 50-symbol list hits the 1 800/h limiter after 18
  views.
* Persistence across restarts / multiple workers: swap the in-memory
  stores for database-backed implementations of `UserStore` /
  `WatchlistStore` (the interfaces exist; the routers don't know the
  difference).
