"""Orchestrates normalization + dedup for a single discovered job (spec
section 6/22). This is the one place `Job` rows get created, so every
discovery path (manual import today; Greenhouse/Lever/email adapters later)
funnels through here and gets identical dedup + audit-trail behavior.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.integrations.job_boards.base import RawJob
from app.modules.jobs.ingestion.fingerprint import compute_description_hash, compute_fingerprint, find_duplicate
from app.modules.jobs.ingestion.normalize import normalize_raw_job
from app.modules.jobs.models import Job, JobStatus, JobStatusHistory

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    job: Job
    created: bool  # False means `job` is a pre-existing duplicate, not newly inserted


def ingest_raw_job(session: Session, raw: RawJob) -> IngestResult:
    normalized = normalize_raw_job(raw)

    duplicate = find_duplicate(session, raw, normalized)
    if duplicate is not None:
        log_event(
            logger,
            "JOB_DUPLICATE",
            job_id=duplicate.id,
            source=raw.source,
            company=normalized["company"],
            title=normalized["title"],
        )
        return IngestResult(job=duplicate, created=False)

    job = Job(
        external_id=normalized["external_id"],
        source=normalized["source"],
        source_url=normalized["source_url"],
        company=normalized["company"],
        title=normalized["title"],
        location=normalized["location"],
        remote_type=normalized["remote_type"],
        salary_min=normalized["salary_min"],
        salary_max=normalized["salary_max"],
        currency=normalized["currency"],
        employment_type=normalized["employment_type"],
        description=normalized["description"],
        date_posted=normalized["date_posted"],
        status=JobStatus.DISCOVERED,
        fingerprint=compute_fingerprint(normalized["company"], normalized["title"], normalized["location"]),
        description_hash=compute_description_hash(normalized["description"]),
    )
    session.add(job)
    session.flush()  # assign job.id for the history row below

    session.add(
        JobStatusHistory(
            job_id=job.id,
            from_status=None,
            to_status=JobStatus.DISCOVERED,
            reason="Discovered via ingestion pipeline",
            actor="system",
        )
    )
    session.commit()
    session.refresh(job)

    log_event(
        logger,
        "JOB_DISCOVERED",
        job_id=job.id,
        source=job.source,
        company=job.company,
        title=job.title,
    )
    return IngestResult(job=job, created=True)
