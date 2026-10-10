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
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
from app.documents.docx_edit import rewritable_passages, tailor_docx
from app.models.ai_usage import AICallLog
from app.models.candidate import AccomplishmentBullet, CandidateProfile
from app.models.job import Job, JobRequirements, JobStatus, JobStatusHistory
from app.models.resume import TailoredBulletProvenance, TailoredResume
from app.resume.validator import (
    BulletValidationResult, ValidationResult, validate_passage, validate_tailored_resume,
)
from app.utils.logging import log_event

logger = logging.getLogger(__name__)


class NoActiveCandidateProfileError(Exception):
    """Raised when tailoring is attempted before a resume has been imported."""


class BaseResumeMissingError(NoActiveCandidateProfileError):
    """The Word resume to tailor can't be found. Tailoring refuses rather than
    produce a resume in a different design."""


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


_UNSAFE_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def resume_download_name(config: AppConfig, candidate: CandidateProfile | None, company: str | None,
                         generated_at: datetime | None, suffix: str) -> str:
    """"First Last - Company - 2026-10-10_1432.docx": the name a tailored resume
    downloads as, so each one is recognisable in a Downloads folder or an upload
    form. The time is when it was generated, in this computer's time zone.
    """
    name = _resolve_candidate_name(config, candidate) if candidate else config.candidate.name
    name = " ".join((name or "").split())
    if name.isupper():
        name = name.title()  # "GLADIA JOY" -> "Gladia Joy"
    when = (generated_at or datetime.now(timezone.utc).replace(tzinfo=None)).replace(tzinfo=timezone.utc).astimezone()
    parts = [name or "Resume", company or "", when.strftime("%Y-%m-%d_%H%M")]
    cleaned = [" ".join(_UNSAFE_IN_FILENAME.sub(" ", part).split()) for part in parts]
    return " - ".join(part for part in cleaned if part)[:150] + suffix


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


RESTORED_DIR = PROJECT_ROOT / "data" / "resumes" / "restored"


def _base_docx(candidate: CandidateProfile) -> Path | None:
    """The candidate's original .docx: the file on disk (relative paths are taken
    from the project folder, so local runs and Docker both find it), else the copy
    kept in the database, restored to disk. None for a PDF resume.

    Raises BaseResumeMissingError for a Word resume that can't be found at all.
    """
    path = Path(candidate.raw_resume_path)
    if path.suffix.lower() != ".docx":
        return None
    for candidate_path in (path, PROJECT_ROOT / path):
        if candidate_path.is_file():
            return candidate_path
    if candidate.source_file:
        restored = RESTORED_DIR / f"resume_v{candidate.version}_{candidate.id}.docx"
        if not restored.is_file():
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(candidate.source_file)
        return restored
    raise BaseResumeMissingError(
        f"Your resume file ({path.name}) is missing, so it can't be tailored without changing its design. "
        "Upload it again on the My resume page."
    )


def write_resume_docx(
    docx_path: Path, config: AppConfig, candidate: CandidateProfile, *, address: AddressProfile | None,
    summary: str, skills: list[str], bullets: list[tuple[int | None, str]],
) -> None:
    """Write the tailored resume DOCX from final (already validated) text.
    `bullets` is (source bullet id, final text) for every bullet to show.

    With a .docx base resume, the candidate's own document is copied and only
    the tailored wording changes -- layout, fonts, tables and styles stay
    exactly as designed (never a different layout: a missing .docx raises
    BaseResumeMissingError). A PDF base resume has no Word layout to keep, so
    a clean one is laid out instead.
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
    if any(p.source_text is not None for p in provenance):  # tailored paragraph by paragraph
        candidate = (session.query(CandidateProfile).filter_by(is_active=True)
                     .order_by(CandidateProfile.version.desc()).first())
        base_docx = _base_docx(candidate) if candidate else None
        if base_docx is None:
            return None
        job = session.get(Job, tailored.job_id)
        docx_path = get_application_folder(job) / "tailored_resume.docx"
        _write_passages(docx_path, base_docx,
                        [(p.source_text, p.generated_text) for p in provenance if p.source_text and not p.removed],
                        resolve_address(config, job, session),
                        remove=[p.source_text for p in provenance if p.source_text and p.removed])
        tailored.docx_path = str(docx_path)
        session.commit()
        return docx_path
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
    base_docx = _base_docx(candidate)  # a missing Word resume fails here, before any AI spend

    provider = ai_provider or build_ai_provider(config.ai, AISettings())
    requirements = _resolve_requirements(session, job)
    address = resolve_address(config, job, session)

    if base_docx is not None:
        tailored_resume, docx_path = _tailor_passages(
            session, job, config, candidate, base_docx, provider, requirements, address)
        return _finish(session, job, tailored_resume, docx_path, update_status=update_status, rejected=sum(
            1 for row in tailored_resume.bullet_provenance if not row.validation_passed))

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
    return _finish(session, job, tailored_resume, docx_path, update_status=update_status,
                   rejected=sum(1 for b in validation.bullets if not b.accepted),
                   rejected_skills=len(validation.skills_rejected))


def _finish(session: Session, job: Job, tailored_resume: TailoredResume, docx_path: Path, *, update_status: bool,
            rejected: int, rejected_skills: int = 0) -> TailorResult:
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
        rejected_bullets=rejected,
        rejected_skills=rejected_skills,
    )

    session.commit()
    session.refresh(job)
    session.refresh(tailored_resume)
    return TailorResult(job=job, tailored_resume=tailored_resume, docx_path=docx_path)


def _resume_text(path: Path) -> str:
    """Everything the resume says (body, tables, text boxes, headers), for the
    truthfulness check: a rewrite may only name what's already in here."""
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(path))
    parts = [doc.element.body] + [s.header._element for s in doc.sections] + [s.footer._element for s in doc.sections]
    return "\n".join("".join(t.text or "" for t in part.iter(qn("w:t"))) for part in parts)


