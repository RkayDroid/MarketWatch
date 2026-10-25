"""Unit tests for rate_limit.py, using a controllable clock."""

import pytest

import rate_limit
from rate_limit import RateLimitExceeded, SlidingWindowRateLimiter


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 10_000.0}
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: state["t"])

    def advance(seconds):
        state["t"] += seconds

    return advance


def test_limiter_allows_up_to_max_then_blocks(clock):
    limiter = SlidingWindowRateLimiter(max_calls=3, window_seconds=3600)

    for _ in range(3):
        limiter.try_acquire()
    assert limiter.remaining() == 0

    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.try_acquire()
    assert exc_info.value.retry_after_seconds == pytest.approx(3600)


def test_limiter_window_slides(clock):
    limiter = SlidingWindowRateLimiter(max_calls=2, window_seconds=3600)

    limiter.try_acquire()  # t=0
    clock(1800)
    limiter.try_acquire()  # t=1800
    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.try_acquire()
    assert exc_info.value.retry_after_seconds == pytest.approx(1800)

    clock(1801)  # first call has fallen out of the window
    limiter.try_acquire()
    assert limiter.remaining() == 0


def test_limiter_remaining_counts_down():
    limiter = SlidingWindowRateLimiter(max_calls=5)
    assert limiter.remaining() == 5
    limiter.try_acquire()
    limiter.try_acquire()
    assert limiter.remaining() == 3


def test_limiter_is_thread_safe_under_contention():
    import threading

    limiter = SlidingWindowRateLimiter(max_calls=500)
    successes = []
    failures = []

    def worker():
        for _ in range(100):
            try:
                limiter.try_acquire()
                successes.append(1)
            except RateLimitExceeded:
                failures.append(1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 500
    assert len(failures) == 500
