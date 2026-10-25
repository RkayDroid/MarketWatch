"""Unit tests for caching.py: each tier on its own, then the tiered
behaviour (memory-first reads, write-through, restart survival, clear
both), the size bounds, the single TTL, and get_or_fetch."""

import threading

import pandas as pd
import pytest

from caching import (
    DEFAULT_TTL_SECONDS,
    MISS,
    DiskLRUCache,
    MemoryLRUCache,
    TieredCache,
    get_or_fetch,
    make_cache_from_env,
)


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def disk_path(tmp_path):
    return str(tmp_path / "cache.sqlite")


def make_tiered(disk_path, clock, *, ttl=DEFAULT_TTL_SECONDS, memory_max=512, disk_max_bytes=10**9):
    return TieredCache(
        memory=MemoryLRUCache(max_entries=memory_max, ttl_seconds=ttl, time_fn=clock),
        disk=DiskLRUCache(path=disk_path, max_bytes=disk_max_bytes, ttl_seconds=ttl, time_fn=clock),
    )


# --------------------------------------------------------------------------
# Single-tier contract (parametrised over both tiers)
# --------------------------------------------------------------------------
@pytest.fixture(params=["memory", "disk"])
def tier_factory(request, tmp_path):
    made = []

    def factory(clock, **kw):
        if request.param == "memory":
            c = MemoryLRUCache(time_fn=clock, **kw)
        else:
            c = DiskLRUCache(path=str(tmp_path / f"tier{len(made)}.sqlite"), time_fn=clock, **kw)
        made.append(c)
        return c

    return factory


def test_tier_miss_then_hit(tier_factory, clock):
    tier = tier_factory(clock)
    assert tier.get("k") is MISS
    tier.set("k", {"v": 1})
    assert tier.get("k") == {"v": 1}


def test_tier_stored_none_is_a_hit_not_a_miss(tier_factory, clock):
    tier = tier_factory(clock)
    tier.set("nope", None)
    assert tier.get("nope") is None  # value None, not MISS


def test_tier_entry_expires_after_ttl(tier_factory, clock):
    tier = tier_factory(clock, ttl_seconds=3600)
    tier.set("k", "v")
    clock.advance(3599)
    assert tier.get("k") == "v"
    clock.advance(2)
    assert tier.get("k") is MISS


def test_tier_default_ttl_is_one_day(tier_factory, clock):
    tier = tier_factory(clock)
    assert tier.ttl_seconds == 24 * 3600
    tier.set("k", "v")
    clock.advance(24 * 3600 - 1)
    assert tier.get("k") == "v"
    clock.advance(2)
    assert tier.get("k") is MISS


def test_tier_purge_expired_removes_only_expired(tier_factory, clock):
    tier = tier_factory(clock, ttl_seconds=1000)
    tier.set("old", 1)
    clock.advance(600)
    tier.set("new", 2)
    clock.advance(500)  # old: 1100s (expired), new: 500s

    assert tier.purge_expired() == 1
    assert tier.get("old") is MISS
    assert tier.get("new") == 2


def test_tier_clear_returns_count(tier_factory, clock):
    tier = tier_factory(clock)
    for i in range(4):
        tier.set(f"k{i}", i)
    assert tier.clear() == 4
    assert tier.stats()["entries"] == 0


def test_tier_dataframe_round_trip(tier_factory, clock):
    tier = tier_factory(clock)
    idx = pd.date_range("2024-05-01", periods=3, freq="h", tz="America/New_York")
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)
    tier.set("frame", df)
    pd.testing.assert_frame_equal(tier.get("frame"), df)


