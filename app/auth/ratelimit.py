"""Simple in-process rate limits for sign-in, sign-up and email-sending forms
(per IP and per account), on top of the per-account lockout in the service.
One process is what `serve` runs; behind several processes use a shared store."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 50_000:  # bound memory under abuse
                self._hits.clear()
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = RateLimiter(limit=10, window_seconds=300)     # per IP
signup_limiter = RateLimiter(limit=5, window_seconds=3600)    # per IP
email_limiter = RateLimiter(limit=5, window_seconds=3600)     # per IP+address: verification/reset emails
guest_limiter = RateLimiter(limit=10, window_seconds=3600)    # per IP


def reset_all() -> None:
    for limiter in (login_limiter, signup_limiter, email_limiter, guest_limiter):
        limiter.reset()
