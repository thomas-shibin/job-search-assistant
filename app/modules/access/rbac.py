"""Role-based access control: what each signed-in user may do.

A user holds one or more roles; a role grants permissions named
``resource.action`` (e.g. ``jobs.view``, ``users.suspend``). Code checks
permissions, never role names -- the only role with special meaning is
SUPER_ADMIN, which always holds every permission and can't be edited.

Permissions are resolved from the database once per request (cached on the
user object for that request only), so a change an admin makes applies to
the user's very next request -- there is no long-lived token holding them.

Every page and API route a member or admin can reach is listed in
`ROUTE_PERMISSIONS` below with the permission it needs (or `None`: any
signed-in account). A route that isn't listed is refused, and
tests/integration/test_rbac.py fails if a guarded route is missing.

Feature switches (`FEATURES`) turn an optional feature off for everyone; who
may use a feature that is on is still decided by its permissions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.modules.access.models import FeatureFlag, Permission, RbacRole, RolePermission, UserRole
from app.modules.auth.models import User
from app.modules.auth.settings import AuthSettings

logger = logging.getLogger(__name__)

SUPER_ADMIN = "SUPER_ADMIN"
ADMIN = "ADMIN"
EMPLOYER = "EMPLOYER"
JOB_SEEKER = "JOB_SEEKER"

# (resource, action, description) -- the permission's name is "resource.action".
PERMISSIONS: list[tuple[str, str, str]] = [
    ("jobs", "view", "Search jobs and view job details"),
    ("jobs", "save", "Save jobs to come back to"),
    ("jobs", "ai.match", "Get a match score and gap analysis for a job"),
    ("jobs", "create", "Post a job (employers)"),
    ("jobs", "update.own", "Edit job postings of their own company"),
    ("jobs", "update.any", "Edit any job posting"),
    ("jobs", "delete.own", "Delete job postings of their own company"),
    ("jobs", "delete.any", "Delete any job posting"),
    ("applications", "create", "Track a job they are applying to"),
    ("applications", "view.own", "See their own applications"),
    ("applications", "update.own", "Update the status of their own applications"),
    ("applications", "view.company", "See applicants to their company's jobs"),
    ("applications", "view.any", "See every application on the platform"),
    ("companies", "view", "View company profiles"),
    ("companies", "create", "Create a company profile"),
    ("companies", "update.own", "Edit their own company's profile"),
    ("companies", "update.any", "Edit any company profile"),
    ("profiles", "view.own", "View their own profile and settings"),
    ("profiles", "update.own", "Edit their own profile and settings"),
    ("resumes", "upload", "Upload and edit their resume"),
    ("resumes", "view.own", "View and download their own resume"),
    ("resumes", "ai.generate", "Create tailored resumes (Quick Tailor, per-job tailoring)"),
    ("alerts", "manage", "Set up job alerts and Telegram notifications"),
    ("users", "view", "View user accounts and their activity"),
    ("users", "create", "Create user accounts"),
    ("users", "update", "Change a user's hourly limits"),
    ("users", "suspend", "Suspend and reactivate accounts"),
    ("users", "roles.assign", "Assign and remove users' roles"),
    ("roles", "view", "View roles and their permissions"),
    ("roles", "create", "Create custom roles"),
    ("roles", "update", "Edit roles"),
    ("roles", "delete", "Delete unused custom roles"),
    ("permissions", "view", "View the permission catalog"),
    ("permissions", "manage", "Change which permissions a role has"),
    ("analytics", "view.company", "View their company's recruiting analytics"),
    ("analytics", "view.platform", "View platform activity, usage and costs"),
    ("features", "view", "View feature switches"),
    ("features", "manage", "Turn optional features on or off"),
    ("settings", "view", "View platform settings and health"),
    ("settings", "manage", "Change platform settings and run scans"),
    ("sources", "view", "View the company job sources"),
    ("sources", "manage", "Add, refresh and remove company job sources"),
    ("audit_logs", "view", "View the audit log"),
    ("admin.dashboard", "view", "Open the admin dashboard"),
    ("employer.dashboard", "view", "Open the employer workspace"),
]
ALL_PERMISSIONS: tuple[str, ...] = tuple(f"{r}.{a}" for r, a, _ in PERMISSIONS)

# Only a Super Admin may grant these to a role, or assign a role holding any
# of them: they are the keys to the admin area and to access control itself.
PROTECTED_PERMISSIONS = frozenset({
    "admin.dashboard.view", "users.roles.assign", "roles.create", "roles.update", "roles.delete",
    "permissions.manage", "settings.manage",
})

_SEEKER = ["jobs.view", "jobs.save", "jobs.ai.match", "applications.create", "applications.view.own",
           "applications.update.own", "profiles.view.own", "profiles.update.own", "resumes.upload",
           "resumes.view.own", "resumes.ai.generate", "alerts.manage"]
_EMPLOYER = ["jobs.view", "jobs.create", "jobs.update.own", "jobs.delete.own", "applications.view.company",
             "companies.view", "companies.create", "companies.update.own", "analytics.view.company",
             "employer.dashboard.view", "profiles.view.own", "profiles.update.own"]
_ADMIN = ["admin.dashboard.view", "users.view", "users.update", "users.suspend", "users.roles.assign",
          "roles.view", "permissions.view", "analytics.view.platform", "features.view", "features.manage",
          "settings.view", "audit_logs.view", "sources.view", "sources.manage", "jobs.view", "jobs.update.any",
          "jobs.delete.any", "companies.view", "companies.update.any", "applications.view.any"]

# name -> (description, default permissions). SUPER_ADMIN's list is implicit: everything.
SYSTEM_ROLES: dict[str, tuple[str, list[str]]] = {
    SUPER_ADMIN: ("Full access to everything, including roles and permissions. Can't be restricted.", []),
    ADMIN: ("Runs the platform: users, job sources, features and analytics.", _ADMIN),
    EMPLOYER: ("Manages their company's profile, job postings and applicants.", _EMPLOYER),
    JOB_SEEKER: ("Searches and saves jobs, tracks applications, manages their profile and resume.", _SEEKER),
}

# key -> (name, description, permissions needed to use it)
FEATURES: dict[str, tuple[str, str, list[str]]] = {
    "job_search": ("Job search", "Searching jobs and viewing postings.", ["jobs.view"]),
    "saved_jobs": ("Saved jobs", "Saving jobs to come back to later.", ["jobs.save"]),
    "application_tracking": ("Application tracking", "Tracking jobs through applied, interview and offer.",
                             ["applications.view.own"]),
    "job_alerts": ("Job alerts", "Scheduled searches with alerts by Telegram.", ["alerts.manage"]),
    "resume_builder": ("Resume editor", "Editing the resume section by section in the browser.",
                       ["resumes.upload"]),
    "ai_resume_builder": ("Resume tailoring", "Quick Tailor and tailored resumes for each job.",
                          ["resumes.ai.generate"]),
    "ai_job_matching": ("Job matching", "Match scores and gap analysis against the user's resume.",
                        ["jobs.ai.match"]),
}

# Every guarded route -> the permission it needs (None: any signed-in account).
# Keys are "METHOD /path/template" exactly as declared on the route.
ROUTE_PERMISSIONS: dict[str | None, list[str]] = {
    None: [
        "GET /account", "POST /account/profile", "POST /account/email", "POST /account/password",
        "POST /account/sessions/revoke", "POST /account/delete",
    ],
    "jobs.view": ["GET /jobs", "GET /jobs/posting/{ref}", "GET /api/jobs/search", "GET /api/jobs/{job_id}",
                  "GET /api/terms/{kind}", "POST /search/defaults"],
    "jobs.save": ["POST /saved", "POST /saved/{job_id}/remove", "GET /saved", "POST /api/jobs/{ref}/save",
                  "DELETE /api/jobs/{ref}/save", "GET /api/saved-jobs"],
    "jobs.ai.match": ["POST /jobs/{job_id}/analyze", "POST /jobs/{job_id}/assess", "POST /browse/analyze"],
    "applications.create": ["POST /browse/track"],
    "applications.view.own": ["GET /applications", "GET /tracked-jobs", "GET /api/jobs", "GET /jobs/{job_id}"],
    "applications.update.own": [
        "POST /jobs/{job_id}/description", "POST /jobs/{job_id}/approve", "POST /jobs/{job_id}/skip",
        "POST /jobs/{job_id}/open-application", "POST /jobs/{job_id}/mark-applied",
        "POST /jobs/{job_id}/mark-interview", "POST /jobs/{job_id}/mark-offer",
        "POST /jobs/{job_id}/mark-rejected", "POST /jobs/{job_id}/archive",
    ],
    "resumes.ai.generate": ["GET /quick", "POST /quick", "POST /jobs/{job_id}/retailor",
                            "POST /jobs/{job_id}/removals", "GET /jobs/{job_id}/resume.docx",
                            "GET /jobs/{job_id}/resume.pdf"],
    "resumes.view.own": ["GET /resume", "GET /resume/file"],
    "resumes.upload": ["POST /resume", "POST /resume/upload", "GET /resume/edit", "POST /resume/edit",
                       "POST /api/profile/resume", "POST /profile/import-resume"],
    "profiles.view.own": ["GET /profile", "GET /api/profile", "GET /welcome", "GET /settings"],
    "profiles.update.own": [
        "POST /welcome", "POST /profile/personal", "POST /profile/professional", "POST /profile/preferences",
        "POST /profile/experience", "POST /profile/experience/{item_id}", "POST /profile/experience/{item_id}/delete",
        "POST /profile/education", "POST /profile/education/{item_id}", "POST /profile/education/{item_id}/delete",
        "PUT /api/profile", "POST /api/profile/experience", "POST /api/profile/education",
        "POST /settings/{section}",
    ],
    "alerts.manage": [
        "GET /watchlist", "POST /watchlist/watches", "POST /watchlist/watches/{entry_id}",
        "POST /watchlist/watches/{entry_id}/toggle", "POST /watchlist/watches/{entry_id}/delete",
        "POST /watchlist/telegram/connect", "POST /watchlist/telegram/disconnect", "POST /watchlist/telegram/test",
    ],
    # ---- admin area
    "admin.dashboard.view": ["GET /admin"],
    "analytics.view.platform": ["GET /admin/activity", "GET /admin/usage"],
    "users.view": ["GET /admin/users", "GET /admin/users/{user_id}", "GET /api/admin/users",
                   "GET /api/admin/users/{user_id}"],
    "users.update": ["POST /admin/users/{user_id}/limits"],
    "users.suspend": ["POST /admin/users/{user_id}/suspend", "POST /admin/users/{user_id}/reactivate"],
    "users.roles.assign": ["POST /admin/users/{user_id}/roles", "PUT /api/admin/users/{user_id}/roles"],
    "roles.view": ["GET /admin/roles", "GET /admin/roles/{role_id}", "GET /api/admin/roles",
                   "GET /api/admin/roles/{role_id}/permissions"],
    "roles.create": ["POST /admin/roles", "POST /api/admin/roles"],
    "roles.update": ["POST /admin/roles/{role_id}", "PUT /api/admin/roles/{role_id}"],
    "roles.delete": ["POST /admin/roles/{role_id}/delete", "DELETE /api/admin/roles/{role_id}"],
    "permissions.view": ["GET /admin/permissions", "GET /api/admin/permissions"],
    "permissions.manage": ["POST /admin/roles/{role_id}/permissions", "PUT /api/admin/roles/{role_id}/permissions"],
    "features.view": ["GET /admin/features", "GET /api/admin/features"],
    "features.manage": ["POST /admin/features/{feature_id}", "PUT /api/admin/features/{feature_id}"],
    "audit_logs.view": ["GET /admin/audit", "GET /api/admin/audit-logs"],
    "settings.view": ["GET /admin/settings", "GET /admin/health", "GET /admin/health.json"],
    "settings.manage": ["POST /admin/settings/{section}", "POST /admin/scan-now", "POST /admin/catalog-scan"],
    "sources.view": ["GET /admin/sources"],
    "sources.manage": ["POST /admin/sources/add", "POST /admin/sources/{company_id}/toggle",
                       "POST /admin/sources/{company_id}/refresh", "POST /admin/sources/{company_id}/delete",
                       "POST /admin/sources/refresh-all"],
}
_RULES: dict[str, str | None] = {route: perm for perm, routes in ROUTE_PERMISSIONS.items() for route in routes}

# feature -> the routes it switches off (path templates; any method).
FEATURE_ROUTES: dict[str, list[str]] = {
    "job_search": ["/jobs", "/jobs/posting/{ref}", "/api/jobs/search", "/search/defaults"],
    "saved_jobs": ["/saved", "/saved/{job_id}/remove", "/api/jobs/{ref}/save", "/api/saved-jobs"],
    "application_tracking": ["/applications", "/tracked-jobs", "/api/jobs", "/browse/track"],
    "job_alerts": ["/watchlist", "/watchlist/watches", "/watchlist/watches/{entry_id}",
                   "/watchlist/watches/{entry_id}/toggle", "/watchlist/watches/{entry_id}/delete",
                   "/watchlist/telegram/connect", "/watchlist/telegram/disconnect", "/watchlist/telegram/test"],
    "resume_builder": ["/resume/edit"],
    "ai_resume_builder": ["/quick", "/jobs/{job_id}/retailor", "/jobs/{job_id}/removals",
                          "/jobs/{job_id}/resume.docx", "/jobs/{job_id}/resume.pdf"],
    "ai_job_matching": ["/jobs/{job_id}/analyze", "/jobs/{job_id}/assess", "/browse/analyze"],
}
_FEATURE_FOR_PATH: dict[str, str] = {path: key for key, paths in FEATURE_ROUTES.items() for path in paths}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- resolving a user's access ---------------------------------------------------

_CACHE = "_rbac_cache"  # on the User instance: lives as long as the request's session


def _cached(user, key: str, compute):
    cache = user.__dict__.setdefault(_CACHE, {})
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def preset(user, roles: list[str], permissions: set[str] | None = None):
    """Fix a (detached) user's roles and permissions without the database --
    for tests and tools that build a User in memory."""
    perms = set(ALL_PERMISSIONS) if SUPER_ADMIN in roles else set(permissions or ())
    if permissions is None and SUPER_ADMIN not in roles:
        for role in roles:
            perms.update(SYSTEM_ROLES.get(role, ("", []))[1])
    user.__dict__[_CACHE] = {"roles": tuple(roles), "permissions": frozenset(perms)}
    return user


def forget(user) -> None:
    """Drop a user's cached access (after changing their roles in this request)."""
    user.__dict__.pop(_CACHE, None)


