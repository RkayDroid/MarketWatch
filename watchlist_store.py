"""
In-memory persistence for watchlists.

This is the service's first (and so far only) piece of stored state. It is
deliberately a plain dictionary behind a lock:

* process-local and volatile - a restart empties it, and with several
  workers each process has its own copy (the same caveat as the rate
  limiter; back it with a shared store when that matters);
* the store hands out *copies* of its records, so callers can't mutate
  stored state by accident, and every write goes through a method that
  validates the invariants in one place;
* ``WatchlistStore`` is the interface the API depends on, so a database-
  backed implementation can replace ``InMemoryWatchlistStore`` without
  touching the router.

Invariants enforced here (the router validates request *shape*; the store
validates *state*):

* a watchlist belongs to one user and has a non-empty name;
* a user cannot have two watchlists with the same name. Both the user id
  and the name are compared case-insensitively (``u1`` / ``U1``, ``Tech``
  / ``tech``), and the rule is kept as an index ``(user, name) -> id``
  that every write path maintains, not as a scan a new code path could
  forget;
* symbols are stored in order and are unique within a watchlist.

Ids are short random strings (``ID_LENGTH`` alphanumerics, e.g.
``k3Zp9QaB``) rather than UUIDs: 62**8 ~ 2e14 values is plenty for a
per-process store, collisions are checked under the lock anyway, and they
are far easier to read, type and paste into a URL.
"""

from __future__ import annotations

import secrets
import string
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ID_LENGTH = 8
_ID_ALPHABET = string.ascii_letters + string.digits


def new_short_id(length: int = ID_LENGTH) -> str:
    """A random, URL-safe, case-sensitive alphanumeric id."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


class WatchlistNotFound(LookupError):
    """No watchlist with the given id."""

    def __init__(self, watchlist_id: str):
        self.watchlist_id = watchlist_id
        super().__init__(f"Watchlist {watchlist_id} not found.")


class DuplicateWatchlistName(ValueError):
    """The user already has a watchlist with this name."""

    def __init__(self, user_id: str, name: str):
        self.user_id = user_id
        self.name = name
        super().__init__(f"User {user_id} already has a watchlist named '{name}'.")


@dataclass(frozen=True)
class Watchlist:
    """One stored watchlist. `symbols` is ordered: position 1 is
    `symbols[0]`. Frozen so a record handed out can't be edited in place;
    writes go through the store."""

    id: str
    user_id: str
    name: str
    symbols: tuple = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WatchlistStore:
    """What the API needs from a watchlist repository."""

    def create(self, user_id: str, name: str, symbols: Sequence[str]) -> Watchlist:
        raise NotImplementedError

    def get(self, watchlist_id: str) -> Watchlist:
        raise NotImplementedError

    def update(
        self, watchlist_id: str, name: Optional[str] = None, symbols: Optional[Sequence[str]] = None
    ) -> Watchlist:
        """Replace the name and/or the whole ordered symbol list; None keeps
        the current value."""
        raise NotImplementedError

    def delete(self, watchlist_id: str) -> Watchlist:
        raise NotImplementedError

    def list_for_user(self, user_id: str) -> List[Watchlist]:
        raise NotImplementedError


class InMemoryWatchlistStore(WatchlistStore):
    """Dictionary-backed store. Thread-safe (FastAPI runs sync endpoints
    in a thread pool). `id_fn` and `time_fn` are injectable for tests."""

    def __init__(
        self,
        id_fn: Callable[[], str] = new_short_id,
        time_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._new_id = id_fn
        self._now = time_fn
        self._items: Dict[str, Watchlist] = {}
        # (user key, name key) -> watchlist id: the uniqueness rule as data
        self._by_name: Dict[Tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # -- helpers (call with the lock held) ---------------------------------
    @staticmethod
    def _user_key(user_id: str) -> str:
        return user_id.strip().casefold()

    @classmethod
    def _name_key(cls, user_id: str, name: str) -> Tuple[str, str]:
        return cls._user_key(user_id), name.casefold()

    def _require(self, watchlist_id: str) -> Watchlist:
        item = self._items.get(watchlist_id)
        if item is None:
            raise WatchlistNotFound(watchlist_id)
        return item

    def _check_name_free(self, user_id: str, name: str, except_id: str | None = None) -> None:
        owner = self._by_name.get(self._name_key(user_id, name))
        if owner is not None and owner != except_id:
            raise DuplicateWatchlistName(user_id, name)

    def _unused_id(self) -> str:
        for _ in range(100):
            candidate = self._new_id()
            if candidate not in self._items:
                return candidate
        raise RuntimeError("could not allocate a unique watchlist id")  # pragma: no cover

    @staticmethod
    def _clean_symbols(symbols: Sequence[str]) -> tuple:
        cleaned = tuple(symbols)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("symbols must be unique within a watchlist")
        if any(not s for s in cleaned):
            raise ValueError("symbols must be non-empty")
        return cleaned

    @staticmethod
    def _clean_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("watchlist name must be non-empty")
        return name

    # -- WatchlistStore -----------------------------------------------------
    def create(self, user_id: str, name: str, symbols: Sequence[str] = ()) -> Watchlist:
        if not user_id:
            raise ValueError("user id must be non-empty")
        name = self._clean_name(name)
        symbols = self._clean_symbols(symbols)
        with self._lock:
            self._check_name_free(user_id, name)
            now = self._now()
            item = Watchlist(
                id=self._unused_id(), user_id=user_id, name=name, symbols=symbols, created_at=now, updated_at=now
            )
            self._items[item.id] = item
            self._by_name[self._name_key(user_id, name)] = item.id
            return item

    def get(self, watchlist_id: str) -> Watchlist:
        with self._lock:
            return self._require(watchlist_id)

    def update(
        self, watchlist_id: str, name: Optional[str] = None, symbols: Optional[Sequence[str]] = None
    ) -> Watchlist:
        """Replace the name and/or the whole ordered symbol list. A None
        argument keeps the current value, so a rename doesn't have to
        resend the symbols and a re-order doesn't have to resend the name."""
        if name is not None:
            name = self._clean_name(name)
        if symbols is not None:
            symbols = self._clean_symbols(symbols)
        with self._lock:
            current = self._require(watchlist_id)
            if name is None:
                name = current.name
            if symbols is None:
                symbols = current.symbols
            self._check_name_free(current.user_id, name, except_id=watchlist_id)
            updated = replace(current, name=name, symbols=symbols, updated_at=self._now())
            del self._by_name[self._name_key(current.user_id, current.name)]
            self._by_name[self._name_key(updated.user_id, updated.name)] = watchlist_id
            self._items[watchlist_id] = updated
            return updated

    def delete(self, watchlist_id: str) -> Watchlist:
        with self._lock:
            item = self._require(watchlist_id)
            del self._items[watchlist_id]
            del self._by_name[self._name_key(item.user_id, item.name)]
            return item

    def list_for_user(self, user_id: str) -> List[Watchlist]:
        wanted = self._user_key(user_id)
        with self._lock:
            return sorted(
                (item for item in self._items.values() if self._user_key(item.user_id) == wanted),
                key=lambda item: item.created_at,
            )

    # -- maintenance ----------------------------------------------------------
    def clear(self) -> int:
        """Drop everything (tests). Returns how many watchlists were removed."""
        with self._lock:
            count = len(self._items)
            self._items.clear()
            self._by_name.clear()
            return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
