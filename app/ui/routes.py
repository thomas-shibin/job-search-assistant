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
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.ai.factory import AIProviderConfigError
from app.applications import state_machine
from app.catalog.store import location_paths, remember_user_terms
from app.config.loader import ConfigError
from app.ai.fit import active_candidate, assess_job_fit, latest_assessment
from app.config.schema import GlobalSearchConfig
from app.config.store import load_app_config, save_section
from app.models.candidate import CandidateProfile
from app.models.resume import TailoredBulletProvenance
from app.database.session import get_session
from app.ingestion.pipeline import ingest_raw_job
from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.models.job import Job, JobStatus
from app.resume.parser import ImportResult, import_resume
from app.resume.pipeline import generate_resume_bundle
from app.resume.tailor import NoActiveCandidateProfileError as TailorNoProfileError
from app.resume.tailor import (
    BaseResumeMissingError, rebuild_tailored_resume, resolve_address, resume_download_name, tailor_resume,
)
from app.documents.libreoffice import PdfConversionError, convert_to_pdf
from app.services.costs import ai_cost_summary
from app.services.jobs import (
    get_job_detail, is_description_preview, latest_match_analysis, list_jobs, replace_job_description,
)
from app.services.search import query_pairs, search_jobs
from app.sources.adzuna import AdzunaSource
from app.sources.base import RawJob
from app.sources.manual import import_manual_job
from app.sources.settings import AdzunaSettings
from app.ui.diff import inline_word_diff_html, side_by_side_diff
from app.utils.filenames import sanitize_filename
from app.utils.terms import split_places, split_terms

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
UPLOAD_DIR = PROJECT_ROOT / "data" / "resumes" / "uploads"
_ALLOWED_RESUME_SUFFIXES = {".docx", ".pdf"}

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["worddiff"] = inline_word_diff_html
templates.env.globals["sidediff"] = side_by_side_diff

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


_CARDS_PER_PAGE = 40


def _ago(value: datetime | None) -> str:
    if value is None:
        return ""
    now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
    days = (now - value).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return value.strftime("%b %d, %Y")


templates.env.filters["ago"] = _ago


def _tracked_by_external_id(session: Session, external_ids: list[str]) -> dict[str, dict]:
    """For search results already saved as jobs: their id, score and AI fit."""
    if not external_ids:
        return {}
    rows = (
        session.query(Job.external_id, Job.id)
        .filter(Job.source == "adzuna", Job.external_id.in_(external_ids))
        .all()
    )
    tracked = {}
    for external_id, job_id in rows:
        analysis = latest_match_analysis(session, job_id)
        tracked[external_id] = {
            "job_id": job_id,
            "score": analysis.overall_score if analysis else None,
            "fit": latest_assessment(session, job_id),
        }
    return tracked


def _match_score(scores: dict | None) -> int | None:
    """The score a job is ranked by -- the one its badge shows: the AI fit
    score if it has been analyzed with AI, else the keyword score."""
    if not scores:
        return None
    if scores.get("fit") is not None:
        return scores["fit"].fit_score
    return scores.get("score")


def _best_match_first(items: list, scores) -> list:
    """Highest matching score first; unscored jobs after them, in their
    original (newest-first) order -- the sort is stable."""
    def key(item):
        score = _match_score(scores(item))
        return (score is None, -(score or 0))
    return sorted(items, key=key)


