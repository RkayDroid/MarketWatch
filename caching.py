"""
Two-tier cache for upstream (yfinance/Yahoo) results.

    request ──► memory (L1) ──miss──► disk (L2) ──miss──► live call
                   ▲                    ▲                    │
                   └────── write-through on every store ─────┘

* Reads check memory first, then disk. A disk hit is promoted back into
  memory so the next read is served from L1.
* Writes go to memory and are persisted to disk immediately (write-through).
* Every entry has a TTL that applies identically in both tiers. The
  default is 1 day; callers may pass a shorter per-entry TTL for volatile
  data (the app uses 5 minutes for "as of now" views - see stock_router).
  Expired entries are dropped when read, swept on writes, and removed by
  ``purge_expired`` / ``clear``.
* Each tier is bounded independently and evicts least-recently-used
  entries when full: memory by entry count, disk by total bytes.
* The disk tier's only purpose is to survive restarts, which empty the
  memory tier. Expiry times are persisted, so a restart never revives an
  entry that had already expired.
* ``clear()`` empties both tiers.

Configuration (environment variables, read by ``make_cache_from_env``):

    MWL_CACHE_TTL_SECONDS          86400   default (long) TTL, both tiers
    MWL_MEMORY_CACHE_MAX_ENTRIES   512     L1 bound (entries)
    MWL_DISK_CACHE_MAX_MB          256     L2 bound (megabytes on disk)
    MWL_CACHE_DIR                  .cache  directory of the SQLite file
"""

from __future__ import annotations

import os
import pickle
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Tuple

DEFAULT_TTL_SECONDS = 24 * 3600  # 1 day
DEFAULT_MEMORY_MAX_ENTRIES = 512
DEFAULT_DISK_MAX_MB = 256

# Returned by get() when a key is absent or expired. A stored ``None`` is a
# legitimate value (negative caching of "symbol not found"), so None can't
# double as the miss marker.
MISS = object()


