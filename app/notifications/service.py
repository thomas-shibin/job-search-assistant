"""Wires `NotificationProvider` into the analyze pipeline (Milestone 10):
a job only gets notified once it's QUALIFIED and its score meets
`notifications.minimum_score`, which may be a stricter bar than
`matching.minimum_score` -- being qualified enough to track is not the same
as being worth an interruption.
"""
from __future__ import annotations

import logging

from app.core.config.schema import AppConfig
from app.matching.scorer import MatchScoreBreakdown
from app.models.job import Job
from app.notifications.base import JobNotification, NotificationProvider

logger = logging.getLogger(__name__)


def maybe_notify_qualified_job(
    job: Job, breakdown: MatchScoreBreakdown, config: AppConfig, provider: NotificationProvider | None
) -> None:
    if provider is None:
        return
    if breakdown.overall_score < config.notifications.minimum_score:
        return

    notification = JobNotification(
        job_id=job.id,
        title=job.title,
        company=job.company,
        location=job.location,
        overall_score=breakdown.overall_score,
        strong_matches=breakdown.strong_matches,
        missing_requirements=breakdown.critical_missing_requirements,
        review_url=f"/jobs/{job.id}",
    )
    try:
        provider.notify(notification)
    except Exception:
        logger.warning("Notification provider failed for job_id=%s", job.id, exc_info=True)