def _search_for_page(
    session: Session, *, titles: list[str], locations: list[str], keywords: list[str], country: str, page: int
) -> dict:
    """Live Adzuna search across every title x location (see
    app/services/search.py). Nothing here touches the DB -- a posting only
    becomes a tracked `Job` when the user analyzes it (`browse_analyze_job`).
    An empty page past the last real results is the "no more results" signal.
    """
    page = max(page, 1)
    result = {"error": None, "cards": [], "page": page, "has_next": False, "failed": [],
              "queries": len(query_pairs(titles, locations))}

    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        result["error"] = (
            "Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env to search Adzuna "
            "(free signup at https://developer.adzuna.com/)."
        )
        return result

    source = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
    per_query = max(10, min(50, _CARDS_PER_PAGE // result["queries"]))
    try:
        found = search_jobs(source, titles, locations, page=page, per_page=per_query,
                            keywords=keywords, areas=location_paths(session))
    except Exception as exc:
        result["error"] = f"Adzuna search failed: {exc}"
        return result

    tracked = _tracked_by_external_id(session, [j.external_id for j in found.jobs if j.external_id])
    cards = [{"raw": j, "tracked": tracked.get(j.external_id)} for j in found.jobs]
    result["cards"] = _best_match_first(cards, lambda card: card["tracked"])
    result["has_next"] = found.has_next
    result["failed"] = found.failed_queries
    return result


def _tracked_filters(status: str | None, min_score: str | None) -> tuple[str | None, int | None]:
    """The tracked-jobs filters as submitted: a blank box (``min_score=``) or an
    unknown status means "no filter" rather than a 422 the page can't show."""
    status = status if status in {s.value for s in JobStatus} else None
    try:
        score = max(0, min(100, int(min_score))) if min_score and min_score.strip() else None
    except ValueError:
        score = None
    return status, score


def _tracked_rows(session: Session, status: str | None, min_score: int | None) -> list[dict]:
    rows = [{"job": j, "score": (a.overall_score if (a := latest_match_analysis(session, j.id)) else None),
             "fit": latest_assessment(session, j.id)} for j in list_jobs(session, status=status, min_score=min_score)]
    return _best_match_first(rows, lambda row: row)


def _picked(values: list[str], split) -> list[str]:
    """Picker values (one per tag) -- or a comma list typed into a URL -- as a clean list."""
    return list(dict.fromkeys(term for value in values for term in split(value)))


@router.get("/")
def job_list_page(
    request: Request,
    status: str | None = None,
    min_score: str | None = None,
    titles: list[str] | None = Query(None),
    locations: list[str] | None = Query(None),
    keywords: list[str] | None = Query(None),
    country: str | None = None,
    page: int = 1,
    msg: str | None = None,
    session: Session = Depends(get_session),
):
    """Landing page: the job search, prefilled from Settings -> Job search and
    run straight away (a blank search box means "any"), as cards; plus your
    tracked jobs below. Query parameters override the saved conditions for
    this one search -- "Save as default" stores them.
    """
    config_error = None
    try:
        search_config = load_app_config(session, DEFAULT_CONFIG_PATH).search
    except ConfigError as exc:
        config_error = f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
        search_config = GlobalSearchConfig()

    title_list = _picked(titles, split_terms) if titles is not None else search_config.titles
    location_list = _picked(locations, split_places) if locations is not None else search_config.locations
    keyword_list = _picked(keywords, split_terms) if keywords is not None else search_config.keywords
    country = (country or search_config.country).strip().lower()
    # Whatever is searched -- saved defaults or typed in -- is offered in the pickers from now on.
    remember_user_terms(session, title=title_list, location=location_list, keyword=keyword_list)

    search = _search_for_page(session, titles=title_list, locations=location_list, keywords=keyword_list,
                              country=country, page=page)
    search.update({
        "title_list": title_list, "location_list": location_list, "keyword_list": keyword_list, "country": country,
        "query_string": urlencode(
            {"titles": title_list or [""], "locations": location_list or [""], "keywords": keyword_list or [""],
             "country": country},
            doseq=True,
        ),
        "is_default": (title_list, location_list, keyword_list, country)
        == (search_config.titles, search_config.locations, search_config.keywords, search_config.country),
    })

    status, min_score = _tracked_filters(status, min_score)
    rows = _tracked_rows(session, status, min_score)
    return templates.TemplateResponse(
        request,
        "job_list.html",
        {
            "jobs": rows,
            "status": status,
            "min_score": min_score,
            "search": search,
            "statuses": [s.value for s in JobStatus],
            "has_resume": active_candidate(session) is not None,
            "error": config_error,
            "message": msg,
        },
    )


@router.get("/tracked-jobs")
def tracked_jobs_table(
    request: Request,
    status: str | None = None,
    min_score: str | None = None,
    session: Session = Depends(get_session),
):
    """Just the "Your tracked jobs" table, for the landing page's filters to swap
    in place -- no page reload, no scroll jump, and no re-run of the Adzuna search."""
    status, min_score = _tracked_filters(status, min_score)
    return templates.TemplateResponse(
        request, "_tracked_jobs.html", {"jobs": _tracked_rows(session, status, min_score)}
    )


@router.post("/search/defaults")
def save_search_defaults(
    titles: list[str] = Form([]), locations: list[str] = Form([]), keywords: list[str] = Form([]),
    country: str = Form("ca"), session: Session = Depends(get_session),
):
    """"Save as default": the current search box values become Settings -> Job search."""
    try:
        current = load_app_config(session, DEFAULT_CONFIG_PATH).search
        save_section(session, "search", {
            **current.model_dump(), "titles": _picked(titles, split_terms),
            "locations": _picked(locations, split_places), "keywords": _picked(keywords, split_terms), "country": country,
        })
    except ConfigError as exc:
        return RedirectResponse(url=f"/?{urlencode({'msg': 'Not saved: ' + '; '.join(exc.errors)})}", status_code=303)
    return RedirectResponse(url=f"/?{urlencode({'msg': 'Saved as your default search.'})}", status_code=303)


@router.post("/resume/upload")
async def upload_resume(
    resume: UploadFile = File(...), next: str = Form("/"), session: Session = Depends(get_session)
):
    error = await _save_uploaded_resume(session, resume)
    path, _, fragment = next.partition("#")
    if not path.startswith("/") or path.startswith("//"):  # only ever redirect within this app
        path, fragment = "/", ""
    if path.startswith("/jobs/"):
        query = f"?{urlencode({'error': error})}" if error else ""
    else:
        query = f"?{urlencode({'msg': error or 'Resume imported -- you can now analyze jobs against it.'})}"
    return RedirectResponse(url=f"{path}{query}{'#' + fragment if fragment else ''}", status_code=303)


def _analyze_and_tailor(session: Session, job: Job, *, force: bool = False) -> str | None:
    """Score `job` and, if it qualifies (or `force`: "Tailor anyway"), generate
    its tailored resume. Returns a human-readable reason when that didn't happen
    (so the dashboard can show it instead of leaving the job silently stuck at
    DISCOVERED), else None.
    """
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        analysis = analyze_job(session, job, config)
        if not analysis.filter_result.passed and not force:
            return ("Skipped by your hard filters (" + "; ".join(analysis.filter_result.reasons)
                    + ") -- no tailored resume generated. Use \"Tailor anyway\" to make one.")
        if not analysis.qualified and not force:
            return (
                f"Scored {analysis.breakdown.overall_score}/100, below your minimum score "
                f"({config.matching.minimum_score}, Settings -> Matching) -- no tailored resume generated. "
                "Use \"Tailor anyway\" to make one."
            )
        if analysis.qualified:
            generate_resume_bundle(session, job, config)
        else:  # forced: tailor without moving the job through the qualified statuses
            result = tailor_resume(session, job, config, update_status=False)
            try:
                result.tailored_resume.pdf_path = str(convert_to_pdf(result.docx_path))
                session.commit()
            except PdfConversionError:
                pass  # DOCX is still there; PDF needs LibreOffice
    except ConfigError as exc:
        return f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
    except BaseResumeMissingError as exc:
        return str(exc)
    except (NoActiveCandidateProfileError, TailorNoProfileError):
        return "No resume yet -- upload yours on the My resume page (or below), then click \"Analyze & tailor\"."
    except AIProviderConfigError as exc:
        return str(exc)
    return None


async def _import_uploaded_resume(session: Session, resume: UploadFile) -> tuple[ImportResult | None, str | None]:
    """Save an uploaded resume and import it as the active candidate profile
    (a no-op if its content matches the current one). Returns (result, None),
    or (None, a message for the user) if the file can't be used.
    """
    suffix = Path(resume.filename or "").suffix.lower()
    if suffix not in _ALLOWED_RESUME_SUFFIXES:
        return None, "Resume must be a .docx or .pdf file."
    data = await resume.read()
    if not data:
        return None, "That file is empty."
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Unique even for two uploads in the same second (the second, if identical,
    # is deleted below -- it must never be the file the active resume points at).
    dest_name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{sanitize_filename(Path(resume.filename).stem)}{suffix}"
    dest_path = UPLOAD_DIR / dest_name
    dest_path.write_bytes(data)
    # Relative to the project when possible, so the same database works for the
    # native install and Docker (/app) alike.
    stored_path = dest_path.relative_to(PROJECT_ROOT) if dest_path.is_relative_to(PROJECT_ROOT) else dest_path
    try:
        result = import_resume(session, stored_path if stored_path.exists() else dest_path)
    except Exception as exc:  # unreadable/corrupt file
        session.rollback()
        dest_path.unlink(missing_ok=True)
        return None, f"Couldn't read that resume ({type(exc).__name__}). Is it a valid Word or PDF file?"
    if not result.created and Path(result.profile.raw_resume_path) not in (stored_path, dest_path):
        dest_path.unlink(missing_ok=True)  # same content as the current resume: keep nothing new
    else:
        try:  # keep Settings -> Candidate -> base resume pointing at the file in use
            candidate = load_app_config(session, DEFAULT_CONFIG_PATH).candidate
            save_section(session, "candidate", {**candidate.model_dump(), "base_resume": str(result.profile.raw_resume_path)})
        except ConfigError:
            pass
    return result, None


async def _save_uploaded_resume(session: Session, resume: UploadFile) -> str | None:
    """Persist and import an uploaded resume as the active candidate profile.
    Returns an error message for an unusable file, else None.
    """
    return (await _import_uploaded_resume(session, resume))[1]


def _resume_file(candidate: CandidateProfile) -> Path | None:
    for path in (Path(candidate.raw_resume_path), PROJECT_ROOT / candidate.raw_resume_path):
        if path.is_file():
            return path
    return None


@router.get("/resume")
def resume_page(request: Request, msg: str | None = None, error: str | None = None,
                session: Session = Depends(get_session)):
    """Your base resume: what the app read from it, and where to replace it."""
    current = active_candidate(session)
    versions = session.query(CandidateProfile).order_by(CandidateProfile.version.desc()).limit(10).all()
    return templates.TemplateResponse(request, "resume.html", {
        "current": current, "versions": versions, "message": msg, "error": error,
        "file_available": bool(current and _resume_file(current)),
        "is_docx": bool(current and Path(current.raw_resume_path).suffix.lower() == ".docx"),
        "unparsed": [part for part in (current.raw_unparsed_sections or "").split("\n\n---\n\n") if part.strip()]
        if current else [],
    })


@router.post("/resume")
async def resume_page_upload(resume: UploadFile = File(...), session: Session = Depends(get_session)):
    result, error = await _import_uploaded_resume(session, resume)
    if error:
        return RedirectResponse(url=f"/resume?{urlencode({'error': error})}", status_code=303)
    if not result.created:
        message = "That's the same content as your current resume -- nothing changed."
    else:
        c = result.counts
        message = (f"Resume uploaded and in use (version {result.profile.version}): {c['employments']} roles, "
                   f"{c['bullets']} bullets, {c['skills']} skills. Check below that everything was read correctly.")
    return RedirectResponse(url=f"/resume?{urlencode({'msg': message})}", status_code=303)


@router.get("/resume/file")
def download_base_resume(session: Session = Depends(get_session)):
    current = active_candidate(session)
    path = _resume_file(current) if current else None
    if path is None:
        raise HTTPException(status_code=404, detail="No base resume file on disk")
    return FileResponse(path, filename=f"base_resume{path.suffix}")


def _job_detail_response(request: Request, session: Session, job_id: int, *, error: str | None = None,
                         message: str | None = None):
    detail = get_job_detail(session, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    resume_address = None
    try:
        minimum_score = load_app_config(session, DEFAULT_CONFIG_PATH).matching.minimum_score
    except ConfigError:
        minimum_score = None
    if detail.tailored_resume is not None:
        try:
            resume_address = resolve_address(load_app_config(session, DEFAULT_CONFIG_PATH), detail.job, session)
        except ConfigError:
            pass
    return templates.TemplateResponse(
        request, "job_detail.html",
        {
            "resume_address": resume_address,
            "description_preview": is_description_preview(detail.job),
            "detail": detail, "actions": _actions_for(detail.job), "error": error, "message": message,
            "fit": latest_assessment(session, job_id), "has_resume": active_candidate(session) is not None,
            "minimum_score": minimum_score,
        },
    )


@router.get("/jobs/{job_id}")
def job_detail_page(request: Request, job_id: int, error: str | None = None, msg: str | None = None,
                    session: Session = Depends(get_session)):
    return _job_detail_response(request, session, job_id, error=error, message=msg)


@router.post("/jobs/{job_id}/description")
def update_job_description(job_id: int, description: str = Form(...), session: Session = Depends(get_session)):
    """Save the full job description (pasted over Adzuna's preview), then
    re-score it and -- if it had one -- re-run the AI fit analysis on it."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    try:
        replace_job_description(session, job, description)
    except ValueError as exc:
        return RedirectResponse(url=f"/jobs/{job_id}?{urlencode({'error': str(exc)})}#description", status_code=303)

    notes = ["Full description saved."]
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        if latest_match_analysis(session, job_id) is not None:
            analyze_job(session, job, config, update_status=False)
            notes.append("Keyword score updated.")
    except (ConfigError, NoActiveCandidateProfileError):
        session.rollback()
    if latest_assessment(session, job_id) is not None:
        error = _assess_fit(session, job, force=True)
        notes.append(error or "AI analysis re-run on the full description.")
    anchor = "fit" if latest_assessment(session, job_id) is not None else "description"
    return RedirectResponse(url=f"/jobs/{job_id}?{urlencode({'msg': ' '.join(notes)})}#{anchor}", status_code=303)


@router.post("/jobs/{job_id}/retailor")
def retailor_job(job_id: int, session: Session = Depends(get_session)):
    """Re-tailor an already-tailored job with AI (whatever its score), in
    your resume's own design, without changing the job's status."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        result = tailor_resume(session, job, config, update_status=False)
    except ConfigError as exc:
        return _job_redirect(job_id, error=f"Settings are invalid: {'; '.join(exc.errors)}")
    except (TailorNoProfileError, AIProviderConfigError) as exc:
        return _job_redirect(job_id, error=str(exc))
    try:
        result.tailored_resume.pdf_path = str(convert_to_pdf(result.docx_path))
        session.commit()
    except PdfConversionError:
        pass  # DOCX is still there; PDF needs LibreOffice
    return _job_redirect(job_id, msg="Resume re-tailored -- download it below.")


@router.post("/jobs/{job_id}/removals")
async def apply_removals(job_id: int, request: Request, session: Session = Depends(get_session)):
    """Take the paragraphs the user ticked out of this job's tailored resume (and
    put back any they unticked), then rebuild it from the base resume -- no AI call."""
    job = _get_job_or_404(session, job_id)
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None:
        raise HTTPException(status_code=404, detail="No tailored resume for this job")
    chosen = {int(v) for v in (await request.form()).getlist("remove") if str(v).isdigit()}
    rows = session.query(TailoredBulletProvenance).filter(
        TailoredBulletProvenance.tailored_resume_id == detail.tailored_resume.id,
        TailoredBulletProvenance.source_text.isnot(None)).all()
    for row in rows:
        row.removed = row.id in chosen
    session.commit()
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        path = rebuild_tailored_resume(session, detail.tailored_resume, config)
    except (ConfigError, TailorNoProfileError) as exc:
        return _job_redirect(job_id, error=str(exc))
    if path is None:
        return _job_redirect(job_id, error="Couldn't rebuild the resume: your base resume is missing.")
    try:
        detail.tailored_resume.pdf_path = str(convert_to_pdf(path))
        session.commit()
    except PdfConversionError:
        pass
    count = sum(1 for row in rows if row.removed)
    return _job_redirect(job_id, msg=f"Resume rebuilt with {count} paragraph{'s' if count != 1 else ''} removed."
                         if count else "Resume rebuilt with every paragraph included.")


def _job_redirect(job_id: int, **params: str) -> RedirectResponse:
    return RedirectResponse(url=f"/jobs/{job_id}?{urlencode(params)}#resume", status_code=303)


@router.post("/jobs/{job_id}/analyze")
async def analyze_job_action(
    request: Request,
    job_id: int,
    resume: UploadFile | None = File(None),
    force: bool = Form(False),
    session: Session = Depends(get_session),
):
    """Re-run scoring + tailoring for one job from its detail page, optionally
    importing a newly uploaded resume first -- the in-browser way out of a job
    stuck at DISCOVERED (e.g. tracked before any resume was imported).
    """
    job = _get_job_or_404(session, job_id)
    error = None
    if resume is not None and resume.filename:
        error = await _save_uploaded_resume(session, resume)
    if error is None:
        error = _analyze_and_tailor(session, job, force=force)
    if error is not None:
        return _job_detail_response(request, session, job_id, error=error)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


def _assess_fit(session: Session, job: Job, *, force: bool) -> str | None:
    """Score (free, deterministic) then run the AI fit assessment. Returns
    a human-readable reason when the assessment couldn't run, else None.
    """
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        if job.status == JobStatus.DISCOVERED:
            analyze_job(session, job, config)
        assess_job_fit(session, job, config, force=force)
    except ConfigError as exc:
        return f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
    except NoActiveCandidateProfileError:
        return "No resume imported yet -- upload your resume below, then click \"Analyze with AI\"."
    except AIProviderConfigError as exc:
        return str(exc)
    except Exception as exc:  # the AI call itself (network, quota, bad response)
        session.rollback()
        return f"AI analysis failed: {exc}"
    return None


@router.post("/jobs/{job_id}/assess")
def assess_job_action(job_id: int, session: Session = Depends(get_session)):
    job = _get_job_or_404(session, job_id)
    error = _assess_fit(session, job, force=latest_assessment(session, job_id) is not None)
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job_id}{query}#fit", status_code=303)


@router.get("/jobs/{job_id}/resume.docx")
def download_resume_docx(job_id: int, session: Session = Depends(get_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.docx_path:
        raise HTTPException(status_code=404, detail="No tailored resume DOCX for this job")
    return FileResponse(detail.tailored_resume.docx_path, filename=_download_name(session, detail, ".docx"))


@router.get("/jobs/{job_id}/resume.pdf")
def download_resume_pdf(job_id: int, session: Session = Depends(get_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.pdf_path:
        raise HTTPException(status_code=404, detail="No tailored resume PDF for this job")
    return FileResponse(detail.tailored_resume.pdf_path, filename=_download_name(session, detail, ".pdf"))


def _download_name(session: Session, detail, suffix: str) -> str:
    """"First Last - Company - timestamp.docx" (see resume_download_name)."""
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
    except ConfigError:
        return f"tailored_resume{suffix}"
    return resume_download_name(config, active_candidate(session), detail.job.company,
                                detail.tailored_resume.generated_at, suffix)


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
def quick_tailor_form(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "quick.html", {"error": None})


@router.post("/quick")
async def quick_tailor_submit(
    request: Request,
    resume: UploadFile | None = File(None),
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

    uploading = resume is not None and bool(resume.filename)
    if uploading and Path(resume.filename).suffix.lower() not in _ALLOWED_RESUME_SUFFIXES:
        return _quick_form_response(request, error="Resume must be a .docx or .pdf file.")
    if not uploading and active_candidate(session) is None:
        return _quick_form_response(request, error="Upload your resume (here, or once on the My resume page) first.")

    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
    except ConfigError as exc:
        return _quick_form_response(request, error=f"Settings are invalid: {'; '.join(exc.errors)}", status_code=500)

    if uploading:
        upload_error = await _save_uploaded_resume(session, resume)
        if upload_error:
            return _quick_form_response(request, error=upload_error)

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


def _raw_job_from_form(
    external_id: str, source_url: str, company: str, title: str, location: str, description: str, date_posted: str
) -> RawJob:
    parsed_date: datetime | None = None
    if date_posted:
        try:
            parsed_date = datetime.fromisoformat(date_posted)
        except ValueError:
            parsed_date = None
    return RawJob(
        external_id=external_id or None, source="adzuna", source_url=source_url, company=company, title=title,
        location=location or None, description=description, date_posted=parsed_date,
    )


@router.post("/browse/analyze")
def browse_analyze_job(
    external_id: str = Form(""),
    source_url: str = Form(...),
    company: str = Form(...),
    title: str = Form(...),
    location: str = Form(""),
    description: str = Form(...),
    date_posted: str = Form(""),
    session: Session = Depends(get_session),
):
    """A search card's "Analyze with AI": saves the posting as a tracked
    job (the posting fields ride along as hidden inputs, since Adzuna has no
    fetch-by-id endpoint), then compares it against your resume. Tailoring a
    resume is a separate, explicit step on the job's page.
    """
    raw_job = _raw_job_from_form(external_id, source_url, company, title, location, description, date_posted)
    job = ingest_raw_job(session, raw_job).job
    error = _assess_fit(session, job, force=False)
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job.id}{query}#fit", status_code=303)


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
    raw_job = _raw_job_from_form(external_id, source_url, company, title, location, description, date_posted)
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job

    error = _analyze_and_tailor(session, job) if ingest_result.created else None
    # The job is tracked either way; a failed analysis just leaves it at
    # whatever status it reached, with the reason shown on its detail page.
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job.id}{query}", status_code=303)