class MemoryLRUCache:
    """L1: in-process LRU with TTL, bounded by entry count. Thread-safe."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MEMORY_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] = time.time,
    ):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._now = time_fn
        self._store: "OrderedDict[str, Tuple[Any, float]]" = OrderedDict()  # key -> (value, expires_at)
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        now = self._now()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return MISS
            value, expires_at = item
            if now >= expires_at:
                del self._store[key]
                return MISS
            self._store.move_to_end(key)  # reading refreshes LRU recency
            return value

    def set(self, key: str, value: Any, expires_at: float | None = None) -> None:
        """Store `value`. `expires_at` lets the tiered cache keep both tiers
        on the same clock for an entry; default is now + ttl."""
        now = self._now()
        with self._lock:
            self._store[key] = (value, expires_at if expires_at is not None else now + self.ttl_seconds)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)  # evict least recently used

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def purge_expired(self) -> int:
        now = self._now()
        with self._lock:
            expired = [k for k, (_, exp) in self._store.items() if now >= exp]
            for k in expired:
                del self._store[k]
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._store), "max_entries": self.max_entries}


class DiskLRUCache:
    """L2: SQLite-backed LRU with TTL, bounded by total stored bytes.
    Persists across restarts. Thread-safe (single connection + lock, WAL).

    Values are pickled (they include pandas DataFrames). LRU recency uses a
    monotonic access counter rather than wall-clock time, because accesses
    within timer resolution would tie and make eviction order arbitrary.
    """

    def __init__(
        self,
        path: str,
        max_bytes: int = DEFAULT_DISK_MAX_MB * 1024 * 1024,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] = time.time,
    ):
        self.path = path
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self._now = time_fn
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                   key TEXT PRIMARY KEY,
                   value BLOB NOT NULL,
                   size INTEGER NOT NULL,
                   expires_at REAL NOT NULL,
                   access_seq INTEGER NOT NULL
               )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_access ON cache(access_seq)")
        (seq,) = self._conn.execute("SELECT MAX(access_seq) FROM cache").fetchone()
        self._seq = seq or 0

    def get(self, key: str) -> Any:
        now = self._now()
        with self._lock:
            row = self._conn.execute("SELECT value, expires_at FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return MISS
            blob, expires_at = row
            if now >= expires_at:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return MISS
            self._seq += 1
            self._conn.execute("UPDATE cache SET access_seq = ? WHERE key = ?", (self._seq, key))
            return pickle.loads(blob)

    def get_with_expiry(self, key: str) -> Tuple[Any, float | None]:
        """Like get() but also returns the entry's expiry so the tiered
        cache can promote it to memory without extending its life."""
        now = self._now()
        with self._lock:
            row = self._conn.execute("SELECT value, expires_at FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return MISS, None
            blob, expires_at = row
            if now >= expires_at:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return MISS, None
            self._seq += 1
            self._conn.execute("UPDATE cache SET access_seq = ? WHERE key = ?", (self._seq, key))
            return pickle.loads(blob), expires_at

    def set(self, key: str, value: Any, expires_at: float | None = None) -> None:
        now = self._now()
        blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock:
            self._seq += 1
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, size, expires_at, access_seq) VALUES (?, ?, ?, ?, ?)",
                (key, blob, len(blob), expires_at if expires_at is not None else now + self.ttl_seconds, self._seq),
            )
            # Maintenance on every write: sweep expired, then enforce the
            # byte bound by evicting least-recently-used entries.
            self._conn.execute("DELETE FROM cache WHERE expires_at <= ?", (now,))
            while True:
                total, count = self._conn.execute("SELECT COALESCE(SUM(size), 0), COUNT(*) FROM cache").fetchone()
                if total <= self.max_bytes or count <= 1:
                    break
                self._conn.execute(
                    "DELETE FROM cache WHERE key = (SELECT key FROM cache ORDER BY access_seq ASC LIMIT 1)"
                )

    def clear(self) -> int:
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()
            self._conn.execute("DELETE FROM cache")
            return count

    def purge_expired(self) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM cache WHERE expires_at <= ?", (self._now(),)).rowcount

    def stats(self) -> dict:
        with self._lock:
            total, count = self._conn.execute("SELECT COALESCE(SUM(size), 0), COUNT(*) FROM cache").fetchone()
            return {"entries": count, "bytes": total, "max_bytes": self.max_bytes, "path": self.path}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class TieredCache:
    """Memory-first, disk-second, write-through. The object the app uses."""

    def __init__(self, memory: MemoryLRUCache, disk: DiskLRUCache):
        self.memory = memory
        self.disk = disk

    @property
    def ttl_seconds(self) -> float:
        return self.memory.ttl_seconds

    def get(self, key: str) -> Tuple[Any, str]:
        """Return (value, tier) with tier in MEMORY | DISK | MISS."""
        value = self.memory.get(key)
        if value is not MISS:
            return value, "MEMORY"
        value, expires_at = self.disk.get_with_expiry(key)
        if value is not MISS:
            self.memory.set(key, value, expires_at=expires_at)  # promote, same expiry
            return value, "DISK"
        return MISS, "MISS"

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        """Store in both tiers with the same expiry: now + `ttl_seconds`
        (or the default TTL)."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = self.memory._now() + ttl
        self.memory.set(key, value, expires_at=expires_at)
        self.disk.set(key, value, expires_at=expires_at)

    def clear(self) -> dict:
        return {"memory": self.memory.clear(), "disk": self.disk.clear()}

    def purge_expired(self) -> dict:
        return {"memory": self.memory.purge_expired(), "disk": self.disk.purge_expired()}

    def stats(self) -> dict:
        return {"ttl_seconds": self.ttl_seconds, "memory": self.memory.stats(), "disk": self.disk.stats()}

    def close(self) -> None:
        self.disk.close()


def make_cache_from_env() -> TieredCache:
    """Build the two-tier cache from MWL_* environment variables (see
    module docstring for names and defaults)."""
    ttl = float(os.environ.get("MWL_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS))
    memory_max = int(os.environ.get("MWL_MEMORY_CACHE_MAX_ENTRIES", DEFAULT_MEMORY_MAX_ENTRIES))
    disk_max_mb = float(os.environ.get("MWL_DISK_CACHE_MAX_MB", DEFAULT_DISK_MAX_MB))
    cache_dir = os.environ.get("MWL_CACHE_DIR", ".cache")
    return TieredCache(
        memory=MemoryLRUCache(max_entries=memory_max, ttl_seconds=ttl),
        disk=DiskLRUCache(
            path=os.path.join(cache_dir, "market_watch_cache.sqlite"),
            max_bytes=int(disk_max_mb * 1024 * 1024),
            ttl_seconds=ttl,
        ),
    )


def get_or_fetch(
    cache: TieredCache,
    key: str,
    fetch_fn: Callable[[], Any],
    ttl_seconds: float | None = None,
) -> Tuple[Any, str]:
    """Cache-then-live-call: returns (value, status) with status MEMORY or
    DISK for a cache hit, MISS when `fetch_fn` was called (its result is
    then stored in both tiers for `ttl_seconds`, default TTL if None).
    Exceptions from `fetch_fn` propagate and nothing is cached for them."""
    value, tier = cache.get(key)
    if tier != "MISS":
        return value, tier
    value = fetch_fn()
    cache.set(key, value, ttl_seconds=ttl_seconds)
    return value, "MISS"
