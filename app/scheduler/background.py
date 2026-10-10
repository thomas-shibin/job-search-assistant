"""Wires `app/scheduler/run.py` into an APScheduler `BackgroundScheduler`
for `python cli.py serve` (Milestone 11) -- runs on its own thread alongside
the uvicorn web server. Settings live in the database and can change from the
dashboard at any time, so the scheduler always runs: each tick re-reads them,
skips the scan while `scheduler.enabled` is off, and `apply_schedule` moves
the timer whenever `scheduler.interval_minutes` changes.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from app.config.schema import AppConfig

logger = logging.getLogger(__name__)

JOB_ID = "job_search_run"
_MANUAL_JOB_ID = "job_search_run_now"

# The running scheduler under `serve`, so dashboard routes can reschedule it or
# trigger a scan; None under plain `run`, the CLI, and tests.
_scheduler: BackgroundScheduler | None = None
_config_path: Path | None = None


def _run_tick(config_path: Path, *, force: bool = False) -> None:
    from app.config.store import load_app_config
    from app.database.session import SessionLocal
    from app.scheduler.run import run_once

    session = SessionLocal()
    try:
        config = load_app_config(session, config_path)
        apply_schedule(config)
        if not (config.scheduler.enabled or force):
            logger.info("Scheduler disabled in settings -- skipping this scan")
            return
        run_once(session, config)
    except Exception:
        logger.exception("Scheduled run failed")
    finally:
        session.close()


def apply_schedule(config: AppConfig) -> None:
    """Re-time the periodic scan if `scheduler.interval_minutes` changed."""
    if _scheduler is None:
        return
    job = _scheduler.get_job(JOB_ID)
    minutes = config.scheduler.interval_minutes
    if job is not None and job.trigger.interval.total_seconds() != minutes * 60:
        _scheduler.reschedule_job(JOB_ID, trigger="interval", minutes=minutes)
        logger.info("Scan interval changed to %s minutes", minutes)


def trigger_scan_now() -> bool:
    """Start one scan immediately in the background (even if periodic scans are
    paused). Returns False when no scheduler is running (i.e. not under `serve`).
    """
    if _scheduler is None or _config_path is None:
        return False
    _scheduler.add_job(
        _run_tick, args=[_config_path], kwargs={"force": True}, id=_MANUAL_JOB_ID,
        replace_existing=True, next_run_time=datetime.now(),
    )
    return True


def is_running() -> bool:
    return _scheduler is not None


def next_scan_time() -> datetime | None:
    job = _scheduler.get_job(JOB_ID) if _scheduler is not None else None
    return job.next_run_time if job is not None else None


def start_background_scheduler(config: AppConfig, config_path: Path) -> BackgroundScheduler:
    global _scheduler, _config_path
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_tick,
        "interval",
        minutes=config.scheduler.interval_minutes,
        args=[config_path],
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler, _config_path = scheduler, config_path
    return scheduler
