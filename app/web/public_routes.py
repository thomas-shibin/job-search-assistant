"""Public pages: the landing page, companies, career resources, terms and
privacy -- plus the one-time "build your profile" step after signing in.

Everything shown publicly is real: categories, recent jobs and companies come
from the job pool (companies' own careers sites). Seeing search results needs
an account; the landing page's search asks visitors to sign in first and then
runs exactly what they typed.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database.session import get_session
from app.modules.auth.deps import current_user, member_session, require_member
from app.modules.auth.models import User
from app.modules.jobs import postings
from app.modules.jobs.pool import service as pool
from app.modules.jobs.pool.models import PoolCompany, PoolJob
from app.modules.profiles import service as profiles
from app.web.templating import safe_next, templates

router = APIRouter()

CATEGORIES = [
    ("Software Development", "Software Engineer", "code"),
    ("Data & Analytics", "Data", "chart"),
    ("Product Management", "Product Manager", "target"),
    ("Design", "Designer", "pen"),
    ("Marketing", "Marketing", "send"),
    ("Sales", "Sales", "coins"),
    ("Finance & Accounting", "Finance", "file"),
    ("Customer Success", "Customer", "user"),
    ("Engineering Management", "Engineering Manager", "briefcase"),
    ("Security", "Security", "shield"),
    ("Operations", "Operations", "sliders"),
    ("Recruiting & People", "Recruit", "search"),
]

_cache: dict[str, tuple[float, object]] = {}
_CACHE_SECONDS = 600


def _cached(key: str, build):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = build()
    _cache[key] = (time.monotonic(), value)
    return value


def _landing_data(session: Session) -> dict:
    def build():
        categories = []
        for label, query, icon in CATEGORIES:
            count = pool.search_pool(session, [query], [], [], "ca", per_page=1).total
            categories.append({"label": label, "query": query, "icon": icon, "count": count})
        recent_rows = session.query(PoolJob).join(PoolCompany, PoolJob.company_id == PoolCompany.id).filter(
            PoolCompany.enabled.is_(True), PoolJob.country == "ca", PoolJob.date_posted.is_not(None)).order_by(
            PoolJob.date_posted.desc()).limit(6).all()
        recent = [postings.from_raw(pool.to_raw_job(r)) for r in recent_rows]
        companies = session.query(PoolCompany).filter(PoolCompany.enabled.is_(True), PoolCompany.canada_count > 0) \
            .order_by(PoolCompany.canada_count.desc()).limit(8).all()
        companies = [{"name": c.label, "count": c.canada_count} for c in companies]
        total = session.query(func.count(PoolJob.id)).filter(PoolJob.country == "ca").scalar() or 0
        return {"categories": categories, "recent": recent, "companies": companies, "total": total}
    return _cached("landing", build)


@router.get("/")
def landing(request: Request, msg: str | None = None, user: User | None = Depends(current_user),
            session: Session = Depends(get_session)):
    if user is not None and not user.is_guest:
        return RedirectResponse(url="/jobs" + (f"?{urlencode({'msg': msg})}" if msg else ""), status_code=303)
    return templates.TemplateResponse(request, "landing.html", {**_landing_data(session), "message": msg})


@router.get("/companies")
def companies_page(request: Request, q: str = "", user: User | None = Depends(current_user),
                   session: Session = Depends(get_session)):
    query = session.query(PoolCompany).filter(PoolCompany.enabled.is_(True), PoolCompany.canada_count > 0)
    if q.strip():
        query = query.filter(PoolCompany.name.ilike(f"%{q.strip()[:80]}%"))
    rows = query.order_by(PoolCompany.canada_count.desc(), PoolCompany.name).all()
    return templates.TemplateResponse(request, "companies.html", {"companies": rows, "q": q})


@router.get("/resources")
def resources_page(request: Request, user: User | None = Depends(current_user)):
    return templates.TemplateResponse(request, "resources.html", {})


@router.get("/terms")
def terms_page(request: Request, user: User | None = Depends(current_user)):
    return templates.TemplateResponse(request, "legal/terms.html", {})


@router.get("/access-denied")
def access_denied_page(request: Request, reason: str = "forbidden", user: User | None = Depends(current_user)):
    """Where htmx navigation lands after a 403 (it can't swap a 403 page in)."""
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    reason = reason if reason in {"forbidden", "feature_disabled"} else "forbidden"
    return templates.TemplateResponse(request, "access_denied.html", {"reason": reason}, status_code=403)


@router.get("/privacy")
def privacy_page(request: Request, user: User | None = Depends(current_user)):
    return templates.TemplateResponse(request, "legal/privacy.html", {})


# ---- the optional profile step after signing in ----------------------------------------

@router.get("/welcome")
def welcome_page(request: Request, next: str = "/jobs", user: User = Depends(require_member),
                 session: Session = Depends(member_session)):
    return templates.TemplateResponse(request, "welcome.html", {
        "next": safe_next(next, "/jobs"), "completion": profiles.completion(session, user)})


@router.post("/welcome")
def welcome_choice(choice: str = Form("search"), next: str = Form("/jobs"), session: Session = Depends(member_session)):
    """Either button means "don't ask again": the profile stays incomplete
    until they fill it in, from the Profile page, whenever they like."""
    profiles.dismiss_prompt(session)
    if choice == "profile":
        return RedirectResponse(url="/profile?" + urlencode({"next": safe_next(next, "/jobs")}), status_code=303)
    return RedirectResponse(url=safe_next(next, "/jobs"), status_code=303)
