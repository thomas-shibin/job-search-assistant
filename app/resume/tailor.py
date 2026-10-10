"""Orchestrates resume tailoring (Milestone 6, spec §8): AI draft -> deterministic
truthfulness validation (`app/resume/validator.py`) -> DOCX: a copy of the
candidate's own .docx with only the tailored wording changed
(`app/documents/docx_edit.py`), or a fresh layout (`app/documents/docx.py`) when
the base resume is a PDF. This is the one place `TailoredResume` rows get created,
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
from app.catalog.geo import job_place, province_code
from app.config.schema import AddressProfile, AppConfig
from app.database.session import PROJECT_ROOT
from app.documents.docx import render_tailored_resume
from app.documents.docx_edit import tailor_docx
from app.models.ai_usage import AICallLog
from app.models.candidate import AccomplishmentBullet, CandidateProfile
from app.models.job import Job, JobRequirements, JobStatus, JobStatusHistory
from app.models.resume import TailoredBulletProvenance, TailoredResume
from app.resume.validator import BulletValidationResult, ValidationResult, validate_tailored_resume
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


def _resolve_candidate_name(config: AppConfig, candidate: CandidateProfile) -> str:
    """`candidate.full_name` (parsed off the resume's own header) wins by
    default, since config.yaml's `candidate.name` commonly gets left at its
    placeholder value. `candidate.override_parsed_name: true` inverts that,
    for a deliberately different name/spelling than what's on the file.
    """
    if config.candidate.override_parsed_name:
        return config.candidate.name
    return candidate.full_name or config.candidate.name


def resolve_address(config: AppConfig, job: Job, session: Session | None = None) -> AddressProfile | None:
    """The address to show on this job's resume (`resume.change_address`), first match wins:

    1. the address profile of the search profile that found the job;
    2. the address profile in the job's province -- as its description states it
       ("Location: Vancouver"), else as listed ("Toronto, Ontario");
    3. your only address profile, if you have exactly one;
    4. with no address profiles at all, the job's own city and province.

    None keeps the resume's own location line (e.g. a remote job, or several
    profiles and none in the job's province).
    """
    if not config.resume.change_address:
        return None
    profiles = config.address_profiles
    if job.search_profile_name:
        search_profile = next((sp for sp in config.search_profiles if sp.name == job.search_profile_name), None)
        if search_profile is not None and search_profile.address_profile in profiles:
            return profiles[search_profile.address_profile]

    place = job_place(session, job.description, job.location)
    if place is not None:
        city, province = place
        match = next((p for p in profiles.values() if province_code(p.province) == province), None)
        if match is not None:
            return match
    if len(profiles) == 1:
        return next(iter(profiles.values()))
    if not profiles and place is not None and place[0]:
        return AddressProfile(city=place[0], province=place[1], country="Canada")
    return None


def _backfill_omitted_content(validation: ValidationResult, candidate: CandidateProfile) -> None:
    """Completeness safety net: nothing on the candidate's real profile
    should silently vanish just because the AI chose to omit it as
    "irrelevant" to this job (a real, documented failure mode -- an AI asked
    to tailor a resume will sometimes drop whole roles/skills rather than
    just reordering/re-emphasizing). This isn't a truthfulness problem (the
    validator already handles that), so anything backfilled here is added as
    `accepted=True` verbatim, not flagged as a validation failure.
    """
    covered_bullet_ids = {b.source_bullet_id for b in validation.bullets if b.source_bullet_id is not None}
    for emp in candidate.employments:
        for bullet in emp.bullets:
            if bullet.id not in covered_bullet_ids:
                validation.bullets.append(BulletValidationResult(text=bullet.text, source_bullet_id=bullet.id, accepted=True))

    covered_skills_lower = {s.lower() for s in validation.skills_accepted}
    for skill in candidate.skills:
        if skill.name.lower() not in covered_skills_lower:
            validation.skills_accepted.append(skill.name)
            covered_skills_lower.add(skill.name.lower())


def _base_docx(candidate: CandidateProfile) -> Path | None:
    """The candidate's original .docx, if it's still on disk (relative paths are
    taken from the project folder, so local runs and Docker both find it).
    """
    path = Path(candidate.raw_resume_path)
    for candidate_path in (path, PROJECT_ROOT / path):
        if candidate_path.suffix.lower() == ".docx" and candidate_path.is_file():
            return candidate_path
    return None


def write_resume_docx(
    docx_path: Path, config: AppConfig, candidate: CandidateProfile, *, address: AddressProfile | None,
    summary: str, skills: list[str], bullets: list[tuple[int | None, str]],
) -> None:
    """Write the tailored resume DOCX from final (already validated) text.
    `bullets` is (source bullet id, final text) for every bullet to show.

    With a .docx base resume, the candidate's own document is copied and only
    the tailored wording changes -- layout, fonts, tables and styles stay
    exactly as designed. A PDF base resume has no Word layout to keep, so a
    clean one is laid out instead.
    """
    base_docx = _base_docx(candidate)
    if base_docx is not None:
        source_text = {b.id: b.text for emp in candidate.employments for b in emp.bullets}
        replacements = [(source_text[bullet_id], text) for bullet_id, text in bullets if bullet_id in source_text]
        if candidate.summary and summary:
            replacements.insert(0, (candidate.summary, summary))
        report = tailor_docx(
            base_docx, docx_path, replacements,
            location_line=(address.one_line() if address.has_street else f"{address.city}, {address.province}, {address.country}")
            if address else None,
            full_address=bool(address and address.has_street),
        )
        if report.not_found:
            logger.warning("Tailored resume: %d passages not found in %s, kept as written",
                           len(report.not_found), base_docx)
        return

    bullet_to_employment = {b.id: emp for emp in candidate.employments for b in emp.bullets}
    bullet_order_index = {b.id: b.order_index for emp in candidate.employments for b in emp.bullets}
    bullets_by_employment_id: dict[int, list[tuple[int, str]]] = {}
    for bullet_id, text in bullets:
        emp = bullet_to_employment.get(bullet_id)
        if emp is not None:
            bullets_by_employment_id.setdefault(emp.id, []).append((bullet_order_index[bullet_id], text))
    ordered_employments = sorted(
        candidate.employments, key=lambda e: (e.is_current, e.start_date or date.min), reverse=True
    )
    render_tailored_resume(
        docx_path,
        candidate_name=_resolve_candidate_name(config, candidate),
        contact_line=candidate.contact_line,
        address=address,
        summary=summary,
        skills_order=skills,
        employment_bullets=[
            (emp, [text for _, text in sorted(bullets_by_employment_id[emp.id])])
            for emp in ordered_employments if emp.id in bullets_by_employment_id
        ],
        education=list(candidate.education),
        certifications=list(candidate.certifications),
    )


def rebuild_tailored_resume(session: Session, tailored: TailoredResume, config: AppConfig) -> Path | None:
    """Re-write an existing tailored resume's DOCX from its saved wording (no AI
    call) -- e.g. after the base resume's design changes. Returns the path, or
    None if its source bullets no longer exist.
    """
    provenance = session.query(TailoredBulletProvenance).filter_by(tailored_resume_id=tailored.id).all()
    source_ids = [p.source_bullet_id for p in provenance if p.source_bullet_id is not None]
    first = session.get(AccomplishmentBullet, source_ids[0]) if source_ids else None
    if first is None:
        return None
    candidate = first.employment.candidate_profile  # the profile version it was tailored from
    job = session.get(Job, tailored.job_id)
    docx_path = get_application_folder(job) / "tailored_resume.docx"  # stable per job, wherever the app runs
    write_resume_docx(
        docx_path, config, candidate, address=resolve_address(config, job, session), summary=tailored.summary_text or "",
        skills=list(tailored.skills_order or []),
        bullets=[(p.source_bullet_id, p.generated_text) for p in provenance if p.generated_text],
    )
    tailored.docx_path = str(docx_path)
    session.commit()
    return docx_path


def tailor_resume(
    session: Session, job: Job, config: AppConfig, ai_provider: AIProvider | None = None, *,
    update_status: bool = True,
) -> TailorResult:
    """Tailor `job`'s resume. `update_status=False` re-tailors without touching
    the job's status (e.g. a job you've already applied to)."""
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
    address = resolve_address(config, job, session)

    draft = provider.tailor_resume(candidate, job, requirements, address)
    usage = getattr(provider, "last_usage", None)
    session.add(AICallLog(
        provider=config.ai.provider, operation="tailor_resume", job_id=job.id,
        tokens_in=usage.tokens_in if usage else None, tokens_out=usage.tokens_out if usage else None,
        estimated_cost=usage.estimated_cost if usage else None, cache_hit=False,
    ))
    validation = validate_tailored_resume(draft, candidate)
    _backfill_omitted_content(validation, candidate)

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

    docx_path = get_application_folder(job) / "tailored_resume.docx"
    write_resume_docx(
        docx_path, config, candidate, address=address, summary=final_summary, skills=final_skills,
        bullets=[(r.source_bullet_id, r.text) for r in validation.bullets if r.text],
    )
    tailored_resume.docx_path = str(docx_path)

    if update_status:
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
