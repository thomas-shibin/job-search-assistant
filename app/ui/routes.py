"""HTML/HTMX dashboard routes (Milestone 9): job list with filters, match
detail + resume diff view, the approve/skip/open-application/... actions
from `app/applications/state_machine.py`, and the one-shot "quick tailor"
flow (upload a resume + paste a job description -> tailored resume, no CLI
required). `hx-boost` on <body> (see base.html) is what gives normal
links/forms AJAX-style partial swaps without a separate template per HTMX
fragment -- it degrades to plain navigation with JS disabled.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.integrations.ai.factory import AIProviderConfigError
from app.applications import state_machine
from app.catalog.store import location_paths, remember_user_terms
from app.core.config.loader import ConfigError
from app.ai.fit import active_candidate, assess_job_fit, latest_assessment
from app.core.config.schema import GlobalSearchConfig
from app.core.config.store import load_app_config, save_section
from app.models.candidate import CandidateProfile
from app.models.resume import TailoredBulletProvenance
from app.auth.deps import member_session, require_member, require_viewer, viewer_session
from app.models.activity import ActivityKind
from app.models.user import User
from app.services import quota
from app.ingestion.pipeline import ingest_raw_job
from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.models.job import Job, JobStatus
from app.resume.editor import parse_edit_form, save_edit
from app.resume.parser import ImportResult, import_resume
from app.resume.pipeline import generate_resume_bundle
from app.resume.tailor import NoActiveCandidateProfileError as TailorNoProfileError
from app.resume.tailor import (
    BaseResumeMissingError,
    _base_docx,
    rebuild_tailored_resume,
    resolve_address,
    resume_download_name,
    tailor_resume,
)
from app.integrations.libreoffice import PdfConversionError, convert_to_pdf
from app.services.jobs import (
    get_job_detail,
    is_description_preview,
    latest_match_analysis,
    list_jobs,
    replace_job_description,
)
from app.services import pool
from app.services.search import query_pairs, search_jobs
from app.models.pool import PoolJob
from app.models.profile import SavedJob
from app.services import postings
from app.integrations.job_boards.adzuna import AdzunaSource
from app.integrations.job_boards.base import RawJob
from app.sources.manual import import_manual_job
from app.integrations.job_boards.settings import AdzunaSettings
from app.ui.diff import inline_word_diff_html, side_by_side_diff
from app.ui.templating import TEMPLATES_DIR, safe_next, templates# noqa: F401  (re-exported)
from app.shared.filenames import sanitize_filename
from app.core.tenancy import require_tenant
from app.shared.terms import split_places, split_terms

router = APIRouter()
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
UPLOAD_DIR = PROJECT_ROOT / "data" / "resumes" / "uploads"
_ALLOWED_RESUME_SUFFIXES = {".docx", ".pdf"}
MAX_RESUME_BYTES = 10_000_000

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
_ANALYSIS_UNAVAILABLE = "Resume analysis is temporarily unavailable. Please try again later."


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


def _source_label(source: str | None) -> str:
    """Where a job came from, in the user's terms (never a provider's name)."""
    source = source or ""
    if source == "manual":
        return "Added by you"
    if source.startswith(("greenhouse", "lever", "ashby")):
        return "Company careers page"
    return "Job board"


templates.env.filters["source_label"] = _source_label


def _tracked_by_external_id(session: Session, external_ids: list[str]) -> dict[str, dict]:
    """For search results already saved as jobs: their id, score and AI fit."""
    if not external_ids:
        return {}
    rows = (
        session.query(Job.external_id, Job.id)
        .filter(Job.source.in_(("adzuna",) + pool.PROVIDERS), Job.external_id.in_(external_ids))
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
    """One page of results: the live job-board search across every title x
    location (app/services/search.py) merged with the free job pool of
    companies' careers sites (app/services/pool.py), which carries full
    descriptions -- a board result the pool also has is shown from the pool.
    Nothing here saves anything: a posting only becomes a tracked `Job` when
    the user analyzes or tracks it. An empty page past the last real results
    is the "no more results" signal.
    """
    page = max(page, 1)
    result = {"error": None, "cards": [], "page": page, "has_next": False, "failed": [],
              "queries": len(query_pairs(titles, locations)), "requests": 0}

    board_jobs: list[RawJob] = []
    board_error = None
    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        logger.error("Job search is not configured (listings provider keys missing)")
        board_error = "Job search is temporarily unavailable. Please try again later."
    else:
        source = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
        per_query = max(10, min(50, _CARDS_PER_PAGE // result["queries"]))
        try:
            found = search_jobs(source, titles, locations, page=page, per_page=per_query,
                                keywords=keywords, areas=location_paths(session))
        except Exception:
            logger.exception("Job search failed")
            board_error = "Job search is temporarily unavailable. Please try again shortly."
        else:
            board_jobs = found.jobs
            result.update(has_next=found.has_next, failed=found.failed_queries, requests=found.requests)

    try:
        pooled = pool.search_pool(session, titles, locations, keywords, country, page=page, per_page=_POOL_PER_PAGE)
    except Exception:
        logger.exception("Job pool search failed")
        pooled = pool.PoolResults(jobs=[], has_next=False, total=0)
    if board_error and not pooled.jobs:
        result["error"] = board_error
        return result

    jobs = _merge_results(pooled.jobs, board_jobs)
    tracked = _tracked_by_external_id(session, [j.external_id for j in jobs if j.external_id])
    cards = [{"raw": j, "tracked": tracked.get(j.external_id)} for j in jobs]
    result["cards"] = _best_match_first(cards, lambda card: card["tracked"])
    result["has_next"] = result["has_next"] or pooled.has_next
    return result


_POOL_PER_PAGE = 20


def _posted_at(job: RawJob) -> float:
    if job.date_posted is None:
        return 0.0
    posted = job.date_posted if job.date_posted.tzinfo else job.date_posted.replace(tzinfo=timezone.utc)
    return posted.timestamp()


def _merge_results(pool_jobs: list[RawJob], board_jobs: list[RawJob]) -> list[RawJob]:
    """Both lists, newest first; a board posting the pool also has (same company
    and title) is dropped -- the pool's copy has the full description."""
    in_pool = {(pool.normalize(j.company, company=True), pool.normalize(j.title)) for j in pool_jobs}
    merged = list(pool_jobs) + [j for j in board_jobs
                                if (pool.normalize(j.company, company=True), pool.normalize(j.title)) not in in_pool]
    return sorted(merged, key=_posted_at, reverse=True)


