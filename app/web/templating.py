"""The one Jinja2 environment every page renders with, and the filters and
globals its templates use (dates, money, sources, resume diffs, permissions)."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import object_session

from app.modules.access import rbac
from app.modules.admin.charts import money
from app.modules.resumes.diff import inline_word_diff_html, side_by_side_diff

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _money(value) -> str:
    return money(float(value or 0))


templates.env.filters["money"] = _money


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


def _source_label(source: str | None) -> str:
    """Where a job came from, in the user's terms (never a provider's name)."""
    source = source or ""
    if source == "manual":
        return "Added by you"
    if source.startswith(("greenhouse", "lever", "ashby", "smartrecruiters", "workable")):
        return "Company careers page"
    return "Job board"


templates.env.filters["ago"] = _ago
templates.env.filters["source_label"] = _source_label
templates.env.filters["worddiff"] = inline_word_diff_html
templates.env.globals["sidediff"] = side_by_side_diff

STATIC_DIR = Path(__file__).resolve().parent / "static"
_static_versions: dict[str, tuple[float, str]] = {}


def static_url(name: str) -> str:
    """/static/<name>?v=<content hash>: a new deploy changes the URL, so browsers
    fetch the new file instead of running a cached old one."""
    path = STATIC_DIR / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return f"/static/{name}"
    cached = _static_versions.get(name)
    if cached is None or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
        _static_versions[name] = cached
    return f"/static/{name}?v={cached[1]}"


templates.env.globals["static_url"] = static_url


def public_url() -> str:
    """The address people reach the site at (PUBLIC_URL), for absolute links
    such as the social-share image."""
    from app.modules.auth.settings import AuthSettings

    return AuthSettings().base_url


templates.env.globals["public_url"] = public_url

THEMES = ("light", "dark", "system")


def default_theme() -> str:
    """The theme for visitors who haven't picked one (DEFAULT_THEME in .env):
    light, dark, or system (follow the device's setting)."""
    value = os.environ.get("DEFAULT_THEME", "").strip().lower()
    return value if value in THEMES else "system"


templates.env.globals["default_theme"] = default_theme


@pass_context
def seo(context) -> dict:
    """What search engines are told about this page (app/web/seo.py): whether
    to index it, its one canonical address, and site-ownership tags for
    Google Search Console / Bing Webmaster Tools (GOOGLE_SITE_VERIFICATION,
    BING_SITE_VERIFICATION in .env)."""
    from app.web import seo as rules

    request = context.get("request")
    if request is None:
        return {"index": False, "canonical": None, "verify": {}}
    index = rules.indexable(request)
    verify = {name: os.environ.get(key, "").strip() for name, key in (
        ("google-site-verification", "GOOGLE_SITE_VERIFICATION"), ("msvalidate.01", "BING_SITE_VERIFICATION"))}
    return {"index": index, "canonical": rules.site_url(request) + request.url.path if index else None,
            "verify": {k: v for k, v in verify.items() if v} if request.url.path == "/" else {}}


templates.env.globals["seo"] = seo


@pass_context
def structured_data(context, posting=None) -> list[dict]:
    """schema.org data for the page: a job from a company career site
    (JobPosting, for Google's job search), or the site itself on the landing page."""
    from app.web import seo as rules

    request = context.get("request")
    if posting is not None:
        found = rules.job_posting(posting, request)
        return [found] if found else []
    return rules.site_data(request) if request is not None else []


templates.env.globals["structured_data"] = structured_data


def _landing_faq() -> list[tuple[str, str]]:
    from app.web.seo import FAQ

    return FAQ


templates.env.globals["landing_faq"] = _landing_faq


@pass_context
def social_sign_in(context) -> dict[str, bool]:
    """Which "Continue with ..." buttons this visitor gets, for the sign-in
    pop-up on every page (the /login and /signup pages pass their own)."""
    from app.modules.auth.routes import social_providers

    request = context.get("request")
    return social_providers(request) if request is not None else {"google": False, "facebook": False}


templates.env.globals["social_sign_in"] = social_sign_in


def _provider_name(provider: str) -> str:
    from app.modules.auth.service import provider_name

    return provider_name(provider)


templates.env.filters["provider_name"] = _provider_name


# ---- permissions in templates (the server-side half of hasPermission & co.) ----------------

def _session(ctx, user):
    request = ctx.get("request")
    return object_session(user) or getattr(getattr(request, "state", None), "db", None)


def _user(ctx):
    request = ctx.get("request")
    user = getattr(getattr(request, "state", None), "user", None) if request is not None else None
    return None if user is None or user.is_guest else user


@pass_context
def can(ctx, *permissions: str) -> bool:
    """True when the signed-in user holds every one of `permissions`."""
    user = _user(ctx)
    return user is not None and set(permissions) <= user.permissions


@pass_context
def can_any(ctx, *permissions: str) -> bool:
    user = _user(ctx)
    return user is not None and bool(set(permissions) & user.permissions)


@pass_context
def can_route(ctx, path: str, method: str = "GET") -> bool:
    """Would the signed-in user get into this route (permission and feature)?"""
    user = _user(ctx)
    return user is not None and user.is_member and rbac.can_access_route(_session(ctx, user), user, path, method)


@pass_context
def feature(ctx, key: str) -> bool:
    """The feature is on and the signed-in user may use it."""
    user = _user(ctx)
    return user is not None and rbac.can_use(_session(ctx, user), user, key)


# The account menu and the admin area's navigation: shown item by item to whoever
# may open each route (app/modules/access/rbac.py), so a revoked permission hides its link.
ACCOUNT_MENU = [
    [("/profile", "My Profile", "user"), ("/saved", "Saved Jobs", "bookmark"),
     ("/applications", "Applications", "briefcase")],
    [("/resume", "My Resume", "file"), ("/quick", "Quick Tailor", "pen"), ("/watchlist", "Job Alerts", "bell")],
    [("/settings", "Settings", "sliders"), ("/account", "Account & security", "lock")],
]
ADMIN_MENU = [
    ("/admin", "Overview", "grid"), ("/admin/users", "Users", "users"), ("/admin/duplicates", "Duplicate accounts", "users"),
    ("/admin/activity", "Activity", "activity"), ("/admin/feedback", "Feedback & messages", "mail"),
    ("/admin/roles", "Roles & permissions", "shield"), ("/admin/features", "Features", "toggle"),
    ("/admin/sources", "Job sources", "globe"), ("/admin/audit", "Audit log", "list"),
    ("/admin/settings", "Platform settings", "key"), ("/admin/usage", "Usage & costs", "chart"),
    ("/admin/health", "Health", "activity"),
]


@pass_context
def menu(ctx, items):
    return [item for item in items if can_route(ctx, item[0])]


templates.env.globals.update(can=can, can_any=can_any, can_route=can_route, feature=feature, menu=menu,
                             ACCOUNT_MENU=ACCOUNT_MENU, ADMIN_MENU=ADMIN_MENU)

# The review card in base.html (app/modules/feedback).
from app.modules.feedback import service as _feedback  # noqa: E402

templates.env.globals.update(review_due=_feedback.review_due, REVIEW_QUESTIONS=_feedback.QUESTIONS,
                             REVIEW_MINUTES=_feedback.ACTIVE_MINUTES)


def safe_next(value: str | None, default: str = "/") -> str:
    """Only ever redirect within this app (no //evil.example or /\\evil)."""
    if not value or not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return default
    return value
