"""Job listing/detail/action logic shared by `app/api/` (JSON) and `app/ui/`
(the HTML/HTMX dashboard) -- per docs/ARCHITECTURE.md's frontend-choice note,
both talk to the same services layer so a future non-HTML client needs no
backend rework.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.modules.resumes.models import AccomplishmentBullet
from app.modules.jobs.models import Job, JobStatus
from app.modules.matching.models import MatchAnalysis
from app.modules.resumes.tailoring_models import TailoredBulletProvenance, TailoredResume


def latest_match_analysis(session: Session, job_id: int) -> MatchAnalysis | None:
    return (
        session.query(MatchAnalysis)
        .filter_by(job_id=job_id)
        .order_by(MatchAnalysis.scored_at.desc())
        .first()
    )


def list_jobs(session: Session, *, status: str | None = None, min_score: int | None = None) -> list[Job]:
    query = session.query(Job)
    if status:
        query = query.filter(Job.status == JobStatus(status))
    jobs = query.order_by(Job.date_discovered.desc()).all()

    if min_score is not None:
        jobs = [j for j in jobs if (score := latest_match_analysis(session, j.id)) and score.overall_score >= min_score]

    return jobs


@dataclass
class BulletDiff:
    source_text: str | None
    generated_text: str
    validation_passed: bool
    rejection_reason: str | None
    role: str | None = None  # "Senior Software Engineer · AOT Technologies"
    id: int | None = None  # the provenance row (paragraph tailoring: what a removal refers to)
    suggest_remove: bool = False  # the AI thinks this paragraph is irrelevant to the job
    remove_reason: str | None = None
    removed: bool = False  # taken out of the resume (the user approved)

    @property
    def changed(self) -> bool:
        """Reworded in the resume. A bullet with no source (a rejected, made-up
        bullet) never reaches the resume, so it isn't a change."""
        if self.source_text is None:
            return False
        return " ".join(self.source_text.split()) != " ".join(self.generated_text.split())


@dataclass
class JobDetail:
    job: Job
    match_analysis: MatchAnalysis | None
    tailored_resume: TailoredResume | None
    bullet_diffs: list[BulletDiff] = field(default_factory=list)  # in resume order
    original_summary: str | None = None  # the base resume's summary the tailored one replaced

    @property
    def bullet_groups(self) -> list[tuple[str | None, list[BulletDiff]]]:
        """Bullet diffs grouped by role, in resume order."""
        groups: list[tuple[str | None, list[BulletDiff]]] = []
        for diff in self.bullet_diffs:
            if groups and groups[-1][0] == diff.role:
                groups[-1][1].append(diff)
            else:
                groups.append((diff.role, [diff]))
        return groups


def get_job_detail(session: Session, job_id: int) -> JobDetail | None:
    job = session.get(Job, job_id)
    if job is None:
        return None

    match_analysis = latest_match_analysis(session, job_id)
    tailored_resume = (
        session.query(TailoredResume)
        .filter_by(job_id=job_id)
        .order_by(TailoredResume.generated_at.desc())
        .first()
    )

    bullet_diffs: list[BulletDiff] = []
    original_summary: str | None = None
    if tailored_resume is not None:
        provenance = (
            session.query(TailoredBulletProvenance).filter_by(tailored_resume_id=tailored_resume.id).all()
        )
        ordered: list[tuple[tuple, BulletDiff]] = []
        for p in provenance:
            source_bullet = session.get(AccomplishmentBullet, p.source_bullet_id) if p.source_bullet_id else None
            employment = source_bullet.employment if source_bullet else None
            if employment is not None and original_summary is None:
                original_summary = employment.candidate_profile.summary
            # Resume order: current role first, then newest start date; bullets as written.
            sort_key = (
                (0, not employment.is_current, -(employment.start_date.toordinal() if employment.start_date else 0),
                 employment.id, source_bullet.order_index)
                if employment is not None else (1,)
            )
            if p.source_text is not None:  # a paragraph of a Word resume, in document order
                sort_key = (0, 0, 0, 0, p.id)
            ordered.append((sort_key, BulletDiff(
                source_text=source_bullet.text if source_bullet else p.source_text,
                generated_text=p.generated_text,
                validation_passed=p.validation_passed,
                rejection_reason=p.rejection_reason,
                role=f"{employment.title} · {employment.company}" if employment is not None else p.section,
                id=p.id if p.source_text is not None else None,
                suggest_remove=bool(p.suggest_remove), remove_reason=p.remove_reason, removed=bool(p.removed),
            )))
        bullet_diffs = [diff for _, diff in sorted(ordered, key=lambda item: item[0])]

    return JobDetail(job=job, match_analysis=match_analysis, tailored_resume=tailored_resume,
                     bullet_diffs=bullet_diffs, original_summary=original_summary)


# Adzuna's API shares at most this many characters of a description; the full
# text is only on the posting page (which Adzuna doesn't allow fetching).
ADZUNA_PREVIEW_CHARS = 500


def is_description_preview(job: Job) -> bool:
    """True when `job.description` is (probably) Adzuna's cut-off preview."""
    return job.source == "adzuna" and len(job.description or "") >= ADZUNA_PREVIEW_CHARS - 10


def replace_job_description(session: Session, job: Job, text: str) -> None:
    """Replace a job's description (e.g. Adzuna's preview with the full posting
    pasted by the user) and drop everything derived from the old text, so the
    next scoring / AI analysis / tailoring reads the new one.
    """
    from app.modules.jobs.ingestion.fingerprint import compute_description_hash
    from app.modules.jobs.models import JobRequirements

    cleaned = "\n".join(line.rstrip() for line in text.strip().splitlines())
    if len(cleaned) < 50:
        raise ValueError("That's too short to be a job description -- paste the full posting text.")
    job.description = cleaned
    job.description_hash = compute_description_hash(cleaned)
    session.query(JobRequirements).filter_by(job_id=job.id).delete()
    session.commit()