def _search_summary(titles: list[str], locations: list[str], keywords: list[str], country: str) -> str:
    parts = [", ".join(titles) or "any title", "in " + (", ".join(locations) or "anywhere")]
    if keywords:
        parts.append("with " + ", ".join(keywords))
    return " ".join(parts) + f" ({country.upper()})"


def _limited_search(session: Session, user: User, titles: list[str], locations: list[str], keywords: list[str],
                    country: str, page: int, query_string: str) -> dict:
    """The job search behind the hourly search limit (app/services/quota.py): a
    new search counts once; its other pages, or running it again within the
    hour, are free. Over the limit, the page offers the last search instead."""
    platform = quota.platform_limits(session)
    fingerprint = quota.search_fingerprint(titles, locations, keywords, country)
    summary = _search_summary(titles, locations, keywords, country)
    try:
        charge = quota.charge_search(
            session, user, fingerprint=fingerprint, page=page, summary=summary, platform=platform,
            meta={"titles": titles, "locations": locations, "keywords": keywords, "country": country,
                  "query_string": query_string},
        )
    except quota.LimitReached as exc:
        current = exc.allowance or quota.allowance(session, user, quota.SEARCH, platform)
        return {"error": str(exc), "limited": True, "cards": [], "page": page, "has_next": False, "failed": [],
                "queries": 0, "allowance": current, "last_search": _last_search(session, user, fingerprint)}
    search = _search_for_page(session, titles=titles, locations=locations, keywords=keywords, country=country,
                              page=page)
    quota.settle_search(session, user, charge, provider_calls=search.get("requests", 0), failed=bool(search.get("error")),
                        summary=summary, fingerprint=fingerprint, page=page)
    search["has_next"] = search["has_next"] and page < platform.max_search_pages
    search["allowance"] = charge.allowance
    return search


