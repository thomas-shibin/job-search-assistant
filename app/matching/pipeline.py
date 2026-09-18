"""Orchestrates hard filters + deterministic scoring for a single job (spec
sections 9/10/14/22): the one place `MatchAnalysis` rows get created and
`Job.status` advances from DISCOVERED -> ANALYZED -> (optionally) QUALIFIED.

No AI call happens here -- see docs/ARCHITECTURE.md's deterministic-vs-AI
table. Milestone 5 adds AI-based requirement extraction *before* this stage
(populating `JobRequirements`), which this pipeline will start consuming once
it exists; scoring itself stays deterministic either way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config.schema import AppConfig, SearchProfile
from app.ingestion.filters import FilterResult, check_hard_filters
from app.matching.scorer import MatchScoreBreakdown, score_job
from app.models.candidate import CandidateProfile
from app.models.job import Job, JobStatus, JobStatusHistory
from app.models.matching import MatchAnalysis
from app.utils.logging import log_event

logger = logging.getLogger(__name__)


class NoActiveCandidateProfileError(Exception):
    """Raised when analysis is attempted before a resume has been imported."""


@dataclass
class AnalyzeResult:
    job: Job
    filter_result: FilterResult
    breakdown: MatchScoreBreakdown | None
    qualified: bool


def _find_search_profile(config: AppConfig, job: Job) -> SearchProfile | None:
    if not job.search_profile_name:
        return None
    return next((sp for sp in config.search_profiles if sp.name == job.search_profile_name), None)


def _transition(session: Session, job: Job, to_status: JobStatus, reason: str) -> None:
    from_status = job.status
    job.status = to_status
    session.add(
        JobStatusHistory(job_id=job.id, from_status=from_status, to_status=to_status, reason=reason, actor="system")
    )


def analyze_job(session: Session, job: Job, config: AppConfig) -> AnalyzeResult:
    """Run hard filters then, if they pass, deterministic scoring for `job`.

    A job that fails a hard filter is left at its current status with no
    `MatchAnalysis` row -- filtering is a "don't spend effort on this yet"
    signal, not a user decision, so it does not move the job to a terminal
    state like SKIPPED (only the user does that, per spec section 15).
    """
    candidate = (
        session.query(CandidateProfile)
        .filter_by(is_active=True)
        .order_by(CandidateProfile.version.desc())
        .first()
    )
    if candidate is None:
        raise NoActiveCandidateProfileError(
            "No active CandidateProfile found. Run 'import-resume' before analyzing jobs."
        )

    search_profile = _find_search_profile(config, job)

    filter_result = check_hard_filters(job, config.matching, search_profile)
    if not filter_result.passed:
        session.commit()
        return AnalyzeResult(job=job, filter_result=filter_result, breakdown=None, qualified=False)

    breakdown = score_job(candidate, job, search_profile, config.matching)

    session.add(
        MatchAnalysis(
            job_id=job.id,
            overall_score=breakdown.overall_score,
            technical_score=breakdown.technical_score,
            experience_score=breakdown.experience_score,
            title_score=breakdown.title_score,
            seniority_score=breakdown.seniority_score,
            platform_score=breakdown.platform_score,
            location_score=breakdown.location_score,
            domain_score=breakdown.domain_score,
            education_score=breakdown.education_score,
            strong_matches=breakdown.strong_matches,
            partial_matches=breakdown.partial_matches,
            missing_requirements=breakdown.missing_requirements,
            critical_missing_requirements=breakdown.critical_missing_requirements,
            explanation=breakdown.explanation,
            weights_snapshot=config.matching.weights.model_dump(),
        )
    )

    _transition(session, job, JobStatus.ANALYZED, reason=f"Scored {breakdown.overall_score}/100")
    log_event(logger, "JOB_ANALYZED", job_id=job.id, overall_score=breakdown.overall_score)

    qualified = breakdown.overall_score >= config.matching.minimum_score
    if qualified:
        _transition(
            session,
            job,
            JobStatus.QUALIFIED,
            reason=f"Score {breakdown.overall_score} >= minimum_score {config.matching.minimum_score}",
        )
        log_event(logger, "JOB_QUALIFIED", job_id=job.id, overall_score=breakdown.overall_score)

    session.commit()
    session.refresh(job)
    return AnalyzeResult(job=job, filter_result=filter_result, breakdown=breakdown, qualified=qualified)