def _session_of(user) -> Session | None:
    from sqlalchemy.orm import object_session

    return object_session(user)


def roles_of(user, session: Session | None = None) -> tuple[str, ...]:
    def compute():
        db = session or _session_of(user)
        if db is None or user.id is None:
            return ()
        rows = db.execute(select(RbacRole.name).join(UserRole, UserRole.role_id == RbacRole.id)
                          .where(UserRole.user_id == user.id).order_by(RbacRole.name)).scalars()
        return tuple(rows)
    return _cached(user, "roles", compute)


def permissions_of(user, session: Session | None = None) -> frozenset[str]:
    """Everything the user may do: the union of their roles' permissions
    (all of them for a Super Admin). Empty for a suspended account."""
    def compute():
        if not getattr(user, "is_active", True):
            return frozenset()
        roles = roles_of(user, session)
        db = session or _session_of(user)
        if SUPER_ADMIN in roles:
            known = set(db.execute(select(Permission.name)).scalars()) if db is not None else set()
            return frozenset(known | set(ALL_PERMISSIONS))
        if db is None or not roles:
            return frozenset()
        rows = db.execute(
            select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id).where(UserRole.user_id == user.id)
        ).scalars()
        return frozenset(rows)
    return _cached(user, "permissions", compute)


def has(user, permission: str) -> bool:
    return user is not None and permission in permissions_of(user)


