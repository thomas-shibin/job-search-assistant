"""Orchestrates one discover -> analyze -> notify -> tailor pass across every
automated source configured under any enabled search profile (Milestone 11).
Per-source failure isolation (spec §21: one failing source must never hide or
abort the others' results) means each source's exceptions are caught and
recorded on its own `SourceRunResult` row rather than aborting the run.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai.base import AIProvider
from app.ai.factory import AIProviderConfigError, build_ai_provider
from app.ai.settings import AISettings
from app.catalog.geo import province_of
from app.catalog.store import location_paths
from app.config.schema import AppConfig, SearchProfile
from app.ingestion.pipeline import ingest_raw_job
from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.models.scheduler import SchedulerRun, SourceRunResult
from app.notifications.base import NotificationProvider
from app.notifications.service import maybe_notify_qualified_job
from app.notifications.watchlist import WatchlistAlerter, build_watchlist_alerter
from app.resume.pipeline import generate_resume_bundle
from app.scheduler.lock import RunAlreadyInProgressError, run_lock
from app.sources.adzuna import AdzunaSource
from app.services.retention import purge_old_jobs, was_deleted
from app.services.search import QuerySpec, queries_per_scan, scan_slice, scan_specs, search_jobs
from app.sources.base import JobSource, RawJob
from app.sources.greenhouse import GreenhouseSource
from app.sources.lever import LeverSource
from app.sources.settings import AdzunaSettings
from app.utils.logging import log_event

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    jobs_discovered: int = 0
    jobs_new: int = 0
    jobs_qualified: int = 0
    jobs_tailored: int = 0
    source_failures: int = 0
    jobs_deleted: int = 0
    skipped: bool = False


def _configured_sources(search_profile: SearchProfile) -> list[tuple[str, JobSource]]:
    sources: list[tuple[str, JobSource]] = []
    for token in search_profile.greenhouse_boards:
        sources.append((f"greenhouse:{token}", GreenhouseSource(board_token=token)))
    for company in search_profile.lever_companies:
        sources.append((f"lever:{company}", LeverSource(company=company)))
    return sources


class ScheduledSearchSource:
    """Adzuna, driven by the global job search *and* every watch that searches
    (see `scan_specs`): one list of queries sharing the daily request budget.
    When there are more than fit in one scan, each scan takes the next slice.
    """

    name = "adzuna"

    def __init__(
        self, adzuna: AdzunaSource, specs: list[QuerySpec], interval_minutes: int, daily_limit: int,
        areas: dict[str, list[str]] | None = None,
    ) -> None:
        self._adzuna = adzuna
        self._specs = specs
        self._interval_minutes = interval_minutes
        self._daily_limit = daily_limit
        self._areas = areas or {}

    def discover_jobs(self, search_profile: SearchProfile | None) -> list[RawJob]:
        per_scan = queries_per_scan(len(self._specs), self._interval_minutes, self._daily_limit)
        todo = scan_slice(self._specs, per_scan, self._interval_minutes)
        groups: dict[tuple, list[tuple[str | None, str | None]]] = {}
        for spec in todo:  # one search_jobs call per (keywords, filters) group
            groups.setdefault((spec.keywords, spec.filters), []).append((spec.title, spec.location))

        jobs: list[RawJob] = []
        seen: set[str] = set()
        failed = 0
        for (keywords, filters), pairs in groups.items():
            results = search_jobs(self._adzuna, [], [], pairs=pairs, per_page=50, use_cache=False,
                                  keywords=list(keywords), areas=self._areas, filters=dict(filters))
            failed += len(results.failed_queries)
            for job in results.jobs:
                key = job.external_id or job.source_url
                if key not in seen:
                    seen.add(key)
                    jobs.append(job)
        if todo and failed == len(todo):
            raise RuntimeError(f"Every Adzuna query failed ({failed})")
        return jobs


# Backwards-compatible name.
GlobalSearchSource = ScheduledSearchSource


def _scheduled_search_source(
    config: AppConfig, adzuna_settings: AdzunaSettings, areas: dict[str, list[str]] | None = None
) -> JobSource | None:
    specs = scan_specs(config)
    if not specs:
        return None
    if not (adzuna_settings.app_id and adzuna_settings.app_key):
        logger.warning("Scheduled Adzuna searches are set up but ADZUNA_APP_ID/ADZUNA_APP_KEY are not set in .env -- skipping")
        return None
    adzuna = AdzunaSource(app_id=adzuna_settings.app_id, app_key=adzuna_settings.app_key, country=config.search.country)
    return ScheduledSearchSource(adzuna, specs, config.scheduler.interval_minutes, config.search.adzuna_daily_limit, areas)


def _build_notification_provider() -> NotificationProvider | None:
    try:
        if sys.platform == "win32":
            from app.notifications.windows_toast import WindowsToastNotificationProvider

            return WindowsToastNotificationProvider()
        from app.notifications.linux_notify import LinuxNotifySendProvider

        return LinuxNotifySendProvider()
    except Exception:
        logger.warning("Could not initialize a desktop notification provider for %s", sys.platform, exc_info=True)
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
    search_profile: SearchProfile | None,
    config: AppConfig,
    ai_provider: AIProvider | None,
    notification_provider: NotificationProvider | None,
    watchlist_alerter: WatchlistAlerter | None,
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
            if was_deleted(session, raw):  # cleaned up earlier: not new, don't re-score or re-alert
                result.jobs_duplicate += 1
                continue
            ingest_result = ingest_raw_job(session, raw)
            if not ingest_result.created:
                result.jobs_duplicate += 1
                continue

            result.jobs_new += 1
            summary.jobs_new += 1
            job = ingest_result.job
            if search_profile is not None:
                job.search_profile_name = search_profile.name
                session.commit()

            analysis = None
            try:
                analysis = analyze_job(session, job, config, ai_provider=ai_provider)
            except NoActiveCandidateProfileError:
                logger.warning("No active candidate profile; skipping analysis for job_id=%s", job.id)
            except Exception:
                # Still worth a watchlist alert even if scoring failed (e.g. AI outage).
                session.rollback()
                logger.warning("Analysis failed for job_id=%s", job.id, exc_info=True)

            if watchlist_alerter is not None:
                score = analysis.breakdown.overall_score if analysis and analysis.breakdown else None
                watchlist_alerter.consider(job, score)

            if analysis is None or not analysis.qualified:
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
    # Province of each job's listed location, via the location catalog
    # ("Mississauga, Peel region" -> ON), for watches that name a province.
    watchlist_alerter = build_watchlist_alerter(
        config, province_resolver=lambda job: province_of(session, job.location), session=session,
    )
    if watchlist_alerter is not None:
        watchlist_alerter.retry_failed()  # alerts Telegram couldn't deliver last time
    adzuna_settings = AdzunaSettings()

    for search_profile in config.search_profiles:
        if not search_profile.enabled:
            continue
        for source_name, source in _configured_sources(search_profile):
            _run_source(
                session, scheduler_run, source_name, source, search_profile, config,
                ai_provider, notification_provider, watchlist_alerter, summary,
            )

    global_source = _scheduled_search_source(config, adzuna_settings, location_paths(session))
    if global_source is not None:
        _run_source(
            session, scheduler_run, "adzuna", global_source, None, config,
            ai_provider, notification_provider, watchlist_alerter, summary,
        )

    if watchlist_alerter is not None:
        watchlist_alerter.flush()

    try:  # retention clean-up: old jobs you haven't acted on (Settings -> Retention)
        summary.jobs_deleted = purge_old_jobs(session, config).deleted
    except Exception:
        session.rollback()
        logger.warning("Job clean-up failed this run", exc_info=True)

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
