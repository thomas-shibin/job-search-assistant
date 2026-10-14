"""JSON API over the same services the web pages use (docs/ARCHITECTURE.md:
"a future React/Electron/mobile client would talk to the same API with no
backend rework"). Members only; every query runs in the member's own
tenant-bound session.

- ``GET /api/jobs/search`` -- the job search (same hourly limit as the page);
- ``GET /api/jobs/{id}`` -- a tracked job (numeric id) or a search result (ref);
- ``POST/DELETE /api/jobs/{ref}/save`` -- save / unsave a search result;
- ``GET /api/jobs`` -- the member's tracked jobs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.catalog.store import suggestions
from app.auth.deps import member_session, require_member, require_permission
from app.models.job import Job
from app.models.profile import SavedJob
from app.models.user import User
from app.services import postings
from app.models.catalog import TermKind
from app.services.jobs import get_job_detail, latest_match_analysis, list_jobs

router = APIRouter(prefix="/api")


class JobSummary(BaseModel):
    id: int
    company: str
    title: str
    location: str | None
    status: str
    overall_score: int | None


@router.get("/jobs", response_model=list[JobSummary])  # only the signed-in member's jobs
def api_list_jobs(status: str | None = None, min_score: int | None = None,
                  session: Session = Depends(member_session)):
    jobs = list_jobs(session, status=status, min_score=min_score)
    return [
        JobSummary(
            id=j.id,
            company=j.company,
            title=j.title,
            location=j.location,
            status=j.status.value,
            overall_score=(a.overall_score if (a := latest_match_analysis(session, j.id)) else None),
        )
        for j in jobs
    ]


class JobDetailResponse(BaseModel):
    id: int
    company: str
    title: str
    status: str
    overall_score: int | None
    explanation: str | None
    tailored_resume_validation_status: str | None


def _posting_json(p: postings.Posting, saved_job_id: int | None = None, *, full: bool = False) -> dict:
    data = {
        "id": p.ref, "title": p.title, "company": p.company, "location": p.location,
        "employment_type": p.employment_type, "workplace": p.workplace, "experience_level": p.experience_level,
        "salary_min": p.salary_min, "salary_max": p.salary_max, "currency": p.currency,
        "date_posted": p.date_posted.isoformat() if p.date_posted else None, "url": p.url,
        "company_site": p.company_site, "saved": saved_job_id is not None, "saved_job_id": saved_job_id,
    }
    data["description" if full else "snippet"] = p.description if full else p.snippet
    return data


@router.get("/jobs/search")
def api_search_jobs(
    q: str | None = None, l: str | None = None, country: str | None = None, page: int = 1,
    workplace: list[str] | None = Query(None), job_type: list[str] | None = Query(None, alias="type"),
    level: list[str] | None = Query(None), salary: str | None = None, days: str | None = None,
    session: Session = Depends(member_session), user: User = Depends(require_member),
):
    """Search jobs. A new q / l / country counts toward the hourly search limit;
    pages and filters of the same search don't."""
    from urllib.parse import urlencode

    from app.ui.routes import _limited_search, _saved_job_keys, _search_terms

    titles, locations, keywords, country, _ = _search_terms(session, q, l, None, None, None, country)
    query_string = urlencode({"q": ", ".join(titles), "l": ", ".join(locations), "country": country})
    search = _limited_search(session, user, titles, locations, keywords, country, page, query_string)
    if search.get("limited"):
        return JSONResponse({"error": {"code": "search_limit", "message": search["error"]}}, status_code=429)
    if search.get("error"):
        return JSONResponse({"error": {"code": "search_unavailable", "message": search["error"]}}, status_code=503)
    found = [postings.from_raw(card["raw"]) for card in search["cards"]]
    postings.remember(found)
    filters = postings.Filters.parse(workplace, job_type, level, salary, days)
    saved = _saved_job_keys(session)
    return {"page": search["page"], "has_next": search["has_next"],
            "results": [_posting_json(p, saved.get((p.source, p.external_id))) for p in found if filters.matches(p)]}


@router.get("/jobs/{job_id}")
def api_get_job(job_id: str, session: Session = Depends(member_session), user: User = Depends(require_member)):
    """A tracked job by its numeric id, or a search result by its ref (e.g. p123)."""
    if not job_id.isdigit():
        from app.ui.routes import _saved_job_keys

        posting = postings.get(session, job_id)
        if posting is None:
            raise HTTPException(status_code=404, detail="No such job (search results expire after an hour)")
        return _posting_json(posting, _saved_job_keys(session).get((posting.source, posting.external_id)), full=True)
    require_permission(user, "applications.view.own")  # a tracked job, not a search result
    detail = get_job_detail(session, int(job_id))
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    return JobDetailResponse(
        id=detail.job.id,
        company=detail.job.company,
        title=detail.job.title,
        status=detail.job.status.value,
        overall_score=detail.match_analysis.overall_score if detail.match_analysis else None,
        explanation=detail.match_analysis.explanation if detail.match_analysis else None,
        tailored_resume_validation_status=detail.tailored_resume.validation_status if detail.tailored_resume else None,
    )


@router.post("/jobs/{ref}/save", status_code=201)
def api_save_job(ref: str, session: Session = Depends(member_session), user: User = Depends(require_member)):
    from app.ui.routes import save_posting

    posting = postings.get(session, ref)
    if posting is None:
        raise HTTPException(status_code=404, detail="No such job (search results expire after an hour)")
    job = save_posting(session, user, posting)
    return JSONResponse({"saved": True, "job_id": job.id}, status_code=201)


@router.delete("/jobs/{ref}/save")
def api_unsave_job(ref: str, session: Session = Depends(member_session)):
    if ref.isdigit():
        job_ids = [int(ref)]
    else:
        posting = postings.get(session, ref)
        if posting is None:
            raise HTTPException(status_code=404, detail="No such job")
        job_ids = [j for (j,) in session.query(Job.id).filter(Job.source == posting.source,
                                                             Job.external_id == posting.external_id)]
    session.query(SavedJob).filter(SavedJob.job_id.in_(job_ids)).delete(synchronize_session=False)
    session.commit()
    return {"saved": False}


@router.get("/saved-jobs")
def api_saved_jobs(session: Session = Depends(member_session)):
    rows = session.query(SavedJob, Job).join(Job, SavedJob.job_id == Job.id).order_by(SavedJob.created_at.desc())
    return [{"job_id": job.id, "title": job.title, "company": job.company, "location": job.location,
             "url": job.source_url, "saved_at": saved.created_at.isoformat()} for saved, job in rows]


class TermSuggestion(BaseModel):
    label: str
    count: int
    type: str | None


@router.get("/terms/{kind}", response_model=list[TermSuggestion])
def api_terms(kind: TermKind, response: Response, limit: int | None = None,
              session: Session = Depends(member_session)):
    """Search-term catalog for one kind (title/keyword/location/company), most
    common first -- the options behind every tag picker in the dashboard.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    return suggestions(session, kind, limit=limit)
