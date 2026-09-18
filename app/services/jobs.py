"""Job listing/detail/action logic shared by `app/api/` (JSON) and `app/ui/`
(the HTML/HTMX dashboard) -- per docs/ARCHITECTURE.md's frontend-choice note,
both talk to the same services layer so a future non-HTML client needs no
backend rework.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.candidate import AccomplishmentBullet
from app.models.job import Job, JobStatus
from app.models.matching import MatchAnalysis
from app.models.resume import TailoredBulletProvenance, TailoredResume


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


@dataclass
class JobDetail:
    job: Job
    match_analysis: MatchAnalysis | None
    tailored_resume: TailoredResume | None
    bullet_diffs: list[BulletDiff] = field(default_factory=list)


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
    if tailored_resume is not None:
        provenance = (
            session.query(TailoredBulletProvenance).filter_by(tailored_resume_id=tailored_resume.id).all()
        )
        for p in provenance:
            source_bullet = session.get(AccomplishmentBullet, p.source_bullet_id) if p.source_bullet_id else None
            bullet_diffs.append(
                BulletDiff(
                    source_text=source_bullet.text if source_bullet else None,
                    generated_text=p.generated_text,
                    validation_passed=p.validation_passed,
                    rejection_reason=p.rejection_reason,
                )
            )

    return JobDetail(job=job, match_analysis=match_analysis, tailored_resume=tailored_resume, bullet_diffs=bullet_diffs)