def _last_search(session: Session, user: User, fingerprint: str) -> dict | None:
    """The most recent search counted this hour (other than this one), to offer
    when the limit stops a new one."""
    from app.models.activity import ActivityEvent

    event = session.query(ActivityEvent).filter(
        ActivityEvent.user_id == user.id, ActivityEvent.kind == ActivityKind.SEARCH,
        ActivityEvent.ref != fingerprint, ActivityEvent.created_at >= quota.utcnow() - quota.WINDOW,
    ).order_by(ActivityEvent.created_at.desc()).first()
    if event is None or not (event.meta or {}).get("query_string"):
        return None
    return {"summary": event.summary, "url": "/jobs?" + event.meta["query_string"]}


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
    """Jobs the user is working on. A job that's only been saved (never
    analyzed) lives on the Saved jobs page instead."""
    saved_ids = {job_id for (job_id,) in session.query(SavedJob.job_id)}
    rows = [{"job": j, "score": (a.overall_score if (a := latest_match_analysis(session, j.id)) else None),
             "fit": latest_assessment(session, j.id)} for j in list_jobs(session, status=status, min_score=min_score)]
    rows = [r for r in rows if not (r["job"].id in saved_ids and r["job"].status == JobStatus.DISCOVERED
                                    and r["score"] is None and r["fit"] is None)]
    return _best_match_first(rows, lambda row: row)


def _picked(values: list[str], split) -> list[str]:
    """Picker values (one per tag) -- or a comma list typed into a URL -- as a clean list."""
    return list(dict.fromkeys(term for value in values for term in split(value)))


def _search_terms(session: Session, q: str | None, l: str | None, titles, locations, keywords,
                  country: str | None) -> tuple[list[str], list[str], list[str], str, str | None]:
    """What to search: the boxes as typed (comma-separated), else the
    `titles`/`locations`/`keywords` lists of older links, else the saved
    default search (Settings -> Job search)."""
    config_error = None
    try:
        search_config = load_app_config(session, DEFAULT_CONFIG_PATH).search
    except ConfigError as exc:
        config_error = f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
        search_config = GlobalSearchConfig()
    typed = q is not None or l is not None or titles is not None or locations is not None
    title_list = split_terms(q or "") if q is not None else (
        _picked(titles, split_terms) if titles is not None else ([] if typed else search_config.titles))
    location_list = split_places(l or "") if l is not None else (
        _picked(locations, split_places) if locations is not None else ([] if typed else search_config.locations))
    keyword_list = _picked(keywords, split_terms) if keywords is not None else ([] if typed else search_config.keywords)
    return title_list, location_list, keyword_list, (country or search_config.country).strip().lower(), config_error


def _saved_job_keys(session: Session) -> dict[tuple[str, str], int]:
    """(source, external id) -> job id, for every job the user saved."""
    rows = session.query(Job.source, Job.external_id, Job.id).join(SavedJob, SavedJob.job_id == Job.id).all()
    return {(source, external_id): job_id for source, external_id, job_id in rows if external_id}


