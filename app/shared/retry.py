"""Small exponential-backoff retry helper (Milestone 13 hardening pass), used
for AI provider calls and source-adapter HTTP requests -- both talk to
external services that fail transiently (rate limits, dropped connections).
Deliberately minimal: no jitter, no circuit breaker -- just enough to absorb
the common transient-failure case without adding a new dependency.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Calls `fn()`, retrying with exponential backoff (`base_delay_seconds *
    2**attempt`) on any exception matching `retry_on`, up to `max_attempts`
    total tries. Re-raises the last exception if every attempt fails.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            delay = base_delay_seconds * (2**attempt)
            logger.warning(
                "Attempt %d/%d failed (%s: %s) -- retrying in %.1fs",
                attempt + 1, max_attempts, type(exc).__name__, exc, delay,
            )
            sleep(delay)
    assert last_exc is not None
    raise last_exc
