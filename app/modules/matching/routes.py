"""Analysis buttons: "Analyze & tailor" and "Analyze match" on a tracked job's
page, and "Analyze match" on a search result (which tracks it first). The
workflows themselves are in app/modules/matching/workflows.py."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.modules.applications.views import get_job_or_404
from app.modules.auth.deps import member_session, require_member
from app.modules.auth.models import User
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.jobs.service import posted_job
from app.modules.matching.fit import latest_assessment
from app.modules.matching.workflows import analyze_and_tailor, assess_fit
from app.modules.resumes.uploads import import_upload
from app.modules.usage import quota
from app.modules.usage.models import ActivityKind

router = APIRouter()


@router.post("/jobs/{job_id}/analyze")
async def analyze_job_action(
    request: Request,
    job_id: int,
    resume: UploadFile | None = File(None),
    force: bool = Form(False),
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
):
    """Re-run scoring + tailoring for one job from its detail page, optionally
    importing a newly uploaded resume first -- the in-browser way out of a job
    stuck at DISCOVERED (e.g. tracked before any resume was imported).
    """
    job = get_job_or_404(session, job_id)
    error = None
    if resume is not None and resume.filename:
        error = (await import_upload(session, resume))[1]
    if error is None:
        try:
            with quota.ai_action(session, user, ActivityKind.AI_TAILOR, quota.job_label(job), job_id=job.id):
                error = analyze_and_tailor(session, job, force=force)
        except quota.LimitReached as exc:
            error = str(exc)
    # Always a redirect, like the other analysis buttons, so the page that shows
    # their progress (app.js, data-work) can go straight to the result.
    query = f"?{urlencode({'error': error})}" if error is not None else ""
    return RedirectResponse(url=f"/jobs/{job_id}{query}#resume", status_code=303)


@router.post("/jobs/{job_id}/assess")
def assess_job_action(job_id: int, session: Session = Depends(member_session), user: User = Depends(require_member)):
    job = get_job_or_404(session, job_id)
    error = assess_fit(session, job, force=latest_assessment(session, job_id) is not None, user=user)
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job_id}{query}#fit", status_code=303)


@router.post("/browse/analyze")
def browse_analyze_job(
    external_id: str = Form(""),
    source_url: str = Form(""),
    company: str = Form(""),
    title: str = Form(""),
    location: str = Form(""),
    description: str = Form(""),
    date_posted: str = Form(""),
    pool_id: str = Form(""),
    ref: str = Form(""),
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
):
    """A search card's "Analyze match": saves the posting as a tracked job,
    then compares it against your resume. Tailoring a resume is a separate,
    explicit step on the job's page.
    """
    raw_job = posted_job(session, pool_id, external_id, source_url, company, title, location, description,
                         date_posted, ref)
    if not (raw_job.source_url and raw_job.title and raw_job.description):
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available. Search again."}),
                                status_code=303)
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job
    if ingest_result.created:
        quota.record(session, user.id, ActivityKind.JOB_TRACKED, quota.job_label(job), meta={"job_id": job.id})
    error = assess_fit(session, job, force=False, user=user)
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job.id}{query}#fit", status_code=303)