@router.get("/jobs")
def jobs_page(
    request: Request,
    q: str | None = None,
    l: str | None = None,
    titles: list[str] | None = Query(None),
    locations: list[str] | None = Query(None),
    keywords: list[str] | None = Query(None),
    country: str | None = None,
    page: int = 1,
    view: str | None = None,
    workplace: list[str] | None = Query(None),
    job_type: list[str] | None = Query(None, alias="type"),
    level: list[str] | None = Query(None),
    salary: str | None = None,
    days: str | None = None,
    msg: str | None = None,
    error: str | None = None,
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
):
    """Job search results: filters on the left, results in the middle, the
    selected job's details on the right (full screen on phones).

    Only the search terms (what / where / country) make a new search -- which
    counts toward the hourly limit. Filters narrow what's already been found,
    so changing them is free, and they live in the URL.
    """
    title_list, location_list, keyword_list, country, config_error = _search_terms(
        session, q, l, titles, locations, keywords, country)
    remember_user_terms(session, title=title_list, location=location_list, keyword=keyword_list)

    search_params = {"q": ", ".join(title_list), "l": ", ".join(location_list), "country": country}
    if keyword_list:
        search_params["keywords"] = keyword_list
    query_string = urlencode(search_params, doseq=True)
    search = _limited_search(session, user, title_list, location_list, keyword_list, country, page, query_string)

    filters = postings.Filters.parse(workplace, job_type, level, salary, days)
    tracked = {}
    found = []
    for card in search["cards"]:
        posting = postings.from_raw(card["raw"])
        found.append(posting)
        tracked[posting.ref] = card["tracked"]
    postings.remember(found)
    shown = [p for p in found if filters.matches(p)]
    saved = _saved_job_keys(session)
    selected = next((p for p in shown if p.ref == view), shown[0] if shown else None)

    filter_params = {"workplace": list(filters.workplaces), "type": list(filters.employment_types),
                     "level": list(filters.levels), "salary": filters.min_salary or "", "days": filters.days or ""}
    page_base = "/jobs?" + urlencode({**search_params, **{k: v for k, v in filter_params.items() if v}}, doseq=True)
    return templates.TemplateResponse(request, "jobs.html", {
        "search": search, "postings": shown, "found_count": len(found), "tracked": tracked,
        "saved": {p.ref: saved.get((p.source, p.external_id)) for p in shown}, "selected": selected,
        "filters": filters, "q": search_params["q"], "l": search_params["l"], "country": country,
        "keywords": keyword_list, "page_base": page_base, "error": config_error or error, "message": msg,
        "has_resume": active_candidate(session) is not None,
        "workplaces": postings.WORKPLACES, "employment_types": postings.EMPLOYMENT_TYPES,
        "levels": postings.EXPERIENCE_LEVELS, "date_ranges": postings.DATE_RANGES,
        "salaries": [40000, 60000, 80000, 100000, 120000, 150000],
    })


@router.get("/jobs/posting/{ref}")
def posting_page(request: Request, ref: str, session: Session = Depends(member_session)):
    """One search result on its own page (links from the landing page, and
    the no-script fallback for "View details")."""
    posting = postings.get(session, ref)
    if posting is None:
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available. Try a new search."}),
                                status_code=303)
    tracked = _tracked_by_external_id(session, [posting.external_id] if posting.external_id else [])
    saved = _saved_job_keys(session)
    return templates.TemplateResponse(request, "posting.html", {
        "p": posting, "tracked": tracked.get(posting.external_id), "saved_job_id": saved.get((posting.source,
                                                                                               posting.external_id)),
        "has_resume": active_candidate(session) is not None})


@router.get("/applications")
def applications_page(request: Request, status: str | None = None, min_score: str | None = None,
                      session: Session = Depends(member_session)):
    status, min_score = _tracked_filters(status, min_score)
    return templates.TemplateResponse(request, "applications.html", {
        "jobs": _tracked_rows(session, status, min_score), "status": status, "min_score": min_score,
        "statuses": [s.value for s in JobStatus]})


# ---- saved jobs ------------------------------------------------------------------------

def save_posting(session: Session, user: User, posting: postings.Posting) -> Job:
    """Keep a search result as the user's own job, on their Saved jobs list."""
    job = ingest_raw_job(session, posting.as_raw_job()).job
    if session.query(SavedJob).filter(SavedJob.job_id == job.id).first() is None:
        session.add(SavedJob(job_id=job.id))
        session.commit()
        quota.record(session, user.id, ActivityKind.JOB_SAVED, quota.job_label(job), meta={"job_id": job.id})
    return job


def _save_button(request: Request, ref: str, job_id: int | None):
    return templates.TemplateResponse(request, "_save_button.html", {"ref": ref, "saved_job_id": job_id})


