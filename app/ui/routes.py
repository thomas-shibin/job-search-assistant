"""HTML/HTMX dashboard routes (Milestone 9): job list with filters, match
detail + resume diff view, the approve/skip/open-application/... actions
from `app/applications/state_machine.py`, and the one-shot "quick tailor"
flow (upload a resume + paste a job description -> tailored resume, no CLI
required). `hx-boost` on <body> (see base.html) is what gives normal
links/forms AJAX-style partial swaps without a separate template per HTMX
fragment -- it degrades to plain navigation with JS disabled.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.ai.factory import AIProviderConfigError
from app.applications import state_machine
from app.config.loader import ConfigError, load_config
from app.database.session import get_session
from app.ingestion.pipeline import ingest_raw_job
from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.models.job import Job, JobStatus
from app.resume.parser import import_resume
from app.resume.pipeline import generate_resume_bundle
from app.services.costs import ai_cost_summary
from app.services.jobs import get_job_detail, latest_match_analysis, list_jobs
from app.sources.adzuna import AdzunaSource
from app.sources.base import RawJob
from app.sources.manual import import_manual_job
from app.sources.settings import AdzunaSettings
from app.ui.diff import inline_word_diff_html
from app.utils.filenames import sanitize_filename

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
UPLOAD_DIR = PROJECT_ROOT / "data" / "resumes" / "uploads"
_ALLOWED_RESUME_SUFFIXES = {".docx", ".pdf"}

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


DEFAULT_ADZUNA_COUNTRY = "ca"


def _search_adzuna(session: Session, request: Request, *, what: str, where: str, country: str, page: int) -> dict:
    """Live Adzuna search for the dashboard's "Search for jobs" section --
    nothing here touches the DB. A posting only becomes a tracked `Job` row
    when the user explicitly hits "Track & tailor" on it (`browse_track_job`
    below), unlike the automated scheduler's `AdzunaSource.discover_jobs`
    (config.yaml's `adzuna_enabled`), which ingests everything it finds.
    Only runs at all if the search form was actually submitted (`what`/`where`
    present in the query string, even blank) -- a plain visit to `/` does no
    Adzuna call. `page` past the last page of real results comes back empty
    from Adzuna -- that's the "no more results" signal, not an error.
    """
    searched = "what" in request.query_params or "where" in request.query_params
    page = max(page, 1)
    result = {
        "searched": searched, "error": None, "results": [],
        "what": what, "where": where, "country": country, "page": page, "has_next": False,
    }
    if not searched:
        return result

    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        result["error"] = (
            "Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env to search Adzuna "
            "(free signup at https://developer.adzuna.com/)."
        )
        return result

    source = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
    try:
        raw_jobs = source.search_page(what=what or None, where=where or None, page=page)
    except httpx.HTTPError as exc:
        result["error"] = f"Adzuna search failed: {exc}"
        return result

    candidate_ids = [j.external_id for j in raw_jobs if j.external_id is not None]
    tracked_job_ids: dict[str, int] = {}
    if candidate_ids:
        tracked_job_ids = {
            row.external_id: row.id
            for row in session.query(Job.external_id, Job.id)
            .filter(Job.source == "adzuna", Job.external_id.in_(candidate_ids))
            .all()
        }

    result["results"] = [{"raw": j, "tracked_job_id": tracked_job_ids.get(j.external_id)} for j in raw_jobs]
    result["has_next"] = len(raw_jobs) > 0
    return result


@router.get("/")
def job_list_page(
    request: Request,
    status: str | None = None,
    min_score: int | None = None,
    what: str = "",
    where: str = "",
    country: str = DEFAULT_ADZUNA_COUNTRY,
    page: int = 1,
    session: Session = Depends(get_session),
):
    jobs = list_jobs(session, status=status, min_score=min_score)
    rows = [{"job": j, "score": (a.overall_score if (a := latest_match_analysis(session, j.id)) else None)} for j in jobs]
    search = _search_adzuna(session, request, what=what, where=where, country=country, page=page)
    return templates.TemplateResponse(
        request,
        "job_list.html",
        {
            "jobs": rows,
            "status": status,
            "min_score": min_score,
            "search": search,
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


def _quick_form_response(request: Request, *, error: str, status_code: int = 400):
    return templates.TemplateResponse(request, "quick.html", {"error": error}, status_code=status_code)


@router.get("/quick")
def quick_tailor_form(request: Request):
    return templates.TemplateResponse(request, "quick.html", {"error": None})


@router.post("/quick")
async def quick_tailor_submit(
    request: Request,
    resume: UploadFile = File(...),
    description: str = Form(...),
    url: str = Form(""),
    company: str = Form(""),
    title: str = Form(""),
    session: Session = Depends(get_session),
):
    """One-shot upload-resume + paste-job-description -> tailored resume flow,
    skipping the discover/analyze/generate-resume CLI chain. Unlike the
    automated pipeline, this always tailors regardless of `matching.
    minimum_score` -- you already picked this job by pasting it in, so there's
    nothing left to filter for.
    """
    if not description.strip():
        return _quick_form_response(request, error="Paste the job description text.")

    suffix = Path(resume.filename or "").suffix.lower()
    if suffix not in _ALLOWED_RESUME_SUFFIXES:
        return _quick_form_response(request, error="Resume must be a .docx or .pdf file.")

    if not DEFAULT_CONFIG_PATH.exists():
        return _quick_form_response(
            request, error=f"Config file not found: {DEFAULT_CONFIG_PATH}. Run 'python cli.py init' first.", status_code=500
        )
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except ConfigError as exc:
        return _quick_form_response(request, error=f"config.yaml is invalid: {'; '.join(exc.errors)}", status_code=500)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest_name = f"{int(time.time())}_{sanitize_filename(Path(resume.filename).stem)}{suffix}"
    dest_path = UPLOAD_DIR / dest_name
    dest_path.write_bytes(await resume.read())

    import_resume(session, dest_path)

    raw_job = import_manual_job(
        description=description, url=url or None, company=company or None, title=title or None
    )
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job

    try:
        analyze_job(session, job, config)
    except NoActiveCandidateProfileError:
        pass  # the import above just created one; this can't actually happen

    try:
        generate_resume_bundle(session, job, config)
    except AIProviderConfigError as exc:
        return _quick_form_response(request, error=str(exc), status_code=500)
    except NoActiveCandidateProfileError as exc:
        return _quick_form_response(request, error=str(exc), status_code=500)

    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


@router.post("/browse/track")
def browse_track_job(
    request: Request,
    external_id: str = Form(""),
    source_url: str = Form(...),
    company: str = Form(...),
    title: str = Form(...),
    location: str = Form(""),
    description: str = Form(...),
    date_posted: str = Form(""),
    session: Session = Depends(get_session),
):
    """Turns one searched-but-not-yet-saved Adzuna posting (from the
    dashboard's "Search for jobs" section) into a tracked `Job` row -- the
    only way a search result reaches the database. The posting fields ride
    along as hidden form inputs from the results page rather than being
    re-fetched, since Adzuna's search API has no "fetch by id" endpoint.
    """
    parsed_date: datetime | None = None
    if date_posted:
        try:
            parsed_date = datetime.fromisoformat(date_posted)
        except ValueError:
            parsed_date = None

    raw_job = RawJob(
        external_id=external_id or None,
        source="adzuna",
        source_url=source_url,
        company=company,
        title=title,
        location=location or None,
        description=description,
        date_posted=parsed_date,
    )
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job

    if ingest_result.created and DEFAULT_CONFIG_PATH.exists():
        try:
            config = load_config(DEFAULT_CONFIG_PATH)
            analysis = analyze_job(session, job, config)
            if analysis.qualified:
                generate_resume_bundle(session, job, config)
        except (ConfigError, NoActiveCandidateProfileError, AIProviderConfigError):
            pass  # job is still tracked -- just left at whatever status analysis reached

    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)
