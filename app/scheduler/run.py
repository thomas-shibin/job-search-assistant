"""Orchestrates one discover -> analyze -> notify -> tailor pass across every
automated source configured under any enabled search profile (Milestone 11).
Per-source failure isolation (spec §21: one failing source must never hide or
abort the others' results) means each source's exceptions are caught and
recorded on its own `SourceRunResult` row rather than aborting the run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai.base import AIProvider
from app.ai.factory import AIProviderConfigError, build_ai_provider
from app.ai.settings import AISettings
from app.config.schema import AppConfig, SearchProfile
from app.ingestion.pipeline import ingest_raw_job
from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.models.scheduler import SchedulerRun, SourceRunResult
from app.notifications.base import NotificationProvider
from app.notifications.service import maybe_notify_qualified_job
from app.resume.pipeline import generate_resume_bundle
from app.scheduler.lock import RunAlreadyInProgressError, run_lock
from app.sources.base import JobSource
from app.sources.greenhouse import GreenhouseSource
from app.sources.lever import LeverSource
from app.utils.logging import log_event

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    jobs_discovered: int = 0
    jobs_new: int = 0
    jobs_qualified: int = 0
    jobs_tailored: int = 0
    source_failures: int = 0
    skipped: bool = False


def _configured_sources(search_profile: SearchProfile) -> list[tuple[str, JobSource]]:
    sources: list[tuple[str, JobSource]] = []
    for token in search_profile.greenhouse_boards:
        sources.append((f"greenhouse:{token}", GreenhouseSource(board_token=token)))
    for company in search_profile.lever_companies:
        sources.append((f"lever:{company}", LeverSource(company=company)))
    return sources


def _build_notification_provider() -> NotificationProvider | None:
    try:
        from app.notifications.windows_toast import WindowsToastNotificationProvider

        return WindowsToastNotificationProvider()
    except Exception:
        logger.warning("Could not initialize the Windows toast notification provider", exc_info=True)
        return None


def run_once(session: Session, config: AppConfig) -> RunSummary:
    try:
        with run_lock():
            return _run(session, config)
    except RunAlreadyInProgressError as exc:
        logger.warning(str(exc))
        return RunSummary(skipped=True)


def _run_source(
    session: Session,
    scheduler_run: SchedulerRun,
    source_name: str,
    source: JobSource,
    search_profile: SearchProfile,
    config: AppConfig,
    ai_provider: AIProvider | None,
    notification_provider: NotificationProvider | None,
    summary: RunSummary,
) -> None:
    result = SourceRunResult(
        scheduler_run_id=scheduler_run.id,
        source_name=source_name,
        status="OK",
        jobs_discovered=0,
        jobs_new=0,
        jobs_duplicate=0,
        jobs_qualified=0,
    )
    try:
        raw_jobs = source.discover_jobs(search_profile)
        result.jobs_discovered = len(raw_jobs)
        summary.jobs_discovered += len(raw_jobs)

        for raw in raw_jobs:
            ingest_result = ingest_raw_job(session, raw)
            if not ingest_result.created:
                result.jobs_duplicate += 1
                continue

            result.jobs_new += 1
            summary.jobs_new += 1
            job = ingest_result.job
            job.search_profile_name = search_profile.name
            session.commit()

            try:
                analysis = analyze_job(session, job, config, ai_provider=ai_provider)
            except NoActiveCandidateProfileError:
                logger.warning("No active candidate profile; skipping analysis for job_id=%s", job.id)
                continue

            if not analysis.qualified:
                continue

            result.jobs_qualified += 1
            summary.jobs_qualified += 1
            maybe_notify_qualified_job(job, analysis.breakdown, config, notification_provider)

            try:
                generate_resume_bundle(session, job, config, ai_provider=ai_provider)
                summary.jobs_tailored += 1
            except Exception:
                logger.warning("Resume generation failed for job_id=%s", job.id, exc_info=True)

    except Exception as exc:
        result.status = "FAILED"
        result.error_message = str(exc)
        summary.source_failures += 1
        logger.warning("Source %s failed this run", source_name, exc_info=True)

    session.add(result)
    session.commit()


def _run(session: Session, config: AppConfig) -> RunSummary:
    summary = RunSummary()
    scheduler_run = SchedulerRun(status="RUNNING")
    session.add(scheduler_run)
    session.commit()

    ai_provider: AIProvider | None = None
    if config.ai.enabled or config.ai.generate_match_explanations:
        try:
            ai_provider = build_ai_provider(config.ai, AISettings())
        except AIProviderConfigError:
            logger.warning("AI provider misconfigured; continuing this run without AI extraction", exc_info=True)

    notification_provider = _build_notification_provider()

    for search_profile in config.search_profiles:
        if not search_profile.enabled:
            continue
        for source_name, source in _configured_sources(search_profile):
            _run_source(
                session, scheduler_run, source_name, source, search_profile, config,
                ai_provider, notification_provider, summary,
            )

    scheduler_run.status = "COMPLETED" if summary.source_failures == 0 else "COMPLETED_WITH_ERRORS"
    scheduler_run.finished_at = datetime.now(timezone.utc)
    session.commit()

    log_event(
        logger, "SCHEDULER_RUN_COMPLETE", scheduler_run_id=scheduler_run.id,
        jobs_discovered=summary.jobs_discovered, jobs_new=summary.jobs_new,
        jobs_qualified=summary.jobs_qualified, jobs_tailored=summary.jobs_tailored,
        source_failures=summary.source_failures,
    )
    return summary
