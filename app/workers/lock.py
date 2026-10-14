"""File-based run-lock (Milestone 11) so a background scheduler tick and a
manual `python cli.py run` (or two overlapping background ticks, if a run
takes longer than the configured interval) never execute concurrently
against the same database.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.paths import PROJECT_ROOT


class RunAlreadyInProgressError(Exception):
    """Raised when a run is requested while the lock file is already held."""


def _lock_path() -> Path:
    """Overridable via SCHEDULER_DATA_DIR (same pattern as DATABASE_URL /
    APPLICATIONS_DATA_DIR) so tests don't touch the real project's data/ dir.
    """
    override = os.environ.get("SCHEDULER_DATA_DIR")
    base = Path(override) if override else PROJECT_ROOT / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base / ".scheduler.lock"


@contextmanager
def run_lock() -> Iterator[None]:
    lock_path = _lock_path()
    if lock_path.exists():
        raise RunAlreadyInProgressError(
            f"A scheduler run is already in progress (lock file: {lock_path}). "
            "If a previous run crashed without cleaning up, delete that file to reset."
        )
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)