@router.post("/saved")
def save_job(request: Request, ref: str = Form(...), next: str = Form("/saved"),
             session: Session = Depends(member_session), user: User = Depends(require_member)):
    posting = postings.get(session, ref)
    if posting is None:
        if request.headers.get("hx-request"):
            return HTMLResponse('<span class="muted">This job is no longer available.</span>')
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available."}), status_code=303)
    job = save_posting(session, user, posting)
    if request.headers.get("hx-request"):
        return _save_button(request, ref, job.id)
    return RedirectResponse(url=safe_next(next, "/saved"), status_code=303)


@router.post("/saved/{job_id}/remove")
def unsave_job(request: Request, job_id: int, ref: str = Form(""), next: str = Form("/saved"),
               session: Session = Depends(member_session)):
    session.query(SavedJob).filter(SavedJob.job_id == job_id).delete(synchronize_session=False)
    session.commit()
    if request.headers.get("hx-request") and ref:
        return _save_button(request, ref, None)
    return RedirectResponse(url=safe_next(next, "/saved"), status_code=303)


@router.get("/saved")
def saved_page(request: Request, session: Session = Depends(member_session)):
    rows = session.query(SavedJob, Job).join(Job, SavedJob.job_id == Job.id).order_by(SavedJob.created_at.desc()).all()
    return templates.TemplateResponse(request, "saved.html", {"rows": rows})


@router.get("/tracked-jobs")
def tracked_jobs_table(
    request: Request,
    status: str | None = None,
    min_score: str | None = None,
    session: Session = Depends(member_session),
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
    country: str = Form("ca"), session: Session = Depends(member_session),
):
    """"Save as default": the current search box values become Settings -> Job search."""
    try:
        current = load_app_config(session, DEFAULT_CONFIG_PATH).search
        save_section(session, "search", {
            **current.model_dump(), "titles": _picked(titles, split_terms),
            "locations": _picked(locations, split_places), "keywords": _picked(keywords, split_terms), "country": country,
        })
    except ConfigError as exc:
        return RedirectResponse(url=f"/jobs?{urlencode({'msg': 'Not saved: ' + '; '.join(exc.errors)})}", status_code=303)
    return RedirectResponse(url=f"/jobs?{urlencode({'msg': 'Saved as your default search.'})}", status_code=303)


@router.post("/resume/upload")
async def upload_resume(
    resume: UploadFile = File(...), next: str = Form("/"), session: Session = Depends(member_session)
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


def _use_full_description(session: Session, job: Job) -> None:
    """A job-board job still holding the ~500-character preview gets the full
    description from the job pool, when the pool has the same job, before
    it's analyzed."""
    if not is_description_preview(job):
        return
    full = pool.full_description_for(session, job.company, job.title)
    if full and len(full) > len(job.description or ""):
        try:
            replace_job_description(session, job, full)
        except ValueError:
            pass


def _analyze_and_tailor(session: Session, job: Job, *, force: bool = False) -> str | None:
    """Score `job` and, if it qualifies (or `force`: "Tailor anyway"), generate
    its tailored resume. Returns a human-readable reason when that didn't happen
    (so the dashboard can show it instead of leaving the job silently stuck at
    DISCOVERED), else None.
    """
    _use_full_description(session, job)
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
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _ANALYSIS_UNAVAILABLE
    return None


async def _import_uploaded_resume(session: Session, resume: UploadFile) -> tuple[ImportResult | None, str | None]:
    """Save an uploaded resume and import it as the active candidate profile
    (a no-op if its content matches the current one). Returns (result, None),
    or (None, a message for the user) if the file can't be used.
    """
    suffix = Path(resume.filename or "").suffix.lower()
    if suffix not in _ALLOWED_RESUME_SUFFIXES:
        return None, "Resume must be a .docx or .pdf file."
    data = await resume.read(MAX_RESUME_BYTES + 1)
    if not data:
        return None, "That file is empty."
    if len(data) > MAX_RESUME_BYTES:
        return None, f"That file is over {MAX_RESUME_BYTES // 1_000_000} MB -- resumes are much smaller."
    user_dir = UPLOAD_DIR / f"user_{require_tenant(session)}"  # each user's files in their own folder
    user_dir.mkdir(parents=True, exist_ok=True)
    # Unique even for two uploads in the same second (the second, if identical,
    # is deleted below -- it must never be the file the active resume points at).
    dest_name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{sanitize_filename(Path(resume.filename).stem)}{suffix}"
    dest_path = user_dir / dest_name
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
    if result.created:
        quota.record(session, require_tenant(session), ActivityKind.RESUME_UPLOAD, f"Version {result.profile.version}")
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
                session: Session = Depends(member_session)):
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
async def resume_page_upload(resume: UploadFile = File(...), session: Session = Depends(member_session)):
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


