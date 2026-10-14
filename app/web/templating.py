"""The one Jinja2 environment every page renders with, and the filters and
globals its templates use (dates, money, sources, resume diffs, permissions)."""
from __future__ import annotations

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
    if source.startswith(("greenhouse", "lever", "ashby")):
        return "Company careers page"
    return "Job board"


templates.env.filters["ago"] = _ago
templates.env.filters["source_label"] = _source_label
templates.env.filters["worddiff"] = inline_word_diff_html
templates.env.globals["sidediff"] = side_by_side_diff


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
    ("/admin", "Overview", "grid"), ("/admin/users", "Users", "users"), ("/admin/activity", "Activity", "activity"),
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


def safe_next(value: str | None, default: str = "/") -> str:
    """Only ever redirect within this app (no //evil.example or /\\evil)."""
    if not value or not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return default
    return value
