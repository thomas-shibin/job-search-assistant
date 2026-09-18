"""HTML/HTMX dashboard routes (Milestone 9): job list with filters, match
detail + resume diff view, and the approve/skip/open-application/... actions
from `app/applications/state_machine.py`. `hx-boost` on <body> (see
base.html) is what gives normal links/forms AJAX-style partial swaps without
a separate template per HTMX fragment -- it degrades to plain navigation with
JS disabled.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.applications import state_machine
from app.database.session import get_session
from app.models.job import Job, JobStatus
from app.services.costs import ai_cost_summary
from app.services.jobs import get_job_detail, latest_match_analysis, list_jobs
from app.ui.diff import inline_word_diff_html

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["worddiff"] = inline_word_diff_html

# (url path segment, state_machine function name, human label, status this action requires)
_ACTIONS_BY_STATUS: dict[JobStatus, list[tuple[str, str, str]]] = {
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


def _actions_for(job: Job) -> list[dict]:
    return [{"path": path, "label": label} for path, _, label in _ACTIONS_BY_STATUS.get(job.status, [])]


def _get_job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    return job


@router.get("/")
def job_list_page(
    request: Request, status: str | None = None, min_score: int | None = None, session: Session = Depends(get_session)
):
    jobs = list_jobs(session, status=status, min_score=min_score)
    rows = [{"job": j, "score": (a.overall_score if (a := latest_match_analysis(session, j.id)) else None)} for j in jobs]
    return templates.TemplateResponse(
        request,
        "job_list.html",
        {
            "jobs": rows,
            "status": status,
            "min_score": min_score,
            "statuses": [s.value for s in JobStatus],
        },
    )


@router.get("/jobs/{job_id}")
def job_detail_page(request: Request, job_id: int, session: Session = Depends(get_session)):
    detail = get_job_detail(session, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    return templates.TemplateResponse(
        request, "job_detail.html", {"detail": detail, "actions": _actions_for(detail.job)}
    )


@router.get("/jobs/{job_id}/resume.docx")
def download_resume_docx(job_id: int, session: Session = Depends(get_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.docx_path:
        raise HTTPException(status_code=404, detail="No tailored resume DOCX for this job")
    return FileResponse(detail.tailored_resume.docx_path, filename=Path(detail.tailored_resume.docx_path).name)


@router.get("/jobs/{job_id}/resume.pdf")
def download_resume_pdf(job_id: int, session: Session = Depends(get_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.pdf_path:
        raise HTTPException(status_code=404, detail="No tailored resume PDF for this job")
    return FileResponse(detail.tailored_resume.pdf_path, filename=Path(detail.tailored_resume.pdf_path).name)


def _action_route(action_fn_name: str):
    def handler(job_id: int, session: Session = Depends(get_session)):
        job = _get_job_or_404(session, job_id)
        transition_fn = getattr(state_machine, action_fn_name)
        try:
            transition_fn(session, job)
        except state_machine.InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    return handler


router.add_api_route("/jobs/{job_id}/approve", _action_route("approve"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/skip", _action_route("skip"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/open-application", _action_route("mark_application_opened"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/mark-applied", _action_route("mark_applied"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/mark-interview", _action_route("mark_interview"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/mark-offer", _action_route("mark_offer"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/mark-rejected", _action_route("mark_rejected"), methods=["POST"])
router.add_api_route("/jobs/{job_id}/archive", _action_route("archive"), methods=["POST"])


@router.get("/costs")
def costs_page(request: Request, session: Session = Depends(get_session)):
    summary = ai_cost_summary(session)
    return templates.TemplateResponse(request, "costs.html", {"summary": summary})
