"""
Process-wide rate limiting for upstream yfinance/Yahoo calls.

``SlidingWindowRateLimiter`` tracks how many upstream HTTP calls were made
in the trailing 60 minutes and refuses new ones once a configurable budget
is reached. This protects the process (and the IP it runs on) from
crossing Yahoo's informal free-tier ceiling of ~2000 requests/hour,
regardless of how many clients hit our own API.

The limiter is process-local; for multiple workers/instances back it with
a shared store (e.g. Redis) so the budget is global rather than
per-process.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


class RateLimitExceeded(Exception):
    """Raised when the upstream (yfinance/Yahoo) call budget is exhausted."""

    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Upstream yfinance call budget exhausted. Retry after "
            f"{retry_after_seconds:.0f}s."
        )


@dataclass
class SlidingWindowRateLimiter:
    """Caps the number of upstream calls allowed in a rolling time window.

    Yahoo's free/unauthenticated endpoints are informally rate limited to
    roughly 2000 requests/hour per IP before returning HTTP 429s (and
    yfinance itself will start raising ``YFRateLimitError``). We default to
    a noticeably smaller budget (e.g. 1800/hour) to leave headroom for:
      * other processes/services sharing the same outbound IP,
      * the extra calls yfinance sometimes issues internally (e.g. to
        resolve timezone/exchange metadata), and
      * simple clock/measurement drift.
    """

    max_calls: int
    window_seconds: float = 3600.0
    _calls: Deque[float] = field(default_factory=deque, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()

    def remaining(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return max(0, self.max_calls - len(self._calls))

    def try_acquire(self) -> None:
        """Reserve budget for one upstream call.

        Raises ``RateLimitExceeded`` if the budget for the current rolling
        window is already spent.
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._calls) >= self.max_calls:
                # Empty deque can coincide with max_calls=0 (hard-disabled).
                if self._calls:
                    retry_after = self.window_seconds - (now - self._calls[0])
                else:
                    retry_after = self.window_seconds
                raise RateLimitExceeded(max(retry_after, 1.0))
            self._calls.append(now)
