"""Who is signed in, and what they may reach -- as FastAPI dependencies.

Every route that touches user data takes its database session from
`member_session` (or `viewer_session`), which binds the session to the signed-in
user before the route runs (app/core/tenancy.py). A route can't forget to filter by
user: the session itself only sees that user's rows.

Access levels:
- viewer: anyone signed in (the email-confirmation pages).
- member: an active account with a verified email, holding the permission
  the route needs (app/modules/access/rbac.py: ROUTE_PERMISSIONS) -- checked here for
  every member and admin route, on every request, from the database.
- admin: a member who may open the admin area (admin.dashboard.view); admin
  routes get a cross-user session for platform totals.
"""
from __future__ import annotations

import logging
from typing import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database.session import get_session
from app.core.tenancy import bind_tenant, is_system, make_strict
from app.modules.access import rbac
from app.modules.auth.models import User
from app.modules.auth.sessions import user_for_request

logger = logging.getLogger(__name__)


class AccessDenied(Exception):
    """Raised by the dependencies below; `main.py` turns each kind into a page,
    a redirect or a JSON error."""

    reason = "denied"

    def __init__(self, next_path: str = "/") -> None:
        super().__init__(self.reason)
        self.next_path = next_path


class LoginRequired(AccessDenied):
    reason = "login"


class MembersOnly(AccessDenied):
    reason = "members_only"


class VerifyEmailRequired(AccessDenied):
    reason = "verify_email"


class AdminOnly(AccessDenied):
    reason = "admin_only"


class PermissionDenied(AccessDenied):
    """Signed in, but without the permission the route needs."""

    reason = "forbidden"

    def __init__(self, next_path: str = "/", permission: str | None = None) -> None:
        super().__init__(next_path)
        self.permission = permission


class FeatureDisabled(AccessDenied):
    """The feature behind this route is switched off for everyone."""

    reason = "feature_disabled"

    def __init__(self, next_path: str = "/", feature: str | None = None) -> None:
        super().__init__(next_path)
        self.feature = feature


def _next(request: Request) -> str:
    path = request.url.path
    if request.method != "GET":
        return "/"
    return path + (f"?{request.url.query}" if request.url.query else "")


def current_user(request: Request, session: Session = Depends(get_session)) -> User | None:
    if not hasattr(request.state, "user"):
        request.state.user = user_for_request(session, request)
    return request.state.user


def require_viewer(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired(_next(request))
    return user


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def authorize(request: Request, session: Session, user: User) -> None:
    """Let the request through only if the user holds the permission its route
    needs and the route's feature is on. Routes missing from the table are
    refused (fail closed)."""
    template = route_template(request)
    rbac.permissions_of(user, session)  # resolve from this request's database, once
    request.state.db = session  # for the page's permission-aware menus (app/web/templating.py)
    if template.startswith(("/admin", "/api/admin")) and not user.can("admin.dashboard.view"):
        raise AdminOnly(_next(request))
    rule = rbac.rule_for(request.method, template)
    if rule is rbac._MISSING:
        logger.error("Route %s %s has no permission rule; refusing it", request.method, template)
        raise PermissionDenied(_next(request))
    if rule is not None and not user.can(rule):
        raise PermissionDenied(_next(request), rule)
    feature = rbac.feature_for(template)
    if feature and not rbac.feature_on(session, feature):
        raise FeatureDisabled(_next(request), feature)


def require_permission(user: User, permission: str, request: Request | None = None) -> None:
    """For a check inside a route (e.g. one route serving two kinds of resource)."""
    if not user.can(permission):
        raise PermissionDenied(_next(request) if request else "/", permission)


def require_member(request: Request, user: User = Depends(require_viewer),
                   session: Session = Depends(get_session)) -> User:
    if user.is_guest:
        raise MembersOnly(_next(request))
    if not user.email_verified:
        raise VerifyEmailRequired(_next(request))
    authorize(request, session, user)
    return user


def require_admin(request: Request, user: User = Depends(require_member)) -> User:
    if not user.is_admin:
        raise AdminOnly(_next(request))
    return user


def _bound(session: Session, user: User) -> Session:
    if is_system(session):  # never hand a cross-user session to a request
        raise RuntimeError("system session in a request")
    return make_strict(bind_tenant(session, user.id))


def viewer_session(user: User = Depends(require_viewer), session: Session = Depends(get_session)) -> Iterator[Session]:
    """The signed-in user's data only (a guest owns none, so sees none)."""
    yield _bound(session, user)


def member_session(user: User = Depends(require_member), session: Session = Depends(get_session)) -> Iterator[Session]:
    yield _bound(session, user)


def admin_session(user: User = Depends(require_admin), session: Session = Depends(get_session)) -> Iterator[Session]:
    """Platform-wide reads for admin pages, which show totals only -- never
    another user's content."""
    session.info["tenant_system"] = True
    session.info.pop("tenant_strict", None)
    yield session