def _tailor_passages(
    session: Session, job: Job, config: AppConfig, candidate: CandidateProfile, base_docx: Path,
    provider: AIProvider, requirements: ExtractedJobRequirements | None, address: AddressProfile | None,
) -> tuple[TailoredResume, Path]:
    """Tailor a Word resume paragraph by paragraph, whatever its layout: every
    prose paragraph (summary lines, bullets, project responsibilities -- also in
    tables) is rewritten in its own place; names, headings, dates, contact
    lines and skill lists are never touched, and no paragraph is added, removed
    or moved. Each rewrite is checked like a bullet (no new numbers, names or
    titles, same length); a rejected one keeps the original wording.
    """
    passages = rewritable_passages(base_docx)
    resume_text = _resume_text(base_docx)
    skills = [s.name for s in candidate.skills]
    rewrites = provider.rewrite_passages(
        [{"id": i, "section": p.section, "text": p.text} for i, p in enumerate(passages)], job, requirements, skills,
    ) if passages else {}
    usage = getattr(provider, "last_usage", None)
    session.add(AICallLog(
        provider=config.ai.provider, operation="tailor_resume", job_id=job.id,
        tokens_in=usage.tokens_in if usage else None, tokens_out=usage.tokens_out if usage else None,
        estimated_cost=usage.estimated_cost if usage else None, cache_hit=False,
    ))

    results = []
    for i, passage in enumerate(passages):
        answer = rewrites.get(i)
        rewrite, drop_reason = (answer.get("text"), answer.get("drop_reason")) if isinstance(answer, dict) else (answer, None)
        text, reason = validate_passage(passage.text, rewrite, resume_text=resume_text, job_title=job.title)
        results.append((passage, text, reason, drop_reason))

    results = _sensible_suggestions(results)

    tailored_resume = TailoredResume(
        job_id=job.id,
        summary_text=candidate.summary or "",
        skills_order=skills,
        validation_status="PASSED" if all(reason is None for _, _, reason, _ in results) else "PARTIAL",
    )
    session.add(tailored_resume)
    session.flush()
    for passage, text, reason, drop_reason in results:
        session.add(TailoredBulletProvenance(
            tailored_resume_id=tailored_resume.id, generated_text=text, source_bullet_id=None,
            source_text=passage.text, section=(passage.section or "")[:255] or None,
            validation_passed=reason is None, rejection_reason=reason,
            suggest_remove=bool(drop_reason), remove_reason=(drop_reason or None) and drop_reason[:300],
        ))

    docx_path = get_application_folder(job) / "tailored_resume.docx"
    _write_passages(docx_path, base_docx, [(p.text, text) for p, text, _, _ in results], address)
    tailored_resume.docx_path = str(docx_path)
    session.flush()
    session.refresh(tailored_resume)
    return tailored_resume, docx_path


_SUMMARY_SECTION = re.compile(r"summary|profile|objective|about", re.I)


def _sensible_suggestions(results: list) -> list:
    """Keep the AI's removal suggestions only where acting on them leaves a sound
    resume: never the summary, and never every paragraph under one heading (that
    would leave an empty section -- the user can still remove those by hand)."""
    by_section: dict = {}
    for passage, _, _, drop in results:
        by_section.setdefault(passage.section, []).append(bool(drop))
    return [
        (passage, text, reason,
         None if (passage.section is None or _SUMMARY_SECTION.search(passage.section) or all(by_section[passage.section]))
         else drop)
        for passage, text, reason, drop in results
    ]


def _write_passages(docx_path: Path, base_docx: Path, replacements: list[tuple[str, str]],
                    address: AddressProfile | None, remove: list[str] = ()) -> None:
    """Write the tailored copy: `replacements` reworded, `remove` (approved by the
    user) taken out, everything else exactly as in `base_docx`."""
    report = tailor_docx(
        base_docx, docx_path, [(old, new) for old, new in replacements if old != new], remove=list(remove),
        location_line=(address.one_line() if address.has_street else f"{address.city}, {address.province}, {address.country}")
        if address else None,
        full_address=bool(address and address.has_street),
    )
    if report.not_found:
        logger.warning("Tailored resume: %d passages not found in %s, kept as written",
                       len(report.not_found), base_docx)
