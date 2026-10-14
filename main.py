from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.modules.auth.deps import (
    AccessDenied,
    AdminOnly,
    FeatureDisabled,
    LoginRequired,
    MembersOnly,
    PermissionDenied,
    VerifyEmailRequired,
)

STATIC_DIR = Path(__file__).resolve().parent / "app" / "web" / "static"
logger = logging.getLogger(__name__)

# What a guest sees when they open a members-only feature (by URL prefix).
_FEATURES = {
    "/watchlist": ("Job alerts", "Get a message the moment a job matching your saved searches is posted.",
                   ["Save as many searches as you like", "Filter by salary, job type and company",
                    "Only jobs that match your resume well, if you want"]),
    "/resume": ("My resume", "Upload your resume once, edit it here, and use it for every application.",
                ["Word or PDF, read automatically", "Edit every section in the browser",
                 "Keeps your own design when tailoring"]),
    "/quick": ("Quick Tailor", "Paste any job description and get a resume tailored to it in seconds.",
               ["Your real experience, reworded for the role", "Nothing invented: every line traces to your resume",
                "Download as Word or PDF"]),
    "/settings": ("Settings", "Make the job search and matching work your way.",
                  ["Default searches and locations", "How jobs are scored", "Addresses for tailored resumes"]),
    "/account": ("Your profile", "Your account details, password and devices.", []),
    "/jobs": ("Job analysis", "See how well you match a job, what's missing, and get a tailored resume.",
              ["Match score and gaps", "A tailored resume per job", "Track every application"]),
    "/browse": ("Job analysis", "See how well you match a job, what's missing, and get a tailored resume.",
                ["Match score and gaps", "A tailored resume per job", "Track every application"]),
}
_DEFAULT_FEATURE = ("Members feature", "Create a free account to use this feature.", [])


def _feature(path: str) -> dict:
    title, pitch, points = next((v for k, v in _FEATURES.items() if path.startswith(k)), _DEFAULT_FEATURE)
    return {"title": title, "pitch": pitch, "points": points}


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _go(request: Request, url: str) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


_DENIED_MESSAGES = {
    "forbidden": "You don't have permission to do this.",
    "feature_disabled": "This feature is turned off at the moment.",
}


async def _access_denied(request: Request, exc: AccessDenied) -> Response:
    if _wants_json(request):
        if isinstance(exc, AdminOnly) and request.url.path.startswith("/api/admin"):
            exc = PermissionDenied(exc.next_path)  # the API answers 403, not a 404 page
        status = 401 if isinstance(exc, LoginRequired) else 403
        body = {"detail": exc.reason}
        if exc.reason in _DENIED_MESSAGES:
            body["error"] = {"code": exc.reason, "message": _DENIED_MESSAGES[exc.reason]}
        return JSONResponse(body, status_code=status)
    if isinstance(exc, (PermissionDenied, FeatureDisabled)):
        if request.headers.get("hx-request"):  # htmx won't swap a 403 page in; send the browser to it
            return _go(request, "/access-denied?" + urlencode({"reason": exc.reason}))
        from app.web.templating import templates

        return templates.TemplateResponse(request, "access_denied.html", {"reason": exc.reason},
                                          status_code=403)
    if isinstance(exc, LoginRequired):
        return _go(request, "/login?" + urlencode({"next": exc.next_path}))
    if isinstance(exc, VerifyEmailRequired):
        return _go(request, "/verify-email/pending")
    if isinstance(exc, MembersOnly):
        from app.web.templating import templates

        return templates.TemplateResponse(request, "members_only.html", {"feature": _feature(request.url.path)},
                                          status_code=403)
    if isinstance(exc, AdminOnly):
        return PlainTextResponse("Not found", status_code=404)  # don't advertise admin pages
    return PlainTextResponse("Forbidden", status_code=403)


_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self' https://accounts.google.com https://www.facebook.com"
    ),
}


def _same_origin(request: Request) -> bool:
    """CSRF defence for state-changing requests: the browser's Origin (or, if
    absent, Referer) must be this site. Cookies are also SameSite=Lax."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True  # non-browser clients (no ambient cookies to abuse)
    from app.modules.auth.settings import AuthSettings

    allowed = {request.headers.get("host", ""), urlparse(AuthSettings().public_url).netloc}
    return urlparse(source).netloc in allowed


def create_app() -> FastAPI:
    fastapi_app = FastAPI(title="Job Search Assistant", docs_url=None, redoc_url=None, openapi_url=None)
    fastapi_app.add_exception_handler(AccessDenied, _access_denied)

    @fastapi_app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in _UNSAFE and not _same_origin(request):
            logger.warning("Blocked cross-site %s %s", request.method, request.url.path)
            return PlainTextResponse("Cross-site request blocked", status_code=403)
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @fastapi_app.get("/health")
    def health() -> JSONResponse:
        # Public liveness only (Docker's healthcheck); the detailed report is
        # admin-only at /admin/health.
        from app.core.database.session import SessionLocal
        from app.modules.admin.health import check_database

        ok = check_database(SessionLocal)
        return JSONResponse({"status": "ok" if ok else "down"}, status_code=200 if ok else 503)

    from app.modules.access import routes as access
    from app.modules.admin import routes as admin
    from app.modules.alerts import routes as alerts
    from app.modules.applications import api as applications_api
    from app.modules.applications import routes as applications
    from app.modules.auth import api as auth_api
    from app.modules.auth import routes as auth
    from app.modules.jobs import api as jobs_api
    from app.modules.jobs import routes as jobs
    from app.modules.matching import routes as matching
    from app.modules.profiles import routes as profiles
    from app.modules.profiles import settings_routes as settings
    from app.modules.resumes import routes as resumes
    from app.web import public_routes as public

    @fastapi_app.on_event("startup")
    def seed_search_catalog() -> None:
        # Built-in titles/keywords/locations for the search pickers (idempotent).
        from app.modules.jobs.catalog.store import seed_catalog
        from app.core.database.session import SessionLocal

        try:
            with SessionLocal() as session:
                seed_catalog(session)
        except Exception:  # e.g. migrations not applied yet -- the app still serves
            logger.warning("Could not seed the search-term catalog", exc_info=True)

    @fastapi_app.on_event("startup")
    def sync_access_rules() -> None:
        # Permissions, roles and feature switches added in code since the last
        # migration (idempotent; never undoes an admin's changes).
        from app.modules.access.rbac import sync_catalog
        from app.core.database.session import engine

        try:
            with engine.begin() as connection:
                sync_catalog(connection)
        except Exception:
            logger.warning("Could not sync the access-control catalog", exc_info=True)

    fastapi_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # Each module contributes its own routers; which permission every route
    # needs is listed in app/modules/access/rbac.py (ROUTE_PERMISSIONS).
    for router in (
        auth_api.router, auth.router, public.router, profiles.router, access.router, admin.router, access.api,
        jobs_api.router, applications_api.router, jobs.router, applications.router, matching.router,
        resumes.router, settings.router, alerts.router,
    ):
        fastapi_app.include_router(router)

    return fastapi_app


app = create_app()
