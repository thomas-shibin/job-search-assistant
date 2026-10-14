"""The one Jinja2 environment every page renders with."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy.orm import object_session

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _money(value) -> str:
    from app.admin.charts import money

    return money(float(value or 0))


templates.env.filters["money"] = _money


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
    from app.auth import rbac

    user = _user(ctx)
    return user is not None and user.is_member and rbac.can_access_route(_session(ctx, user), user, path, method)


@pass_context
def feature(ctx, key: str) -> bool:
    """The feature is on and the signed-in user may use it."""
    from app.auth import rbac

    user = _user(ctx)
    return user is not None and rbac.can_use(_session(ctx, user), user, key)


# The account menu and the admin area's navigation: shown item by item to whoever
# may open each route (app/auth/rbac.py), so a revoked permission hides its link.
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
