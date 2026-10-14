"""The user-triggered analysis workflows behind the job pages' buttons:
"Analyze match" (keyword score, then the AI fit assessment) and
"Analyze & tailor" (score, then a tailored resume when the job qualifies).

Both return a message for the user instead of raising, because the pages show
the reason a job wasn't analyzed or tailored rather than an error page.
Callers wrap them in `quota.ai_action` where they count toward the hourly limit.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.integrations.ai.factory import AIProviderConfigError
from app.integrations.libreoffice import PdfConversionError, convert_to_pdf
from app.modules.applications.service import is_description_preview, replace_job_description
from app.modules.auth.models import User
from app.modules.jobs.models import Job, JobStatus
from app.modules.jobs.pool import service as pool
from app.modules.matching.fit import assess_job_fit, latest_assessment
from app.modules.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.modules.resumes.pipeline import generate_resume_bundle
from app.modules.resumes.tailor import BaseResumeMissingError, tailor_resume
from app.modules.resumes.tailor import NoActiveCandidateProfileError as TailorNoProfileError
from app.modules.usage import quota
from app.modules.usage.models import ActivityKind

logger = logging.getLogger(__name__)

# Shown instead of provider details, which stay server-side.
ANALYSIS_UNAVAILABLE = "Resume analysis is temporarily unavailable. Please try again later."


def use_full_description(session: Session, job: Job) -> None:
    """A job-board job still holding the ~500-character preview gets the full
    description from the job pool, when the pool has the same job, before
    it's analyzed."""
    if not is_description_preview(job):
        return
    full = pool.full_description_for(session, job.company, job.title)
    if full and len(full) > len(job.description or ""):
        try:
            replace_job_description(session, job, full)
        except ValueError:
            pass  # the pool's text didn't pass the description checks: keep the preview


def analyze_and_tailor(session: Session, job: Job, *, force: bool = False) -> str | None:
    """Score `job` and, if it qualifies (or `force`: "Tailor anyway"), generate
    its tailored resume.

    Returns:
        A human-readable reason when no resume was made (so the page can show
        it instead of leaving the job silently stuck at DISCOVERED), else None.
    """
    use_full_description(session, job)
    try:
        config = user_config(session)
        analysis = analyze_job(session, job, config)
        if not analysis.filter_result.passed and not force:
            return ("Skipped by your hard filters (" + "; ".join(analysis.filter_result.reasons)
                    + ") -- no tailored resume generated. Use \"Tailor anyway\" to make one.")
        if not analysis.qualified and not force:
            return (
                f"Scored {analysis.breakdown.overall_score}/100, below your minimum score "
                f"({config.matching.minimum_score}, Settings -> Matching) -- no tailored resume generated. "
                "Use \"Tailor anyway\" to make one."
            )
        if analysis.qualified:
            generate_resume_bundle(session, job, config)
        else:  # forced: tailor without moving the job through the qualified statuses
            result = tailor_resume(session, job, config, update_status=False)
            try:
                result.tailored_resume.pdf_path = str(convert_to_pdf(result.docx_path))
                session.commit()
            except PdfConversionError:
                pass  # DOCX is still there; PDF needs LibreOffice
    except ConfigError as exc:
        return f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
    except BaseResumeMissingError as exc:
        return str(exc)
    except (NoActiveCandidateProfileError, TailorNoProfileError):
        return "No resume yet -- upload yours on the My resume page (or below), then click \"Analyze & tailor\"."
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return ANALYSIS_UNAVAILABLE
    return None


def assess_fit(session: Session, job: Job, *, force: bool, user: User) -> str | None:
    """Score (free, deterministic) then run the AI fit assessment.

    Counts toward the user's hourly resume actions when it calls the provider
    (an existing assessment, not re-run, is free).

    Returns:
        A human-readable reason when the assessment couldn't run, else None.
    """
    if not force and latest_assessment(session, job.id) is not None:
        return None  # already analyzed: nothing to run
    use_full_description(session, job)
    try:
        config = user_config(session)
        with quota.ai_action(session, user, ActivityKind.AI_ANALYSIS, quota.job_label(job), job_id=job.id):
            if job.status == JobStatus.DISCOVERED:
                analyze_job(session, job, config)
            assess_job_fit(session, job, config, force=force)
    except quota.LimitReached as exc:
        return str(exc)
    except ConfigError as exc:
        return f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
    except NoActiveCandidateProfileError:
        return "No resume yet -- upload your resume below, then click \"Analyze match\"."
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return ANALYSIS_UNAVAILABLE
    except Exception:  # the provider call itself (network, quota, bad response)
        session.rollback()
        logger.exception("Match analysis failed")
        return "The analysis couldn't be completed. Please try again in a moment."
    return None