@router.get("/resume/edit")
def resume_edit_page(request: Request, session: Session = Depends(member_session)):
    current = active_candidate(session)
    if current is None:
        return RedirectResponse(url="/resume?" + urlencode({"error": "Upload your resume first, then edit it here."}),
                                status_code=303)
    return templates.TemplateResponse(request, "resume_edit.html", {"current": current, "errors": []})


@router.post("/resume/edit")
async def resume_edit_save(request: Request, session: Session = Depends(member_session)):
    """Save edited resume content as a new version (see app/resume/editor.py)."""
    current = active_candidate(session)
    if current is None:
        return RedirectResponse(url="/resume", status_code=303)
    form = await request.form()
    edit, errors = parse_edit_form(form)

    def again(**extra):
        return templates.TemplateResponse(request, "resume_edit.html", {
            "current": current, "errors": errors, "form": form, **extra}, status_code=400)

    if errors:
        return again()
    try:
        base_docx = _base_docx(current)
    except BaseResumeMissingError:
        base_docx = None  # nothing to keep: the standard layout it is
    result = save_edit(session, current, edit, base_docx=base_docx, upload_dir=UPLOAD_DIR,
                       allow_layout_change=form.get("allow_layout_change") == "1")
    if result.needs_layout_consent:
        return again(layout_reasons=result.reasons)
    message = f"Saved as version {result.profile.version}."
    quota.record(session, require_tenant(session), ActivityKind.RESUME_EDIT, f"Version {result.profile.version}"
                 + (" (standard layout)" if result.layout_changed else ""))
    if result.layout_changed:
        message += " Your resume now uses the standard layout."
    return RedirectResponse(url="/resume?" + urlencode({"msg": message}), status_code=303)


@router.get("/resume/file")
def download_base_resume(session: Session = Depends(member_session)):
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
            "minimum_score": minimum_score, "ai_allowance": _ai_allowance(request, session),
        },
    )


def _ai_allowance(request: Request, session: Session) -> quota.Allowance | None:
    user = getattr(request.state, "user", None)
    return quota.allowance(session, user, quota.AI) if user is not None else None


@router.get("/jobs/{job_id}")
def job_detail_page(request: Request, job_id: int, error: str | None = None, msg: str | None = None,
                    session: Session = Depends(member_session)):
    return _job_detail_response(request, session, job_id, error=error, message=msg)


@router.post("/jobs/{job_id}/description")
def update_job_description(job_id: int, description: str = Form(...), session: Session = Depends(member_session),
                           user: User = Depends(require_member)):
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
    rescore = latest_match_analysis(session, job_id) is not None
    reassess = latest_assessment(session, job_id) is not None
    if rescore or reassess:
        try:  # one resume action, however many provider calls it takes
            config = load_app_config(session, DEFAULT_CONFIG_PATH)
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
            notes.append(_ANALYSIS_UNAVAILABLE)
        except Exception:
            session.rollback()
            logger.exception("Match analysis failed")
            notes.append("The analysis couldn't be completed. Please try again in a moment.")
    anchor = "fit" if latest_assessment(session, job_id) is not None else "description"
    return RedirectResponse(url=f"/jobs/{job_id}?{urlencode({'msg': ' '.join(notes)})}#{anchor}", status_code=303)


