"""Job search pages: results (/jobs), one posting (/jobs/posting/{ref}),
saving and unsaving results, the Saved jobs page, and "save as default search".
Business logic is in app/modules/jobs/service.py."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.config.store import save_section
from app.modules.auth.deps import member_session, require_member
from app.modules.auth.models import User
from app.modules.jobs import postings
from app.modules.jobs import service as jobs
from app.modules.jobs.catalog.store import remember_user_terms
from app.modules.jobs.models import Job, SavedJob
from app.modules.matching.fit import active_candidate
from app.shared.terms import split_places, split_terms
from app.web.templating import safe_next, templates

router = APIRouter()


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
    title_list, location_list, keyword_list, country, config_error = jobs.search_terms(
        session, q, l, titles, locations, keywords, country)
    remember_user_terms(session, title=title_list, location=location_list, keyword=keyword_list)

    search_params = {"q": ", ".join(title_list), "l": ", ".join(location_list), "country": country}
    if keyword_list:
        search_params["keywords"] = keyword_list
    query_string = urlencode(search_params, doseq=True)
    search = jobs.limited_search(session, user, title_list, location_list, keyword_list, country, page, query_string)

    filters = postings.Filters.parse(workplace, job_type, level, salary, days)
    tracked = {}
    found = []
    for card in search["cards"]:
        posting = postings.from_raw(card["raw"])
        found.append(posting)
        tracked[posting.ref] = card["tracked"]
    postings.remember(found)
    shown = [p for p in found if filters.matches(p)]
    saved = jobs.saved_job_keys(session)
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
    tracked = jobs.tracked_by_external_id(session, [posting.external_id] if posting.external_id else [])
    saved = jobs.saved_job_keys(session)
    return templates.TemplateResponse(request, "posting.html", {
        "p": posting, "tracked": tracked.get(posting.external_id), "saved_job_id": saved.get((posting.source,
                                                                                               posting.external_id)),
        "has_resume": active_candidate(session) is not None})


# ---- saved jobs ------------------------------------------------------------------------

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
    job = jobs.save_posting(session, user, posting)
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


@router.post("/search/defaults")
def save_search_defaults(
    titles: list[str] = Form([]), locations: list[str] = Form([]), keywords: list[str] = Form([]),
    country: str = Form("ca"), session: Session = Depends(member_session),
):
    """"Save as default": the current search box values become Settings -> Job search."""
    try:
        current = user_config(session).search
        save_section(session, "search", {
            **current.model_dump(), "titles": jobs.picked(titles, split_terms),
            "locations": jobs.picked(locations, split_places), "keywords": jobs.picked(keywords, split_terms),
            "country": country,
        })
    except ConfigError as exc:
        return RedirectResponse(url=f"/jobs?{urlencode({'msg': 'Not saved: ' + '; '.join(exc.errors)})}", status_code=303)
    return RedirectResponse(url=f"/jobs?{urlencode({'msg': 'Saved as your default search.'})}", status_code=303)
