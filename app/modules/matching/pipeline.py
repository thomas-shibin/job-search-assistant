"""Orchestrates hard filters + deterministic scoring for a single job (spec
sections 9/10/14/22): the one place `MatchAnalysis` rows get created and
`Job.status` advances from DISCOVERED -> ANALYZED -> (optionally) QUALIFIED.

Scoring itself stays deterministic (see docs/ARCHITECTURE.md's
deterministic-vs-AI table) -- but as of Milestone 5, when `config.ai.enabled`,
AI-based requirement extraction runs first (`app/ai/service.py`, cached so an
unchanged job is never re-billed) and its output extends what the scorer can
recognize beyond the user's configured skill lists. `config.ai.
generate_match_explanations` (off by default) optionally replaces the
deterministic explanation text with an AI-polished version; if that call
fails, the deterministic explanation is kept rather than failing the whole
analysis, since it's polish, not a required input.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config.schema import AppConfig, SearchProfile
from app.core.exceptions import AppError
from app.core.logging import log_event
from app.integrations.ai.base import AIProvider
from app.integrations.ai.factory import build_ai_provider
from app.integrations.ai.settings import AISettings
from app.modules.jobs.ingestion.filters import FilterResult, check_hard_filters
from app.modules.jobs.models import Job, JobStatus, JobStatusHistory
from app.modules.matching.models import MatchAnalysis
from app.modules.matching.requirements import get_or_extract_requirements
from app.modules.matching.scorer import MatchScoreBreakdown, score_job
from app.modules.resumes.models import CandidateProfile
from app.modules.usage.ai_calls import AICallLog

logger = logging.getLogger(__name__)


class NoActiveCandidateProfileError(AppError):
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


def _scoring_profile(config: AppConfig, search_profile: SearchProfile | None) -> SearchProfile | None:
    """Jobs found by the global search (or tracked from the dashboard) have no
    search profile; score their title/location against the global `search`
    conditions instead. Hard filters still only use a real profile, so a job
    you picked by hand is never filtered out for its location.
    """
    if search_profile is not None:
        return search_profile
    if not (config.search.titles or config.search.locations):
        return None
    return SearchProfile(
        name="Global search", address_profile="", titles=config.search.titles, locations=config.search.locations
    )


def _transition(session: Session, job: Job, to_status: JobStatus, reason: str) -> None:
    from_status = job.status
    job.status = to_status
    session.add(
        JobStatusHistory(job_id=job.id, from_status=from_status, to_status=to_status, reason=reason, actor="system")
    )


def _get_ai_provider(config: AppConfig, ai_provider: AIProvider | None) -> AIProvider | None:
    if ai_provider is not None:
        return ai_provider
    if config.ai.enabled or config.ai.generate_match_explanations:
        return build_ai_provider(config.ai, AISettings())
    return None


def analyze_job(
    session: Session, job: Job, config: AppConfig, ai_provider: AIProvider | None = None, *,
    update_status: bool = True,
) -> AnalyzeResult:
    """Run hard filters then, if they pass, deterministic scoring for `job`.

    A job that fails a hard filter is left at its current status with no
    `MatchAnalysis` row -- filtering is a "don't spend effort on this yet"
    signal, not a user decision, so it does not move the job to a terminal
    state like SKIPPED (only the user does that, per spec section 15).

    `ai_provider` lets a caller (e.g. the CLI, batching over many jobs) build
    one `AIProvider` instance up front and reuse it; passing `None` builds
    one from `config.ai` on demand (and only if `config.ai.enabled` or
    `config.ai.generate_match_explanations` actually needs one).

    `update_status=False` re-scores without moving the job's status -- e.g.
    after its description was replaced on a job you've already applied to.
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

    provider = _get_ai_provider(config, ai_provider)

    requirements = None
    if config.ai.enabled:
        requirements, cache_hit = get_or_extract_requirements(session, job, provider, config.ai.provider)
        log_event(
            logger, "JOB_REQUIREMENTS_READY", job_id=job.id, provider=config.ai.provider, cache_hit=cache_hit
        )

    breakdown = score_job(candidate, job, _scoring_profile(config, search_profile), config.matching, requirements)

    if config.ai.generate_match_explanations:
        try:
            polished = provider.generate_match_explanation(breakdown)
        except Exception:
            polished = None
            logger.warning("AI match-explanation polish failed for job_id=%s; keeping deterministic text", job.id)
        if polished:
            breakdown = breakdown.model_copy(update={"explanation": polished})
            usage = getattr(provider, "last_usage", None)
            session.add(
                AICallLog(
                    provider=config.ai.provider,
                    operation="generate_match_explanation",
                    job_id=job.id,
                    tokens_in=usage.tokens_in if usage else None,
                    tokens_out=usage.tokens_out if usage else None,
                    estimated_cost=usage.estimated_cost if usage else None,
                    cache_hit=False,
                )
            )

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

    if update_status:
        _transition(session, job, JobStatus.ANALYZED, reason=f"Scored {breakdown.overall_score}/100")
    log_event(logger, "JOB_ANALYZED", job_id=job.id, overall_score=breakdown.overall_score)

    qualified = breakdown.overall_score >= config.matching.minimum_score
    if qualified and update_status:
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