@router.post("/jobs/{job_id}/retailor")
def retailor_job(job_id: int, session: Session = Depends(member_session), user: User = Depends(require_member)):
    """Re-tailor an already-tailored job with AI (whatever its score), in
    your resume's own design, without changing the job's status."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        with quota.ai_action(session, user, ActivityKind.AI_TAILOR, quota.job_label(job), job_id=job.id):
            result = tailor_resume(session, job, config, update_status=False)
    except quota.LimitReached as exc:
        return _job_redirect(job_id, error=str(exc))
    except ConfigError as exc:
        return _job_redirect(job_id, error=f"Settings are invalid: {'; '.join(exc.errors)}")
    except TailorNoProfileError as exc:
        return _job_redirect(job_id, error=str(exc))
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _job_redirect(job_id, error=_ANALYSIS_UNAVAILABLE)
    try:
        result.tailored_resume.pdf_path = str(convert_to_pdf(result.docx_path))
        session.commit()
    except PdfConversionError:
        pass  # DOCX is still there; PDF needs LibreOffice
    return _job_redirect(job_id, msg="Resume re-tailored -- download it below.")


@router.post("/jobs/{job_id}/removals")
async def apply_removals(job_id: int, request: Request, session: Session = Depends(member_session)):
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
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
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
        try:
            with quota.ai_action(session, user, ActivityKind.AI_TAILOR, quota.job_label(job), job_id=job.id):
                error = _analyze_and_tailor(session, job, force=force)
        except quota.LimitReached as exc:
            error = str(exc)
    if error is not None:
        return _job_detail_response(request, session, job_id, error=error)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


def _assess_fit(session: Session, job: Job, *, force: bool, user: User) -> str | None:
    """Score (free, deterministic) then run the AI fit assessment. Returns
    a human-readable reason when the assessment couldn't run, else None.
    Counts toward the user's hourly resume actions when it calls the provider
    (an existing assessment, not re-run, is free).
    """
    if not force and latest_assessment(session, job.id) is not None:
        return None  # already analyzed: nothing to run
    _use_full_description(session, job)
    try:
        config = load_app_config(session, DEFAULT_CONFIG_PATH)
        with quota.ai_action(session, user, ActivityKind.AI_ANALYSIS, quota.job_label(job), job_id=job.id):
            if job.status == JobStatus.DISCOVERED:
                analyze_job(session, job, config)
            assess_job_fit(session, job, config, force=force)
    except quota.LimitReached as exc:
        return str(exc)
    except ConfigError as exc:
        return f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
    except NoActiveCandidateProfileError:
        return "No resume yet -- upload your resume below, then click \"Analyze match\"."
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _ANALYSIS_UNAVAILABLE
    except Exception:  # the provider call itself (network, quota, bad response)
        session.rollback()
        logger.exception("Match analysis failed")
        return "The analysis couldn't be completed. Please try again in a moment."
    return None


@router.post("/jobs/{job_id}/assess")
def assess_job_action(job_id: int, session: Session = Depends(member_session), user: User = Depends(require_member)):
    job = _get_job_or_404(session, job_id)
    error = _assess_fit(session, job, force=latest_assessment(session, job_id) is not None, user=user)
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job_id}{query}#fit", status_code=303)


@router.get("/jobs/{job_id}/resume.docx")
def download_resume_docx(job_id: int, session: Session = Depends(member_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.docx_path:
        raise HTTPException(status_code=404, detail="No tailored resume DOCX for this job")
    return FileResponse(detail.tailored_resume.docx_path, filename=_download_name(session, detail, ".docx"))


@router.get("/jobs/{job_id}/resume.pdf")
def download_resume_pdf(job_id: int, session: Session = Depends(member_session)):
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
    def handler(job_id: int, session: Session = Depends(member_session)):
        job = _get_job_or_404(session, job_id)
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


def _quick_form_response(request: Request, *, error: str, status_code: int = 400):
    return templates.TemplateResponse(request, "quick.html", {"error": error}, status_code=status_code)


@router.get("/quick")
def quick_tailor_form(request: Request, session: Session = Depends(member_session)):
    return templates.TemplateResponse(request, "quick.html", {"error": None,
                                                              "ai_allowance": _ai_allowance(request, session)})


@router.post("/quick")
async def quick_tailor_submit(
    request: Request,
    resume: UploadFile | None = File(None),
    description: str = Form(...),
    url: str = Form(""),
    company: str = Form(""),
    title: str = Form(""),
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
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
    try:
        quota.check_ai(session, user)  # say so before saving anything
    except quota.LimitReached as exc:
        return _quick_form_response(request, error=str(exc), status_code=429)

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
        with quota.ai_action(session, user, ActivityKind.QUICK_TAILOR, quota.job_label(job), job_id=job.id):
            try:
                analyze_job(session, job, config)
            except NoActiveCandidateProfileError:
                pass  # the import above just created one; this can't actually happen
            generate_resume_bundle(session, job, config)
    except quota.LimitReached as exc:
        return _quick_form_response(request, error=str(exc), status_code=429)
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _quick_form_response(request, error=_ANALYSIS_UNAVAILABLE, status_code=503)
    except NoActiveCandidateProfileError:
        return _quick_form_response(request, error="Upload your resume first (here, or on the My resume page).")

    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


def _posted_job(
    session: Session, pool_id: str, external_id: str, source_url: str, company: str, title: str, location: str,
    description: str, date_posted: str, ref: str = "",
) -> RawJob:
    """The posting a search result's button sends: by `ref` (app/services/
    postings.py) when it has one. A careers-site posting is re-read from the
    job pool (its full text, as published); a job-board one comes from the
    remembered search result -- or the form's fields -- with the pool's full
    description when the pool has the same job."""
    remembered = postings.get(session, ref) if ref else None
    if remembered is not None:
        raw = remembered.as_raw_job()
        if not remembered.company_site:
            raw.description = pool.full_description_for(session, raw.company, raw.title) or raw.description
        return raw
    if pool_id.isdigit():
        pooled = session.get(PoolJob, int(pool_id))
        if pooled is not None:
            return pool.to_raw_job(pooled)
    parsed_date: datetime | None = None
    if date_posted:
        try:
            parsed_date = datetime.fromisoformat(date_posted)
        except ValueError:
            parsed_date = None
    full = pool.full_description_for(session, company, title)
    return RawJob(
        external_id=external_id or None, source="adzuna", source_url=source_url, company=company, title=title,
        location=location or None, description=full or description, date_posted=parsed_date,
    )


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
    """A search card's "Analyze with AI": saves the posting as a tracked
    job (the posting fields ride along as hidden inputs, since Adzuna has no
    fetch-by-id endpoint), then compares it against your resume. Tailoring a
    resume is a separate, explicit step on the job's page.
    """
    raw_job = _posted_job(session, pool_id, external_id, source_url, company, title, location, description,
                          date_posted, ref)
    if not (raw_job.source_url and raw_job.title and raw_job.description):
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available. Search again."}),
                                status_code=303)
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job
    if ingest_result.created:
        quota.record(session, user.id, ActivityKind.JOB_TRACKED, quota.job_label(job), meta={"job_id": job.id})
    error = _assess_fit(session, job, force=False, user=user)
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job.id}{query}#fit", status_code=303)


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
    """Turns one searched-but-not-yet-saved Adzuna posting (from the
    dashboard's "Search for jobs" section) into a tracked `Job` row -- the
    only way a search result reaches the database. The posting fields ride
    along as hidden form inputs from the results page rather than being
    re-fetched, since Adzuna's search API has no "fetch by id" endpoint.
    """
    raw_job = _posted_job(session, pool_id, external_id, source_url, company, title, location, description,
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
                error = _analyze_and_tailor(session, job)
        except quota.LimitReached as exc:
            error = f"Job saved. {exc}"
    # The job is tracked either way; a failed analysis just leaves it at
    # whatever status it reached, with the reason shown on its detail page.
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(url=f"/jobs/{job.id}{query}", status_code=303)
