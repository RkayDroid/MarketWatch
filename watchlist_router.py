"""
Watchlist endpoints: a named, ordered list of symbols owned by a user.

    POST   /watchlist          create            {userId, watchListName, symbols: [..]}
    GET    /watchlist/{id}     view              -> name + one /stock/{symbol} snapshot per position
    PUT    /watchlist          replace           {watchListId, watchListName, symbols: {1: "AAPL", 2: "MSFT"}}
    DELETE /watchlist/{id}     delete

`PUT` replaces the name and the whole ordered symbol list, so adding,
removing and re-ordering symbols and renaming are all "send the state you
want" - no per-operation endpoints to keep consistent with each other.

A watchlist belongs to a registered user (`POST /user`): creating one for
an unknown userId is a 422, and the stored/echoed userId is the spelling
the user was registered with.

Symbols are validated syntactically only (upper-cased, no whitespace,
unique, at most MAX_SYMBOLS): checking each one against Yahoo would cost
2 upstream calls per symbol on every write. Unknown symbols therefore
show up in `GET` as an `{"ticker", "error"}` entry rather than a snapshot,
and the whole request fails only when the upstream budget runs out (429)
or Yahoo misbehaves (502) - the same rule as multi-date `/historic`.

Upstream cost of `GET`/`PUT`: 2 calls per symbol (4 for a dotted symbol
that needs the dash retry); `POST` and `DELETE` make no upstream calls.
The store is in-memory (watchlist_store.py): process-local and empty
after a restart.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field, field_validator, model_validator

from stock_router import fetch_snapshot
from stores import users, watchlists as store
from user_router import clean_user_id
from user_store import UserNotFound
from watchlist_store import DuplicateWatchlistName, Watchlist, WatchlistNotFound

router = APIRouter(prefix="/watchlist", tags=["Watchlist"])

# Every symbol costs 2 upstream calls per GET, out of 1800/hour, so a
# watchlist is capped. 50 symbols = 100 calls per view, ~18 views/hour.
MAX_SYMBOLS = 50
MAX_NAME_LENGTH = 100
MAX_SYMBOL_LENGTH = 20


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
def _clean_symbols(symbols: List[str]) -> List[str]:
    """Upper-case, strip and check a symbol list the way /stock/{symbol}
    would see each item; duplicates (after normalising) are rejected."""
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"a watchlist holds at most {MAX_SYMBOLS} symbols, got {len(symbols)}")
    cleaned: List[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol:
            raise ValueError("symbols must not be empty")
        if any(ch.isspace() for ch in symbol) or "/" in symbol:
            raise ValueError(f"symbol '{raw}' must not contain whitespace or '/'")
        if len(symbol) > MAX_SYMBOL_LENGTH:
            raise ValueError(f"symbol '{raw}' is longer than {MAX_SYMBOL_LENGTH} characters")
        if symbol in cleaned:
            raise ValueError(f"symbol '{symbol}' appears more than once")
        cleaned.append(symbol)
    return cleaned


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("watchListName must not be blank")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"watchListName is longer than {MAX_NAME_LENGTH} characters")
    return name


class WatchlistCreate(BaseModel):
    userId: str = Field(..., description="Owner of the watchlist; must be registered with POST /user.")
    watchListName: str = Field(..., description="Unique per user (case-insensitive).")
    symbols: List[str] = Field(default_factory=list, description="Ordered symbols, e.g. [\"AAPL\", \"SAP.DE\"].")

    @field_validator("userId")
    @classmethod
    def _user_id(cls, value: str) -> str:
        return clean_user_id(value)

    @field_validator("watchListName")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: List[str]) -> List[str]:
        return _clean_symbols(value)


class WatchlistUpdate(BaseModel):
    """New name and/or complete ordered symbol list; an omitted field keeps
    its current value. At least one of the two must be given."""

    watchListId: str = Field(..., min_length=1)
    watchListName: Optional[str] = Field(None, description="New name; omit to keep the current one.")
    symbols: Optional[Union[Dict[int, str], List[str]]] = Field(
        None,
        description=(
            "The complete symbol list in the wanted order, as {position: symbol} "
            "(ordered by position, any positive integers) or as a plain list. Omit to keep the current list."
        ),
    )

    @field_validator("watchListName")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _clean_name(value)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: Optional[Union[Dict[int, str], List[str]]]) -> Optional[List[str]]:
        if value is None:
            return None
        if isinstance(value, dict):
            if any(position < 1 for position in value):
                raise ValueError("symbol positions must be positive integers")
            value = [value[position] for position in sorted(value)]
        return _clean_symbols(value)

    @model_validator(mode="after")
    def _something_to_update(self):
        if self.watchListName is None and self.symbols is None:
            raise ValueError("nothing to update: provide watchListName and/or symbols")
        return self


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
def _summary(item: Watchlist) -> dict:
    return {"userId": item.user_id, "watchListName": item.name, "watchListId": item.id}


def _view(item: Watchlist) -> dict:
    """The watchlist with one /stock/{symbol} snapshot per position
    (1-based). An unknown symbol becomes {"ticker", "error"}; 429/502 from
    any symbol fail the whole request."""
    symbols = {}
    for position, symbol in enumerate(item.symbols, start=1):
        snapshot = fetch_snapshot(symbol)
        if snapshot is None:
            snapshot = {"ticker": symbol, "error": f"Ticker {symbol} not found."}
        symbols[str(position)] = snapshot
    return {
        "watchListId": item.id,
        "userId": item.user_id,
        "watchListName": item.name,
        "count": len(item.symbols),
        "symbols": symbols,
    }


def _load(watchlist_id: str) -> Watchlist:
    try:
        return store.get(watchlist_id)
    except WatchlistNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


_ID_PATH = Path(..., description="The watchListId returned by POST /watchlist.")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.post("", status_code=201)
def create_watchlist(body: WatchlistCreate):
    """Create a watchlist for a registered user. No upstream calls:
    symbols are normalised and checked for shape only, not looked up on
    Yahoo.

    Request::

        {"userId": "alice", "watchListName": "Tech", "symbols": ["aapl", "MSFT"]}

    Response (201)::

        {"userId": "alice", "watchListName": "Tech", "watchListId": "k3Zp9QaB"}

    * `userId` must exist (`POST /user`), else 422 and nothing is stored;
      it is matched case-insensitively and echoed as registered.
    * `watchListId` is a short random id (8 alphanumerics, case-sensitive).
    * 409 if the user already has a watchlist with that name - names are
      unique per user, compared case-insensitively (`Tech`/`tech`); 422
      for a blank name/userId, more than 50 symbols, an empty or
      duplicate symbol.
    """
    try:
        user = users.get(body.userId)
    except UserNotFound as exc:
        raise HTTPException(
            status_code=422, detail=f"userId '{body.userId}' does not exist; register it with POST /user first."
        ) from exc
    try:
        item = store.create(user.id, body.watchListName, body.symbols)
    except DuplicateWatchlistName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(item)


@router.get("/{watchlist_id}")
def get_watchlist(watchlist_id: str = _ID_PATH):
    """The watchlist with a live `/stock/{symbol}` snapshot per symbol,
    keyed by position (1-based, in the list's order).

    Response::

        {
          "watchListId": "k3Zp9QaB", "userId": "u1", "watchListName": "Tech", "count": 2,
          "symbols": {
            "1": {"ticker": "AAPL", "sector": "Technology", "fulldayPrice": 347.725, ...},
            "2": {"ticker": "NOPE", "error": "Ticker NOPE not found."}
          }
        }

    * Each entry is exactly the `/stock/{symbol}` response; a symbol Yahoo
      doesn't know is an `{"ticker", "error"}` entry instead.
    * 404 for an unknown watchListId (no upstream call). 429 if the hourly
      upstream budget runs out part-way, 502 on any other upstream failure
      - the whole request, as for multi-date `/historic`.
    * Upstream cost: 2 calls per symbol (4 with a dash retry).
    """
    return _view(_load(watchlist_id))


@router.put("")
def update_watchlist(body: WatchlistUpdate):
    """Rename a watchlist and/or replace its entire ordered symbol list -
    this is how symbols are added, removed and re-ordered and how the list
    is renamed. A field that is left out keeps its current value.

    Requests::

        {"watchListId": "k3Zp9QaB", "watchListName": "Tech & ETFs"}                      rename only
        {"watchListId": "k3Zp9QaB", "symbols": {"1": "MSFT", "2": "AAPL"}}               symbols only
        {"watchListId": "k3Zp9QaB", "watchListName": "Tech", "symbols": ["MSFT", "AAPL"]} both

    `symbols` is `{position: symbol}` (taken in position order; positions
    need not be contiguous, the response renumbers them 1..N) or a plain
    list. An empty object/list empties the watchlist; omitting the field
    keeps it.

    Response: the same as `GET /watchlist/{id}`, i.e. the new state with
    live snapshots (2 upstream calls per symbol).

    * 404 for an unknown watchListId, 409 if the new name collides with
      another watchlist of the same user, 422 for invalid input or when
      neither field is given - all before any upstream call.
    """
    try:
        item = store.update(body.watchListId, body.watchListName, body.symbols)
    except WatchlistNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DuplicateWatchlistName as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(item)


@router.delete("/{watchlist_id}")
def delete_watchlist(watchlist_id: str = _ID_PATH):
    """Delete a watchlist. 200 with `{"watchListId": ..., "deleted": true}`;
    404 if there is no such watchlist. No upstream calls."""
    try:
        item = store.delete(watchlist_id)
    except WatchlistNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"watchListId": item.id, "deleted": True}
