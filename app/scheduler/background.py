"""Wires `app/scheduler/run.py` into an APScheduler `BackgroundScheduler`
for `python cli.py serve` (Milestone 11) -- runs on its own thread alongside
the uvicorn web server, on the interval configured under
`scheduler.interval_minutes`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from app.config.schema import AppConfig

logger = logging.getLogger(__name__)


def _run_tick(config_path: Path) -> None:
    from app.config.loader import load_config
    from app.database.session import SessionLocal
    from app.scheduler.run import run_once

    # Re-read config.yaml on every tick so edits take effect without restarting `serve`.
    config = load_config(config_path)
    session = SessionLocal()
    try:
        run_once(session, config)
    except Exception:
        logger.exception("Scheduled run failed")
    finally:
        session.close()


def start_background_scheduler(config: AppConfig, config_path: Path) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_tick,
        "interval",
        minutes=config.scheduler.interval_minutes,
        args=[config_path],
        id="job_search_run",
    )
    scheduler.start()
    return scheduler
