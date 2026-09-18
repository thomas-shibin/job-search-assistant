"""Orchestrates resume tailoring (Milestone 6, spec §8): AI draft -> deterministic
truthfulness validation (`app/resume/validator.py`) -> DOCX rendering
(`app/documents/docx.py`). This is the one place `TailoredResume` rows get created,
and the one place the truthfulness guardrail is actually enforced end to end: every
bullet that ends up in the rendered resume is either the AI's own text (validated), a
verbatim fallback to its source bullet (validation failed), or simply absent (its
source_bullet_id didn't match anything real).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.ai.base import AIProvider, ExtractedJobRequirements
from app.ai.factory import build_ai_provider
from app.ai.settings import AISettings
from app.applications.folder import get_application_folder
from app.config.schema import AddressProfile, AppConfig
from app.documents.docx import render_tailored_resume
from app.models.candidate import CandidateProfile
from app.models.job import Job, JobRequirements, JobStatus, JobStatusHistory
from app.models.resume import TailoredBulletProvenance, TailoredResume
from app.resume.validator import validate_tailored_resume
from app.utils.logging import log_event

logger = logging.getLogger(__name__)


class NoActiveCandidateProfileError(Exception):
    """Raised when tailoring is attempted before a resume has been imported."""


@dataclass
class TailorResult:
    job: Job
    tailored_resume: TailoredResume
    docx_path: Path


def _resolve_requirements(session: Session, job: Job) -> ExtractedJobRequirements | None:
    row = session.query(JobRequirements).filter_by(job_id=job.id).first()
    if row is None:
        return None
    return ExtractedJobRequirements(
        required_skills=list(row.required_skills or []),
        preferred_skills=list(row.preferred_skills or []),
        seniority_level=row.seniority_level,
        min_years_experience=row.min_years_experience,
        remote_type_detected=row.remote_type_detected,
    )


def _resolve_address(config: AppConfig, job: Job) -> AddressProfile | None:
    """Address swap only applies to a job discovered under a search profile
    (spec: swap to the address matching where you're applying) -- a manually
    imported job with no search profile keeps no address override.
    """
    if not config.resume.change_address or not job.search_profile_name:
        return None
    search_profile = next((sp for sp in config.search_profiles if sp.name == job.search_profile_name), None)
    if search_profile is None:
        return None
    return config.address_profiles.get(search_profile.address_profile)


def tailor_resume(
    session: Session, job: Job, config: AppConfig, ai_provider: AIProvider | None = None
) -> TailorResult:
    candidate = (
        session.query(CandidateProfile)
        .filter_by(is_active=True)
        .order_by(CandidateProfile.version.desc())
        .first()
    )
    if candidate is None:
        raise NoActiveCandidateProfileError(
            "No active CandidateProfile found. Run 'import-resume' before generating a tailored resume."
        )

    provider = ai_provider or build_ai_provider(config.ai, AISettings())
    requirements = _resolve_requirements(session, job)
    address = _resolve_address(config, job)

    draft = provider.tailor_resume(candidate, job, requirements, address)
    validation = validate_tailored_resume(draft, candidate)

    final_summary = draft.summary if validation.summary_accepted else (candidate.summary or "")
    final_skills = validation.skills_accepted
    all_passed = (
        validation.summary_accepted
        and not validation.skills_rejected
        and all(b.accepted for b in validation.bullets)
    )

    tailored_resume = TailoredResume(
        job_id=job.id,
        summary_text=final_summary,
        skills_order=final_skills,
        validation_status="PASSED" if all_passed else "PARTIAL",
    )
    session.add(tailored_resume)
    session.flush()  # assign tailored_resume.id for provenance rows below

    bullet_to_employment = {b.id: emp for emp in candidate.employments for b in emp.bullets}
    bullet_order_index = {b.id: b.order_index for emp in candidate.employments for b in emp.bullets}
    bullets_by_employment_id: dict[int, list[tuple[int, str]]] = {}

    for result in validation.bullets:
        session.add(
            TailoredBulletProvenance(
                tailored_resume_id=tailored_resume.id,
                generated_text=result.text or "",
                source_bullet_id=result.source_bullet_id,
                validation_passed=result.accepted,
                rejection_reason=result.rejection_reason,
            )
        )
        if not result.text:
            continue  # dropped: source_bullet_id didn't match anything real
        emp = bullet_to_employment.get(result.source_bullet_id)
        if emp is not None:
            bullets_by_employment_id.setdefault(emp.id, []).append(
                (bullet_order_index[result.source_bullet_id], result.text)
            )

    ordered_employments = sorted(
        candidate.employments, key=lambda e: (e.is_current, e.start_date or date.min), reverse=True
    )
    employment_bullets = [
        (emp, [text for _, text in sorted(bullets_by_employment_id[emp.id])])
        for emp in ordered_employments
        if emp.id in bullets_by_employment_id
    ]

    folder = get_application_folder(job)
    docx_path = folder / "tailored_resume.docx"
    render_tailored_resume(
        docx_path,
        candidate_name=config.candidate.name,
        address=address,
        summary=final_summary,
        skills_order=final_skills,
        employment_bullets=employment_bullets,
        education=list(candidate.education),
        certifications=list(candidate.certifications),
    )
    tailored_resume.docx_path = str(docx_path)

    from_status = job.status
    job.status = JobStatus.RESUME_GENERATED
    session.add(
        JobStatusHistory(
            job_id=job.id,
            from_status=from_status,
            to_status=JobStatus.RESUME_GENERATED,
            reason=f"Tailored resume generated ({tailored_resume.validation_status})",
            actor="system",
        )
    )
    log_event(
        logger,
        "RESUME_GENERATED",
        job_id=job.id,
        tailored_resume_id=tailored_resume.id,
        validation_status=tailored_resume.validation_status,
        rejected_bullets=sum(1 for b in validation.bullets if not b.accepted),
        rejected_skills=len(validation.skills_rejected),
    )

    session.commit()
    session.refresh(job)
    session.refresh(tailored_resume)
    return TailorResult(job=job, tailored_resume=tailored_resume, docx_path=docx_path)