def test_tier_thread_safety_smoke(tier_factory, clock):
    tier = tier_factory(clock)
    errors = []

    def worker(n):
        try:
            for i in range(100):
                tier.set(f"k{n}_{i % 10}", i)
                tier.get(f"k{n}_{i % 10}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# --------------------------------------------------------------------------
# Size bounds
# --------------------------------------------------------------------------
def test_memory_bound_is_entry_count_with_lru_eviction(clock):
    memory = MemoryLRUCache(max_entries=3, time_fn=clock)
    for k in ("a", "b", "c"):
        memory.set(k, k)
    memory.get("a")  # a most-recently-used -> b is LRU
    memory.set("d", "d")

    assert memory.get("b") is MISS
    assert all(memory.get(k) is not MISS for k in ("a", "c", "d"))
    assert memory.stats() == {"entries": 3, "max_entries": 3}


def test_disk_bound_is_total_bytes_with_lru_eviction(disk_path, clock):
    payload = "x" * 1000  # ~1KB pickled
    one = len(__import__("pickle").dumps(payload, protocol=5))
    disk = DiskLRUCache(path=disk_path, max_bytes=one * 3 + 50, time_fn=clock)  # room for 3

    for k in ("a", "b", "c"):
        disk.set(k, payload)
    assert disk.stats()["entries"] == 3
    disk.get("a")  # refresh a -> b is LRU
    disk.set("d", payload)

    assert disk.get("b") is MISS
    assert all(disk.get(k) is not MISS for k in ("a", "c", "d"))
    assert disk.stats()["bytes"] <= disk.max_bytes


def test_disk_bound_keeps_a_single_oversized_entry(disk_path, clock):
    disk = DiskLRUCache(path=disk_path, max_bytes=10, time_fn=clock)
    disk.set("big", "x" * 1000)
    assert disk.get("big") is not MISS  # can't evict below one entry


def test_disk_lru_is_deterministic_under_a_frozen_clock(disk_path, clock):
    """Recency is a counter, not a timestamp - all these writes share one
    clock tick, so timestamp-based LRU would be arbitrary."""
    disk = DiskLRUCache(path=disk_path, max_bytes=10**9, time_fn=clock)
    for k in ("a", "b", "c"):
        disk.set(k, k)
    # Shrink the bound so one entry must go, and trigger maintenance.
    disk.get("a")
    disk.max_bytes = disk.stats()["bytes"]  # exactly full
    disk.set("d", "d")  # now over -> evict exactly the LRU one: b
    assert disk.get("b") is MISS
    assert disk.get("a") == "a"


# --------------------------------------------------------------------------
# Tiered behaviour
# --------------------------------------------------------------------------
def test_write_goes_to_both_tiers(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    cache.set("k", "v")
    assert cache.memory.get("k") == "v"
    assert cache.disk.get("k") == "v"


def test_read_is_memory_first(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    cache.set("k", "from-set")
    cache.disk.set("k", "changed-on-disk")  # diverge tiers on purpose
    assert cache.get("k") == ("from-set", "MEMORY")


def test_read_falls_back_to_disk_and_promotes_to_memory(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    cache.disk.set("k", "only-on-disk")

    assert cache.get("k") == ("only-on-disk", "DISK")
    assert cache.memory.get("k") == "only-on-disk"  # promoted
    assert cache.get("k") == ("only-on-disk", "MEMORY")


def test_miss_in_both_tiers(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    assert cache.get("nothing") == (MISS, "MISS")


def test_restart_empties_memory_but_disk_survives(disk_path, clock):
    first = make_tiered(disk_path, clock)
    first.set("k", {"survives": True})
    first.close()

    restarted = make_tiered(disk_path, clock)  # new memory tier, same disk file
    assert restarted.memory.get("k") is MISS
    assert restarted.get("k") == ({"survives": True}, "DISK")


def test_promotion_keeps_original_expiry(disk_path, clock):
    """A disk entry promoted to memory must not get a fresh TTL."""
    first = make_tiered(disk_path, clock, ttl=1000)
    first.set("k", "v")
    first.close()

    clock.advance(900)
    restarted = make_tiered(disk_path, clock, ttl=1000)
    assert restarted.get("k") == ("v", "DISK")  # promoted at age 900
    clock.advance(150)  # age 1050 > ttl
    assert restarted.get("k") == (MISS, "MISS")


def test_expired_entry_is_not_revived_by_restart(disk_path, clock):
    first = make_tiered(disk_path, clock, ttl=1000)
    first.set("k", "v")
    first.close()

    clock.advance(1001)
    restarted = make_tiered(disk_path, clock, ttl=1000)
    assert restarted.get("k") == (MISS, "MISS")


def test_clear_empties_both_tiers(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.memory.clear()  # simulate memory-only loss
    cache.set("c", 3)

    assert cache.clear() == {"memory": 1, "disk": 3}
    assert cache.get("a") == (MISS, "MISS")
    assert cache.stats()["memory"]["entries"] == 0
    assert cache.stats()["disk"]["entries"] == 0


def test_tiered_purge_and_stats(disk_path, clock):
    cache = make_tiered(disk_path, clock, ttl=100, memory_max=7)
    cache.set("k", 1)
    clock.advance(101)
    assert cache.purge_expired() == {"memory": 1, "disk": 1}
    stats = cache.stats()
    assert stats["ttl_seconds"] == 100
    assert stats["memory"] == {"entries": 0, "max_entries": 7}
    assert stats["disk"]["entries"] == 0 and stats["disk"]["bytes"] == 0


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def test_make_cache_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MWL_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("MWL_MEMORY_CACHE_MAX_ENTRIES", "42")
    monkeypatch.setenv("MWL_DISK_CACHE_MAX_MB", "1.5")
    monkeypatch.setenv("MWL_CACHE_DIR", str(tmp_path))

    cache = make_cache_from_env()

    assert cache.ttl_seconds == 300
    assert cache.memory.max_entries == 42
    assert cache.memory.ttl_seconds == 300 and cache.disk.ttl_seconds == 300
    assert cache.disk.max_bytes == int(1.5 * 1024 * 1024)
    assert cache.disk.path == str(tmp_path / "market_watch_cache.sqlite")
    cache.close()


def test_make_cache_from_env_defaults(monkeypatch, tmp_path):
    for var in ("MWL_CACHE_TTL_SECONDS", "MWL_MEMORY_CACHE_MAX_ENTRIES", "MWL_DISK_CACHE_MAX_MB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MWL_CACHE_DIR", str(tmp_path))

    cache = make_cache_from_env()

    assert cache.ttl_seconds == 24 * 3600
    assert cache.memory.max_entries == 512
    assert cache.disk.max_bytes == 256 * 1024 * 1024
    cache.close()


# --------------------------------------------------------------------------
# get_or_fetch
# --------------------------------------------------------------------------
def test_get_or_fetch_miss_then_memory_then_disk(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    calls = []

    def fetch():
        calls.append(1)
        return "live"

    assert get_or_fetch(cache, "k", fetch) == ("live", "MISS")
    assert get_or_fetch(cache, "k", fetch) == ("live", "MEMORY")
    cache.memory.clear()
    assert get_or_fetch(cache, "k", fetch) == ("live", "DISK")
    assert len(calls) == 1


def test_get_or_fetch_refetches_after_ttl(disk_path, clock):
    cache = make_tiered(disk_path, clock, ttl=100)
    values = iter(["v1", "v2"])
    get_or_fetch(cache, "k", lambda: next(values))
    clock.advance(101)
    assert get_or_fetch(cache, "k", lambda: next(values)) == ("v2", "MISS")


def test_get_or_fetch_does_not_cache_failures(disk_path, clock):
    cache = make_tiered(disk_path, clock)

    def failing():
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        get_or_fetch(cache, "k", failing)
    assert cache.get("k") == (MISS, "MISS")


# --------------------------------------------------------------------------
# Per-entry TTL override
# --------------------------------------------------------------------------
def test_per_entry_ttl_overrides_default_in_both_tiers(disk_path, clock):
    cache = make_tiered(disk_path, clock, ttl=DEFAULT_TTL_SECONDS)
    cache.set("short", "s", ttl_seconds=300)
    cache.set("long", "l")

    clock.advance(301)
    assert cache.get("short") == (MISS, "MISS")
    assert cache.disk.get("short") is MISS  # disk tier expired too
    assert cache.get("long") == ("l", "MEMORY")

    clock.advance(DEFAULT_TTL_SECONDS)
    assert cache.get("long") == (MISS, "MISS")


def test_get_or_fetch_honours_ttl_argument(disk_path, clock):
    cache = make_tiered(disk_path, clock)
    values = iter(["v1", "v2"])
    get_or_fetch(cache, "k", lambda: next(values), ttl_seconds=300)
    clock.advance(299)
    assert get_or_fetch(cache, "k", lambda: next(values), ttl_seconds=300) == ("v1", "MEMORY")
    clock.advance(2)
    assert get_or_fetch(cache, "k", lambda: next(values), ttl_seconds=300) == ("v2", "MISS")
