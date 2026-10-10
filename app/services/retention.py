"""Retention clean-up: delete stored jobs older than `retention.delete_jobs_after_days`
(Settings -> Retention), with everything derived from them -- scores, AI
analyses, tailored resumes and their files. Runs after every scheduled scan;
also `python cli.py cleanup-jobs`.

Never deleted: settings, search criteria, watches; jobs you've acted on (unless
`keep_jobs_you_acted_on` is off); AI cost history (kept, detached from the job);
the watchlist alert log (kept, so a reposted role still isn't re-alerted).

Each deleted job leaves a `JobTombstone`, so if a later scan finds the same
posting again it is recognised and skipped instead of being saved, scored (AI
cost) and alerted as new.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.applications.folder import application_folder_path
from app.config.schema import AppConfig
from app.ingestion.fingerprint import compute_description_hash, compute_fingerprint
from app.ingestion.normalize import normalize_raw_job
from app.models.ai_usage import AICallLog
from app.models.application import ApplicationArtifactSet
from app.models.job import Job, JobRequirements, JobStatus, JobStatusHistory, JobTombstone
from app.models.matching import JobFitAssessment, MatchAnalysis
from app.models.resume import TailoredBulletProvenance, TailoredResume
from app.models.settings import WatchlistAlert
from app.sources.base import RawJob
from app.utils.logging import log_event

logger = logging.getLogger(__name__)

# Statuses that mean "I did something with this job" -- kept by default.
ACTED_ON = (
    JobStatus.APPROVED, JobStatus.APPLICATION_OPENED, JobStatus.APPLIED, JobStatus.INTERVIEW,
    JobStatus.OFFER, JobStatus.REJECTED, JobStatus.ARCHIVED,
)
# Tombstones and old alert-log rows only need to outlive the job board's own
# listings (typically 30-60 days) to stop re-discovery.
TOMBSTONE_DAYS = 120


@dataclass
class CleanupReport:
    deleted: int = 0
    kept_acted_on: int = 0
    folders_removed: int = 0
    tombstones_pruned: int = 0
    deleted_titles: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


def expired_jobs(session: Session, config: AppConfig, now: datetime | None = None) -> tuple[list[Job], int]:
    """(jobs to delete, how many old jobs are kept because you acted on them)."""
    days = config.retention.delete_jobs_after_days
    if days <= 0:
        return [], 0
    cutoff = (now or _utcnow()) - timedelta(days=days)
    old = session.query(Job).filter(Job.date_discovered < cutoff).all()
    if not config.retention.keep_jobs_you_acted_on:
        return old, 0
    keep = [job for job in old if job.status in ACTED_ON]
    return [job for job in old if job.status not in ACTED_ON], len(keep)


def delete_job(session: Session, job: Job) -> bool:
    """Delete one job and everything derived from it, leaving a tombstone. No commit.
    Returns whether its application folder was removed."""
    session.add(JobTombstone(source=job.source, external_id=job.external_id,
                             fingerprint=job.fingerprint, description_hash=job.description_hash))
    tailored_ids = [row.id for row in session.query(TailoredResume.id).filter_by(job_id=job.id)]
    if tailored_ids:
        session.query(TailoredBulletProvenance).filter(
            TailoredBulletProvenance.tailored_resume_id.in_(tailored_ids)).delete(synchronize_session=False)
    for model in (TailoredResume, ApplicationArtifactSet, MatchAnalysis, JobFitAssessment, JobRequirements,
                  JobStatusHistory):
        session.query(model).filter_by(job_id=job.id).delete(synchronize_session=False)
    # Kept, just detached: AI spend history and the alert log.
    session.query(AICallLog).filter_by(job_id=job.id).update({AICallLog.job_id: None}, synchronize_session=False)
    session.query(WatchlistAlert).filter_by(job_id=job.id).update({WatchlistAlert.job_id: None},
                                                                  synchronize_session=False)
    folder = application_folder_path(job)
    session.delete(job)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        return True
    return False


def purge_old_jobs(session: Session, config: AppConfig, *, dry_run: bool = False,
                   now: datetime | None = None) -> CleanupReport:
    jobs, kept = expired_jobs(session, config, now)
    report = CleanupReport(kept_acted_on=kept, deleted_titles=[f"{j.title} @ {j.company}" for j in jobs])
    if dry_run:
        report.deleted = len(jobs)
        return report
    for job in jobs:
        report.folders_removed += delete_job(session, job)
        report.deleted += 1
    old = (now or _utcnow()) - timedelta(days=TOMBSTONE_DAYS)
    report.tombstones_pruned = session.query(JobTombstone).filter(JobTombstone.deleted_at < old).delete(
        synchronize_session=False)
    session.query(WatchlistAlert).filter(WatchlistAlert.created_at < old).delete(synchronize_session=False)
    session.commit()
    if report.deleted:
        log_event(logger, "JOBS_PURGED", deleted=report.deleted, kept_acted_on=kept,
                  days=config.retention.delete_jobs_after_days)
    return report


def was_deleted(session: Session, raw: RawJob) -> bool:
    """Is `raw` a posting the clean-up already deleted? (Scans skip those.)"""
    normalized = normalize_raw_job(raw)
    if raw.external_id and session.query(JobTombstone.id).filter_by(
            source=raw.source, external_id=raw.external_id).first():
        return True
    fingerprint = compute_fingerprint(normalized["company"], normalized["title"], normalized["location"])
    description_hash = compute_description_hash(normalized["description"])
    return session.query(JobTombstone.id).filter(
        (JobTombstone.fingerprint == fingerprint) | (JobTombstone.description_hash == description_hash)
    ).first() is not None
