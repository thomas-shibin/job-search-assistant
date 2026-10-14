"""Tracked-job pages: the Applications list (and its filter fragment), one
job's page with its status actions (approve, skip, applied, interview, ...),
replacing a job-board preview with the full description, and tracking a
search result. Status rules live in app/modules/applications/state_machine.py.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.current import load_config
from app.core.config.loader import ConfigError
from app.core.tenancy import require_tenant
from app.integrations.ai.factory import AIProviderConfigError
from app.modules.applications import state_machine
from app.modules.applications.service import latest_match_analysis, replace_job_description, tracked_filters, tracked_rows
from app.modules.applications.views import get_job_or_404, job_detail_response
from app.modules.auth.deps import member_session, require_member
from app.modules.auth.models import User
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.jobs.models import Job, JobStatus
from app.modules.jobs.service import posted_job
from app.modules.matching.fit import assess_job_fit, latest_assessment
from app.modules.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.modules.matching.workflows import ANALYSIS_UNAVAILABLE, analyze_and_tailor
from app.modules.usage import quota
from app.modules.usage.models import ActivityKind
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/applications")
def applications_page(request: Request, status: str | None = None, min_score: str | None = None,
                      session: Session = Depends(member_session)):
    status, min_score = tracked_filters(status, min_score)
    return templates.TemplateResponse(request, "applications.html", {
        "jobs": tracked_rows(session, status, min_score), "status": status, "min_score": min_score,
        "statuses": [s.value for s in JobStatus]})


@router.get("/tracked-jobs")
def tracked_jobs_table(
    request: Request,
    status: str | None = None,
    min_score: str | None = None,
    session: Session = Depends(member_session),
):
    """Just the "Your tracked jobs" table, for the filters to swap in place --
    no page reload, no scroll jump, and no re-run of the job search."""
    status, min_score = tracked_filters(status, min_score)
    return templates.TemplateResponse(
        request, "_tracked_jobs.html", {"jobs": tracked_rows(session, status, min_score)}
    )


@router.get("/jobs/{job_id}")
def job_detail_page(request: Request, job_id: int, error: str | None = None, msg: str | None = None,
                    session: Session = Depends(member_session)):
    return job_detail_response(request, session, job_id, error=error, message=msg)


@router.post("/jobs/{job_id}/description")
def update_job_description(job_id: int, description: str = Form(...), session: Session = Depends(member_session),
                           user: User = Depends(require_member)):
    """Save the full job description (pasted over the job board's preview), then
    re-score it and -- if it had one -- re-run the AI fit analysis on it."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    try:
        replace_job_description(session, job, description)
    except ValueError as exc:
        return RedirectResponse(url=f"/jobs/{job_id}?{urlencode({'error': str(exc)})}#description", status_code=303)

    notes = ["Full description saved."]
    rescore = latest_match_analysis(session, job_id) is not None
    reassess = latest_assessment(session, job_id) is not None
    if rescore or reassess:
        try:  # one resume action, however many provider calls it takes
            config = load_config(session)
            with quota.ai_action(session, user, ActivityKind.AI_ANALYSIS, quota.job_label(job), job_id=job.id):
                if rescore:
                    analyze_job(session, job, config, update_status=False)
                    notes.append("Keyword score updated.")
                if reassess:
                    assess_job_fit(session, job, config, force=True)
                    notes.append("Match analysis re-run on the full description.")
        except quota.LimitReached as exc:
            notes.append(str(exc))
        except (ConfigError, NoActiveCandidateProfileError):
            session.rollback()
        except AIProviderConfigError:
            logger.exception("Analysis provider is not configured")
            notes.append(ANALYSIS_UNAVAILABLE)
        except Exception:
            session.rollback()
            logger.exception("Match analysis failed")
            notes.append("The analysis couldn't be completed. Please try again in a moment.")
    anchor = "fit" if latest_assessment(session, job_id) is not None else "description"
    return RedirectResponse(url=f"/jobs/{job_id}?{urlencode({'msg': ' '.join(notes)})}#{anchor}", status_code=303)


def _action_route(action_fn_name: str):
    """A POST handler moving a job to its next status with the named
    state-machine function (409 when the move isn't allowed from its status)."""
    def handler(job_id: int, session: Session = Depends(member_session)):
        job = get_job_or_404(session, job_id)
        transition_fn = getattr(state_machine, action_fn_name)
        try:
            transition_fn(session, job)
        except state_machine.InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        status = job.status.value if hasattr(job.status, "value") else str(job.status)
        quota.record(session, require_tenant(session), ActivityKind.JOB_STATUS,
                     f"{status.replace('_', ' ').title()}: {quota.job_label(job)}", meta={"job_id": job.id})
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


@router.post("/browse/track")
def browse_track_job(
    request: Request,
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
    """Turns one search result into a tracked `Job` row (then scores it and,
    if it qualifies, tailors a resume) -- the only way a search result reaches
    the database besides saving it. The posting fields ride along as hidden
    form inputs, since the job board's API has no "fetch by id" endpoint.
    """
    raw_job = posted_job(session, pool_id, external_id, source_url, company, title, location, description,
                         date_posted, ref)
    if not (raw_job.source_url and raw_job.title and raw_job.description):
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available. Search again."}),
                                status_code=303)
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job

    error = None
    if ingest_result.created:
        quota.record(session, user.id, ActivityKind.JOB_TRACKED, quota.job_label(job), meta={"job_id": job.id})
        try:
            with quota.ai_action(session, user, ActivityKind.AI_TAILOR, quota.job_label(job), job_id=job.id):
                error = analyze_and_tailor(session, job)
        except quota.LimitReached as exc:
            error = f"Job saved. {exc}"
    # The job is tracked either way; a failed analysis just leaves it at
    # whatever status it reached, with the reason shown on its detail page.
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job.id}{query}", status_code=303)
