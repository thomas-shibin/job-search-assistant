"""Rendering a tracked job's page (/jobs/{id}): the detail, its next status
actions, the match analysis and the tailored resume. Shared by the
applications, matching and resumes routes, which all land back on this page."""
from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.modules.applications.service import get_job_detail, is_description_preview
from app.modules.jobs.models import Job, JobStatus
from app.modules.matching.fit import active_candidate, latest_assessment
from app.modules.resumes.tailor import resolve_address
from app.modules.usage import quota
from app.web.templating import templates

# (url path segment, state_machine function name, human label) offered per status
ACTIONS_BY_STATUS: dict[JobStatus, list[tuple[str, str, str]]] = {
    JobStatus.READY_FOR_REVIEW: [("approve", "approve", "Approve"), ("skip", "skip", "Skip")],
    JobStatus.APPROVED: [
        ("open-application", "mark_application_opened", "Open Application"),
        ("skip", "skip", "Skip"),
    ],
    JobStatus.APPLICATION_OPENED: [("mark-applied", "mark_applied", "Mark Applied"), ("skip", "skip", "Skip")],
    JobStatus.APPLIED: [
        ("mark-interview", "mark_interview", "Mark Interview"),
        ("mark-rejected", "mark_rejected", "Mark Rejected"),
        ("archive", "archive", "Archive"),
    ],
    JobStatus.INTERVIEW: [
        ("mark-offer", "mark_offer", "Mark Offer"),
        ("mark-rejected", "mark_rejected", "Mark Rejected"),
        ("archive", "archive", "Archive"),
    ],
    JobStatus.OFFER: [("archive", "archive", "Archive")],
}


def actions_for(job: Job) -> list[dict]:
    return [{"path": path, "label": label} for path, _, label in ACTIONS_BY_STATUS.get(job.status, [])]


def get_job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    return job


def job_detail_response(request: Request, session: Session, job_id: int, *, error: str | None = None,
                        message: str | None = None):
    """The job's page, optionally with an error or message on top (an action that
    failed renders it in place rather than redirecting)."""
    detail = get_job_detail(session, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    resume_address = None
    try:
        minimum_score = user_config(session).matching.minimum_score
    except ConfigError:
        minimum_score = None
    if detail.tailored_resume is not None:
        try:
            resume_address = resolve_address(user_config(session), detail.job, session)
        except ConfigError:
            pass
    return templates.TemplateResponse(
        request, "job_detail.html",
        {
            "resume_address": resume_address,
            "description_preview": is_description_preview(detail.job),
            "detail": detail, "actions": actions_for(detail.job), "error": error, "message": message,
            "fit": latest_assessment(session, job_id), "has_resume": active_candidate(session) is not None,
            "minimum_score": minimum_score,
            "ai_allowance": quota.ai_allowance_for(session, getattr(request.state, "user", None)),
        },
    )