def has_any(user, permissions) -> bool:
    return user is not None and bool(permissions_of(user) & set(permissions))


def has_all(user, permissions) -> bool:
    return user is not None and set(permissions) <= permissions_of(user)


def is_super(user) -> bool:
    return user is not None and SUPER_ADMIN in roles_of(user)


# ---- feature switches --------------------------------------------------------------

@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    description: str
    enabled: bool
    required_permissions: tuple[str, ...]


def features(session: Session | None) -> dict[str, Feature]:
    """All feature switches, read once per session (one request). A feature
    with no row yet (or no database at hand) counts as on."""
    if session is None:
        return {key: Feature(key, name, description, True, tuple(perms))
                for key, (name, description, perms) in FEATURES.items()}
    if "rbac_features" not in session.info:
        rows = {f.feature_key: f for f in session.query(FeatureFlag)}
        result = {}
        for key, (name, description, perms) in FEATURES.items():
            row = rows.get(key)
            result[key] = Feature(key, row.name if row else name, row.description if row else description,
                                  row.enabled if row else True,
                                  tuple(row.required_permissions or perms) if row else tuple(perms))
        session.info["rbac_features"] = result
    return session.info["rbac_features"]


def feature_on(session: Session | None, key: str) -> bool:
    feature = features(session).get(key)
    return feature is None or feature.enabled


