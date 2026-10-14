"""Admin-only pages for running the platform: settings, usage, health, and
the activity dashboard (what each member does, what it costs, hourly-limit
overrides, suspending an account). Admins see account details and activity
-- search terms and the titles of jobs acted on -- but never a user's resume,
tailored resumes, job descriptions or settings (see app/admin/activity.py)."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.ai.settings import AISettings
from app.admin import access
from app.admin import activity as activity_data
from app.auth import rbac
from app.auth.deps import admin_session, require_admin
from app.catalog.scan import scan_status, start_background_scan
from app.catalog.store import counts as catalog_counts
from app.config.loader import ConfigError
from app.config.schema import AppConfig
from app.config.store import PLATFORM_SECTIONS, load_app_config, save_section
from app.database.session import SessionLocal
from app.models.ai_usage import AICallLog
from app.models.activity import ActivityKind
from app.models.pool import PoolCompany, PoolJob
from app.services import pool
from app.models.user import Role, User
from app.scheduler import background
from app.services.costs import ai_cost_summary
from app.services.health import check_health
from app.sources.adzuna import AdzunaSource
from app.sources.settings import AdzunaSettings
from app.ui.forms import parse_form, section_form
from app.ui.routes import DEFAULT_CONFIG_PATH
from app.ui.templating import templates

router = APIRouter(prefix="/admin")

PLATFORM_DESCRIPTIONS = {
    "platform": "Limits shared by every member",
    "scheduler": "Automatic scans for every member",
    "retention": "Delete old jobs automatically -- members' settings, searches and watches are never deleted",
    "ai": "Analysis provider",
}


def _redirect(path: str, **params: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?{urlencode(params)}" if params else path, status_code=303)


def _load(session: Session) -> AppConfig:
    try:
        return load_app_config(session, DEFAULT_CONFIG_PATH)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Platform settings are invalid: {'; '.join(exc.errors)}")


def _settings_page(request: Request, session: Session, *, message: str | None = None, error: str | None = None,
                   error_section: str | None = None, status_code: int = 200):
    config = _load(session)
    dumped = config.model_dump(mode="json")
    sections = [
        {"key": key, "description": PLATFORM_DESCRIPTIONS[key],
         "form": section_form(AppConfig.model_fields[key].annotation, key, dumped[key])}
        for key in ("platform", "scheduler", "retention", "ai")
    ]
    adzuna = AdzunaSettings()
    integrations = {
        "Analysis provider": bool(AISettings().openai_api_key),
        "Job listings provider": bool(adzuna.app_id and adzuna.app_key),
    }
    return templates.TemplateResponse(request, "admin/settings.html", {
        "sections": sections, "message": message, "error": error, "error_section": error_section,
        "integrations": integrations, "scheduler_running": background.is_running(),
        "next_scan": background.next_scan_time(), "catalog": {"counts": catalog_counts(session), **scan_status()},
    }, status_code=status_code)


@router.get("/settings")
def platform_settings(request: Request, msg: str | None = None, session: Session = Depends(admin_session)):
    return _settings_page(request, session, message=msg)


def _redact(value):
    if isinstance(value, dict):
        return {k: ("***" if any(w in k.lower() for w in ("key", "secret", "token", "password")) else _redact(v))
                for k, v in value.items()}
    return value


@router.post("/settings/{section}")
async def save_platform_section(request: Request, section: str, session: Session = Depends(admin_session),
                                admin: User = Depends(require_admin)):
    if section not in PLATFORM_SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown platform section")
    form = await request.form()
    value = parse_form((k, v) for k, v in form.multi_items() if isinstance(v, str)).get(section)
    old = _load(session).model_dump(mode="json").get(section)
    try:
        config = save_section(session, section, value)
    except ConfigError as exc:
        return _settings_page(request, session, error="; ".join(exc.errors), error_section=section, status_code=400)
    new = config.model_dump(mode="json").get(section)
    if old != new:
        access.audit(session, admin, "settings.update", "settings", section, _redact(old), _redact(new), request)
        session.commit()
    background.apply_schedule(config)
    return RedirectResponse(url=f"/admin/settings?{urlencode({'msg': 'Saved.'})}#{section}", status_code=303)


@router.post("/scan-now")
def scan_now(request: Request, session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    if not background.trigger_scan_now():
        return _redirect("/admin/settings", msg="Scans only run while the server is running (serve / Docker).")
    access.audit(session, admin, "platform.scan", "scheduler", None, None, {"scan": "all members"}, request)
    session.commit()
    return _redirect("/admin/settings", msg="Scan started for every member in the background.")


@router.post("/catalog-scan")
def catalog_scan(session: Session = Depends(admin_session)):
    """Refresh the shared search-term catalog from a quick market scan."""
    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        return _redirect("/admin/settings", msg="The job listings provider isn't configured.")
    adzuna = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country="ca")
    if not start_background_scan(SessionLocal, adzuna, "ca"):
        return _redirect("/admin/settings", msg="A catalog scan is already running.")
    return RedirectResponse(url=f"/admin/settings?{urlencode({'msg': 'Catalog scan started -- about 2 minutes.'})}#catalog",
                            status_code=303)


@router.get("/usage")
def usage(request: Request, session: Session = Depends(admin_session)):
    accounts = dict(session.query(User.role, func.count(User.id)).group_by(User.role).all())
    active_members = session.query(func.count(func.distinct(AICallLog.user_id))).scalar()
    return templates.TemplateResponse(request, "costs.html", {
        "summary": ai_cost_summary(session),
        "accounts": {"members": accounts.get(Role.MEMBER.value, 0) + accounts.get(Role.ADMIN.value, 0),
                     "guests": accounts.get(Role.GUEST.value, 0), "using_analysis": active_members},
    })


@router.get("/health")
def health_page(request: Request, session: Session = Depends(admin_session)):
    return templates.TemplateResponse(request, "admin/health.html", {"report": check_health(session, DEFAULT_CONFIG_PATH)})


@router.get("/health.json")
def health_json(session: Session = Depends(admin_session)):
    report = check_health(session, DEFAULT_CONFIG_PATH)
    return JSONResponse(report.as_dict(), status_code=503 if report.status == "down" else 200)


# ---- activity dashboard ----------------------------------------------------------

@router.get("/activity")
def activity_page(request: Request, days: int = 30, q: str = "", guests: bool = False, sort: str = "last_active",
                  session: Session = Depends(admin_session)):
    days = activity_data.clamp_days(days)
    data = activity_data.overview(session, days, q=q[:100], include_guests=guests, sort=sort)
    return templates.TemplateResponse(request, "admin/activity.html", {
        "data": data, "days": days, "ranges": activity_data.RANGES, "q": q, "guests": guests, "sort": sort,
        "labels": ActivityKind.LABELS, "platform": activity_data.quota.platform_limits(session),
    })


def _target(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


@router.get("/users/{user_id}")
def user_page(request: Request, user_id: int, days: int = 30, kind: str | None = None, msg: str | None = None,
              error: str | None = None, session: Session = Depends(admin_session),
              admin: User = Depends(require_admin)):
    user = _target(session, user_id)
    days = activity_data.clamp_days(days)
    return templates.TemplateResponse(request, "admin/user.html", {
        "d": activity_data.user_detail(session, user, days, kind=kind), "days": days,
        "ranges": activity_data.RANGES, "labels": ActivityKind.LABELS, "message": msg, "error": error,
        "is_self": user.id == admin.id, "roles": _role_choices(session, admin, user),
        "manage_problem": access.manageable(session, admin, user),
    })


def _role_choices(session: Session, admin: User, user: User) -> list[dict]:
    held = set(rbac.roles_of(user, session))
    return [{"role": r["role"], "held": r["role"].name in held,
             "assignable": access.assignable(session, admin, r["role"])}
            for r in access.roles_with_counts(session)]


def _limit_value(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    value = int(raw)
    if not 0 <= value <= 10_000:
        raise ValueError
    return value


@router.post("/users/{user_id}/limits")
def set_user_limits(request: Request, user_id: int, search_limit: str = Form(""), ai_limit: str = Form(""),
                    session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    user = _target(session, user_id)
    old = {"search": user.search_limit_per_hour, "ai": user.ai_limit_per_hour}
    try:
        user.search_limit_per_hour = _limit_value(search_limit)
        user.ai_limit_per_hour = _limit_value(ai_limit)
    except ValueError:
        return _redirect(f"/admin/users/{user_id}", error="Limits must be whole numbers from 0 to 10000, or blank.")
    new = {"search": user.search_limit_per_hour, "ai": user.ai_limit_per_hour}
    if old != new:
        access.audit(session, admin, "user.limits.update", "user", user.id, old, new, request)
    session.commit()
    return _redirect(f"/admin/users/{user_id}", msg="Limits saved.")


@router.post("/users/{user_id}/suspend")
def suspend_user(request: Request, user_id: int, session: Session = Depends(admin_session),
                 admin: User = Depends(require_admin)):
    user = _target(session, user_id)
    try:
        access.set_user_active(session, admin, user, False, request)
    except access.AccessError as exc:
        session.rollback()
        return _redirect(f"/admin/users/{user_id}", error=str(exc))
    return _redirect(f"/admin/users/{user_id}", msg="Account suspended and signed out everywhere.")


@router.post("/users/{user_id}/reactivate")
def reactivate_user(request: Request, user_id: int, session: Session = Depends(admin_session),
                    admin: User = Depends(require_admin)):
    user = _target(session, user_id)
    try:
        access.set_user_active(session, admin, user, True, request)
    except access.AccessError as exc:
        session.rollback()
        return _redirect(f"/admin/users/{user_id}", error=str(exc))
    user.failed_logins = 0
    user.locked_until = None
    session.commit()
    return _redirect(f"/admin/users/{user_id}", msg="Account reactivated.")


# ---- job sources (the job pool) ---------------------------------------------------

@router.get("/sources")
def sources_page(request: Request, msg: str | None = None, error: str | None = None, q: str = "",
                 session: Session = Depends(admin_session)):
    query = session.query(PoolCompany)
    if q.strip():
        query = query.filter(or_(PoolCompany.name.ilike(f"%{q.strip()}%"), PoolCompany.board.ilike(f"%{q.strip()}%")))
    companies = query.order_by(PoolCompany.enabled.desc(), PoolCompany.canada_count.desc(), PoolCompany.name).all()
    return templates.TemplateResponse(request, "admin/sources.html", {
        "companies": companies, "totals": pool.totals(session), "status": pool.sync_status(), "q": q,
        "providers": pool.PROVIDER_LABELS, "message": msg, "error": error,
        "refresh_hours": activity_data.quota.platform_limits(session).pool_refresh_hours,
    })


@router.post("/sources/add")
def add_source(request: Request, provider: str = Form(""), board: str = Form(""),
               session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    try:
        company = pool.add_company(session, provider, board)
    except ValueError as exc:
        session.rollback()
        return _redirect("/admin/sources", error=str(exc))
    access.audit(session, admin, "source.add", "job_source", company.id, None,
                 {"provider": company.provider, "board": company.board}, request)
    session.commit()
    return _redirect("/admin/sources", msg=f"Added {company.label}: {company.job_count} open jobs, "
                                            f"{company.canada_count} in Canada.")


def _company(session: Session, company_id: int) -> PoolCompany:
    company = session.get(PoolCompany, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="No such company")
    return company


@router.post("/sources/{company_id}/toggle")
def toggle_source(request: Request, company_id: int, session: Session = Depends(admin_session),
                  admin: User = Depends(require_admin)):
    company = _company(session, company_id)
    company.enabled = not company.enabled
    access.audit(session, admin, "source.enable" if company.enabled else "source.pause", "job_source", company.id,
                 {"enabled": not company.enabled}, {"enabled": company.enabled}, request)
    session.commit()
    return _redirect("/admin/sources", msg=f"{company.label} {'enabled' if company.enabled else 'paused'}.")


@router.post("/sources/{company_id}/refresh")
def refresh_source(company_id: int, session: Session = Depends(admin_session)):
    company = _company(session, company_id)
    if pool.sync_company(session, company):
        return _redirect("/admin/sources", msg=f"{company.label} refreshed: {company.job_count} open jobs, "
                                                f"{company.canada_count} in Canada.")
    return _redirect("/admin/sources", error=f"Couldn't refresh {company.label}: {company.last_error}")


@router.post("/sources/{company_id}/delete")
def delete_source(request: Request, company_id: int, session: Session = Depends(admin_session),
                  admin: User = Depends(require_admin)):
    company = _company(session, company_id)
    label = company.label
    access.audit(session, admin, "source.delete", "job_source", company.id,
                 {"provider": company.provider, "board": company.board}, None, request)
    session.query(PoolJob).filter(PoolJob.company_id == company.id).delete(synchronize_session=False)
    session.delete(company)
    session.commit()
    return _redirect("/admin/sources", msg=f"Removed {label} and its postings. Jobs members already track are kept.")


@router.post("/sources/refresh-all")
def refresh_all_sources(session: Session = Depends(admin_session)):
    if not pool.start_background_sync(SessionLocal):
        return _redirect("/admin/sources", msg="A refresh is already running.")
    return _redirect("/admin/sources", msg="Refreshing every company in the background -- reload in a minute.")
