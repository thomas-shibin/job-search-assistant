"""Tests for the Milestone 13 retry helper."""
from __future__ import annotations

import pytest

from app.shared.retry import with_retry


def test_succeeds_first_try_without_sleeping():
    sleeps = []
    result = with_retry(lambda: 42, sleep=sleeps.append)
    assert result == 42
    assert sleeps == []


def test_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    sleeps = []
    result = with_retry(flaky, max_attempts=5, base_delay_seconds=1, sleep=sleeps.append)

    assert result == "ok"
    assert calls["n"] == 3
    assert sleeps == [1, 2]  # exponential backoff: 1*2^0, 1*2^1


def test_raises_last_exception_after_exhausting_attempts():
    def always_fails():
        raise ConnectionError("still broken")

    with pytest.raises(ConnectionError, match="still broken"):
        with_retry(always_fails, max_attempts=3, base_delay_seconds=0.01, sleep=lambda s: None)


def test_only_retries_matching_exception_types():
    def raises_value_error():
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        with_retry(raises_value_error, retry_on=(ConnectionError,), sleep=lambda s: None)
