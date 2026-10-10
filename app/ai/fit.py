""""Analyze with AI": sends the active resume and one job posting to the
configured AI provider for a fit assessment (`AIProvider.assess_fit`). Runs
whenever the user asks, independent of the `ai.enabled` toggle (which only
governs automatic, per-scan AI calls) -- but each (job, resume version) pair is
assessed at most once unless `force=True`, and every call is logged to
`AICallLog` for the AI Costs page.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.ai.base import AIProvider
from app.ai.factory import build_ai_provider
from app.ai.settings import AISettings
from app.config.schema import AppConfig
from app.matching.pipeline import NoActiveCandidateProfileError
from app.models.ai_usage import AICallLog
from app.models.candidate import CandidateProfile
from app.models.job import Job
from app.models.matching import JobFitAssessment
from app.utils.logging import log_event

logger = logging.getLogger(__name__)

_OPERATION = "assess_fit"


def active_candidate(session: Session) -> CandidateProfile | None:
    return (
        session.query(CandidateProfile)
        .filter_by(is_active=True)
        .order_by(CandidateProfile.version.desc())
        .first()
    )


def candidate_payload(candidate: CandidateProfile) -> dict:
    """The resume as structured data, plus any text the parser couldn't place
    (often a skills or education section with unusual headings) so the model
    still sees the whole resume.
    """
    return {
        "summary": candidate.summary,
        "skills": [s.name for s in candidate.skills],
        "employment": [
            {
                "title": e.title, "company": e.company, "location": e.location,
                "start": e.start_date, "end": "present" if e.is_current else e.end_date,
                "accomplishments": [b.text for b in e.bullets],
            }
            for e in candidate.employments
        ],
        "education": [
            {"institution": e.institution, "degree": e.degree, "field": e.field_of_study, "end": e.end_date}
            for e in candidate.education
        ],
        "certifications": [c.name for c in candidate.certifications],
        "projects": [{"name": p.name, "description": p.description} for p in candidate.projects],
        "other_resume_text": candidate.raw_unparsed_sections,
    }


def latest_assessment(session: Session, job_id: int) -> JobFitAssessment | None:
    return (
        session.query(JobFitAssessment)
        .filter_by(job_id=job_id)
        .order_by(JobFitAssessment.id.desc())
        .first()
    )


def assess_job_fit(
    session: Session, job: Job, config: AppConfig, *, provider: AIProvider | None = None, force: bool = False
) -> JobFitAssessment:
    """Raises NoActiveCandidateProfileError (no resume imported) or
    AIProviderConfigError (e.g. no OPENAI_API_KEY).
    """
    candidate = active_candidate(session)
    if candidate is None:
        raise NoActiveCandidateProfileError("Upload your resume first -- the analysis compares the job against it.")

    if not force:
        cached = (
            session.query(JobFitAssessment)
            .filter_by(job_id=job.id, candidate_profile_id=candidate.id)
            .order_by(JobFitAssessment.id.desc())
            .first()
        )
        if cached is not None:
            return cached

    provider = provider or build_ai_provider(config.ai, AISettings())
    result = provider.assess_fit(candidate_payload(candidate), job)
    usage = getattr(provider, "last_usage", None)

    assessment = JobFitAssessment(
        job_id=job.id, candidate_profile_id=candidate.id, provider=config.ai.provider,
        model=config.ai.model if config.ai.provider == "openai" else None,
        fit_score=result.fit_score, verdict=result.verdict, summary=result.summary,
        matched=result.matched, gaps=result.gaps, recommendation=result.recommendation,
    )
    session.add(assessment)
    session.add(AICallLog(
        provider=config.ai.provider, operation=_OPERATION, job_id=job.id,
        tokens_in=usage.tokens_in if usage else None, tokens_out=usage.tokens_out if usage else None,
        estimated_cost=usage.estimated_cost if usage else None, cache_hit=False,
    ))
    session.commit()
    log_event(logger, "JOB_FIT_ASSESSED", job_id=job.id, fit_score=result.fit_score, verdict=result.verdict)
    return assessment