def can_use(session: Session | None, user, key: str) -> bool:
    """The feature is on and the user holds what it needs."""
    feature = features(session).get(key)
    if feature is None:
        return True
    return feature.enabled and has_all(user, feature.required_permissions)


def forget_features(session: Session) -> None:
    session.info.pop("rbac_features", None)


# ---- route rules -------------------------------------------------------------------

def route_key(method: str, path_template: str) -> str:
    return f"{method.upper()} {path_template}"


_MISSING = object()


def rule_for(method: str, path_template: str):
    """The permission a route needs, None for any signed-in account, or
    `_MISSING` when the route isn't in the table (refused)."""
    if method.upper() == "HEAD":
        method = "GET"
    return _RULES.get(route_key(method, path_template), _MISSING)


def feature_for(path_template: str) -> str | None:
    return _FEATURE_FOR_PATH.get(path_template)


def can_access_route(session: Session | None, user, path: str, method: str = "GET") -> bool:
    """Would `user` be let into this route? (`path` is a route template such
    as /jobs or /admin/roles.) Drives the navigation."""
    rule = rule_for(method, path)
    if rule is _MISSING or user is None:
        return False
    if path.startswith(("/admin", "/api/admin")) and not has(user, "admin.dashboard.view"):
        return False
    if rule is not None and not has(user, rule):
        return False
    feature = feature_for(path)
    return feature is None or feature_on(session, feature)


