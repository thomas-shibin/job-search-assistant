"""Tests for the Milestone 11 file-based run-lock."""
from __future__ import annotations

import pytest

from app.scheduler.lock import RunAlreadyInProgressError, run_lock


def test_lock_acquired_and_released(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    lock_file = tmp_path / ".scheduler.lock"

    with run_lock():
        assert lock_file.exists()

    assert not lock_file.exists()


def test_concurrent_lock_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))

    with run_lock():
        with pytest.raises(RunAlreadyInProgressError):
            with run_lock():
                pass


def test_lock_released_even_if_body_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    lock_file = tmp_path / ".scheduler.lock"

    with pytest.raises(ValueError):
        with run_lock():
            raise ValueError("boom")

    assert not lock_file.exists()
