"""Orchestrates AI-based job-requirement extraction with the caching
guarantee documented on `ExtractedJobRequirements` (spec §19): a job is never
sent to the AI provider twice. Two cache layers:

1. `JobRequirements` already exists for this exact job (e.g. `analyze` was
   run on it before) -- reuse it, no call.
2. Another job with the identical `description_hash` (the same posting,
   ingested as a separate `Job` row -- e.g. reposted by an agency and the
   employer both) already has `JobRequirements` -- copy them, no call.

Only if neither cache hits does the configured `AIProvider` get called, and
every outcome (including cache hits, at zero cost) is logged as an
`AICallLog` row so cache effectiveness stays visible per spec §19.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.ai.base import AIProvider, ExtractedJobRequirements
from app.ai.usage import AIUsage
from app.models.ai_usage import AICallLog
from app.models.job import Job, JobRequirements
from app.utils.logging import log_event

logger = logging.getLogger(__name__)

_OPERATION = "extract_job_requirements"


def _to_extracted(row: JobRequirements) -> ExtractedJobRequirements:
    return ExtractedJobRequirements(
        required_skills=list(row.required_skills or []),
        preferred_skills=list(row.preferred_skills or []),
        seniority_level=row.seniority_level,
        min_years_experience=row.min_years_experience,
        remote_type_detected=row.remote_type_detected,
    )


def _log_call(session: Session, job: Job, provider_name: str, usage: AIUsage | None, *, cache_hit: bool) -> None:
    session.add(
        AICallLog(
            provider=provider_name,
            operation=_OPERATION,
            job_id=job.id,
            tokens_in=usage.tokens_in if usage else None,
            tokens_out=usage.tokens_out if usage else None,
            estimated_cost=usage.estimated_cost if usage else None,
            cache_hit=cache_hit,
        )
    )


def get_or_extract_requirements(
    session: Session, job: Job, provider: AIProvider, provider_name: str
) -> tuple[ExtractedJobRequirements, bool]:
    """Returns `(requirements, cache_hit)`. Persists a new `JobRequirements`
    row (and an `AICallLog` row) as a side effect when there's no cache hit;
    does not commit -- the caller's transaction owns that.
    """
    existing = session.query(JobRequirements).filter_by(job_id=job.id).first()
    if existing is not None:
        _log_call(session, job, provider_name, usage=None, cache_hit=True)
        return _to_extracted(existing), True

    reusable = (
        session.query(JobRequirements)
        .join(Job, JobRequirements.job_id == Job.id)
        .filter(Job.description_hash == job.description_hash, Job.id != job.id)
        .first()
    )
    if reusable is not None:
        extracted = _to_extracted(reusable)
        session.add(
            JobRequirements(
                job_id=job.id,
                required_skills=extracted.required_skills,
                preferred_skills=extracted.preferred_skills,
                seniority_level=extracted.seniority_level,
                min_years_experience=extracted.min_years_experience,
                remote_type_detected=extracted.remote_type_detected,
            )
        )
        _log_call(session, job, provider_name, usage=None, cache_hit=True)
        log_event(logger, "JOB_REQUIREMENTS_CACHE_HIT", job_id=job.id, reused_from_job_id=reusable.job_id)
        return extracted, True

    extracted = provider.extract_job_requirements(job.description)
    session.add(
        JobRequirements(
            job_id=job.id,
            required_skills=extracted.required_skills,
            preferred_skills=extracted.preferred_skills,
            seniority_level=extracted.seniority_level,
            min_years_experience=extracted.min_years_experience,
            remote_type_detected=extracted.remote_type_detected,
        )
    )
    usage = getattr(provider, "last_usage", None)
    _log_call(session, job, provider_name, usage=usage, cache_hit=False)
    log_event(logger, "JOB_REQUIREMENTS_EXTRACTED", job_id=job.id, provider=provider_name)
    return extracted, False