# ---- seeding -------------------------------------------------------------------------

def sync_catalog(connection: Connection) -> None:
    """Add any permission, system role or feature switch this code knows but the
    database doesn't yet. Repeatable: rows that exist are left as admins set
    them; a permission that is new gets granted to the system roles that have it
    by default (so a new feature reaches its users), never anything more."""
    from sqlalchemy import insert

    now = utcnow()
    roles = dict(connection.execute(select(RbacRole.name, RbacRole.id)).all())
    new_roles = set()
    for name, (description, _) in SYSTEM_ROLES.items():
        if name not in roles:
            roles[name] = connection.execute(insert(RbacRole).values(
                name=name, description=description, is_system_role=True, created_at=now, updated_at=now)
            ).inserted_primary_key[0]
            new_roles.add(name)
    perms = dict(connection.execute(select(Permission.name, Permission.id)).all())
    new_perms = set()
    for resource, action, description in PERMISSIONS:
        name = f"{resource}.{action}"
        if name not in perms:
            perms[name] = connection.execute(insert(Permission).values(
                name=name, description=description, resource=resource, action=action, created_at=now)
            ).inserted_primary_key[0]
            new_perms.add(name)
    granted = set(connection.execute(select(RolePermission.role_id, RolePermission.permission_id)).all())
    for role_name, (_, defaults) in SYSTEM_ROLES.items():
        for perm in defaults:
            if role_name not in new_roles and perm not in new_perms:
                continue  # an admin may have removed it on purpose
            pair = (roles[role_name], perms[perm])
            if pair not in granted:
                connection.execute(insert(RolePermission).values(role_id=pair[0], permission_id=pair[1]))
                granted.add(pair)
    flags = set(connection.execute(select(FeatureFlag.feature_key)).scalars())
    for key, (name, description, required) in FEATURES.items():
        if key not in flags:
            connection.execute(insert(FeatureFlag).values(
                feature_key=key, name=name, description=description, enabled=True,
                required_permissions=required, created_at=now, updated_at=now))


def role_id(session: Session, name: str) -> int | None:
    return session.execute(select(RbacRole.id).where(RbacRole.name == name)).scalar_one_or_none()


def grant_role(session: Session, user, name: str) -> bool:
    """Give `user` the role (no-op if they hold it). Trusted callers only --
    registration, the ADMIN_EMAILS bootstrap and the command line; admins go
    through app/admin/access.py, which checks who may assign what."""
    rid = role_id(session, name)
    if rid is None:
        sync_catalog(session.connection())
        rid = role_id(session, name)
    if session.get(UserRole, (user.id, rid)) is not None:
        return False
    session.add(UserRole(user_id=user.id, role_id=rid))
    session.flush()
    forget(user)
    return True


def super_admin_ids(session: Session, *, active_only: bool = True) -> list[int]:
    query = (select(UserRole.user_id).join(RbacRole, RbacRole.id == UserRole.role_id)
             .where(RbacRole.name == SUPER_ADMIN))
    if active_only:
        query = query.join(User, User.id == UserRole.user_id).where(User.is_active.is_(True))
    return list(session.execute(query).scalars())


def is_last_super_admin(session: Session, user_id: int) -> bool:
    return super_admin_ids(session) == [user_id]


def default_role_for(account_type: str | None) -> str:
    """The role public registration gives: never an administrative one."""
    return EMPLOYER if account_type == "employer" else JOB_SEEKER


def bootstrap_admin(session: Session, user) -> None:
    """Accounts listed in ADMIN_EMAILS (set by whoever runs the server) are
    Super Admins -- the trusted way to create the first one."""
    if user.email and user.email.lower() in AuthSettings().admins:
        if grant_role(session, user, SUPER_ADMIN):
            logger.info("Granted SUPER_ADMIN to user_id=%s from ADMIN_EMAILS", user.id)
