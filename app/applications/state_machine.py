"""Explicit allowed-transition map for the review/apply/outcome stages of
`JobStatus` (spec §15: only the user decides these -- unlike the earlier
DISCOVERED -> ... -> RESUME_GENERATED stages, which the pipeline
(`app/matching/pipeline.py`, `app/resume/tailor.py`) advances on its own).
Every transition here is recorded via `JobStatusHistory`, same as the
automated ones, so the full lifecycle stays in one audit trail regardless of
who moved a job.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.job import Job, JobStatus, JobStatusHistory
from app.utils.logging import log_event

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised when a requested status transition isn't allowed from the job's current status."""


_TERMINAL_STATUSES = frozenset({JobStatus.SKIPPED, JobStatus.REJECTED, JobStatus.ARCHIVED, JobStatus.ERROR})

# A user can always back out to SKIPPED/ARCHIVED from any non-terminal status, so those
# are checked separately rather than repeated in every entry below.
_ALWAYS_AVAILABLE = frozenset({JobStatus.SKIPPED, JobStatus.ARCHIVED})

_STAGE_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.RESUME_GENERATED: frozenset({JobStatus.READY_FOR_REVIEW}),
    JobStatus.READY_FOR_REVIEW: frozenset({JobStatus.APPROVED}),
    JobStatus.APPROVED: frozenset({JobStatus.APPLICATION_OPENED}),
    JobStatus.APPLICATION_OPENED: frozenset({JobStatus.APPLIED}),
    JobStatus.APPLIED: frozenset({JobStatus.INTERVIEW, JobStatus.REJECTED}),
    JobStatus.INTERVIEW: frozenset({JobStatus.OFFER, JobStatus.REJECTED}),
}


def _is_allowed(from_status: JobStatus, to_status: JobStatus) -> bool:
    if from_status in _TERMINAL_STATUSES:
        return False
    if to_status in _ALWAYS_AVAILABLE:
        return True
    return to_status in _STAGE_TRANSITIONS.get(from_status, frozenset())


def transition_job(session: Session, job: Job, to_status: JobStatus, *, reason: str, actor: str = "user") -> Job:
    if not _is_allowed(job.status, to_status):
        raise InvalidTransitionError(f"Cannot move job {job.id} from {job.status.value} to {to_status.value}")

    from_status = job.status
    job.status = to_status
    session.add(
        JobStatusHistory(job_id=job.id, from_status=from_status, to_status=to_status, reason=reason, actor=actor)
    )
    session.commit()
    session.refresh(job)
    log_event(
        logger,
        "JOB_STATUS_CHANGED",
        job_id=job.id,
        from_status=from_status.value,
        to_status=to_status.value,
        actor=actor,
    )
    return job


def mark_ready_for_review(session: Session, job: Job, reason: str = "Resume bundle ready") -> Job:
    return transition_job(session, job, JobStatus.READY_FOR_REVIEW, reason=reason, actor="system")


def approve(session: Session, job: Job, reason: str = "Approved by user") -> Job:
    return transition_job(session, job, JobStatus.APPROVED, reason=reason)


def skip(session: Session, job: Job, reason: str = "Skipped by user") -> Job:
    return transition_job(session, job, JobStatus.SKIPPED, reason=reason)


def mark_application_opened(session: Session, job: Job, reason: str = "Application page opened") -> Job:
    return transition_job(session, job, JobStatus.APPLICATION_OPENED, reason=reason)


def mark_applied(session: Session, job: Job, reason: str = "Marked applied by user") -> Job:
    return transition_job(session, job, JobStatus.APPLIED, reason=reason)


def mark_interview(session: Session, job: Job, reason: str = "Interview scheduled") -> Job:
    return transition_job(session, job, JobStatus.INTERVIEW, reason=reason)


def mark_offer(session: Session, job: Job, reason: str = "Offer received") -> Job:
    return transition_job(session, job, JobStatus.OFFER, reason=reason)


def mark_rejected(session: Session, job: Job, reason: str = "Rejected") -> Job:
    return transition_job(session, job, JobStatus.REJECTED, reason=reason)


def archive(session: Session, job: Job, reason: str = "Archived by user") -> Job:
    return transition_job(session, job, JobStatus.ARCHIVED, reason=reason)
