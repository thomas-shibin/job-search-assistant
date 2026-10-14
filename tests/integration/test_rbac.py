"""Role-based access control: default roles, permission checks on every
route, permission-driven navigation, admin management of roles / users /
features with its escalation rules, and the audit log."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.auth import rbac
from app.database.base import Base
from app.database.session import get_session
from app.models.rbac import AuditLog, FeatureFlag, Permission, RbacRole, RolePermission, UserRole
from app.models.user import AuthSession, User
from app.tenancy import system_session

pytestmark = pytest.mark.rbac_strict

SUPER, ADMIN, SEEKER, OTHER, BOTH = 1, 2, 3, 4, 5
_signed_in: dict = {}


def _current(request: Request):
    """Whoever `_signed_in` names, loaded fresh each request (as the real dependency does)."""
    if _signed_in.get("id") is None:
        return None
    with system_session(_signed_in["factory"]) as s:
        user = s.get(User, _signed_in["id"])
        if user is not None:
            s.expunge(user)
    if user is None or not user.is_active:
        return None
    request.state.user = user
    return user


def _add_user(s, uid, email, *roles, verified=True):
    s.add(User(id=uid, email=email, first_name=email.split("@")[0].title(), last_name="Test", role="member",
               email_verified_at=datetime(2026, 1, 1) if verified else None))
    s.flush()
    for name in roles:
        s.add(UserRole(user_id=uid, role_id=rbac.role_id(s, name)))


@pytest.fixture()
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)  # seeds roles, permissions and features
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        s.add(RbacRole(name="REVIEWER", description="Reads job sources", is_system_role=False))
        s.flush()
        _add_user(s, SUPER, "root@example.com", "SUPER_ADMIN")
        _add_user(s, ADMIN, "ada@example.com", "ADMIN")
        _add_user(s, SEEKER, "sam@example.com", "JOB_SEEKER")
        _add_user(s, OTHER, "otto@example.com", "JOB_SEEKER")
        _add_user(s, BOTH, "bo@example.com", "JOB_SEEKER", "EMPLOYER")
        s.commit()

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    _signed_in.clear()
    _signed_in.update(id=SEEKER, factory=factory)
    import main
    from app.auth.deps import current_user

    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = _current
    yield TestClient(main.app), factory
    main.app.dependency_overrides.clear()


def _as(uid):
    _signed_in["id"] = uid


def _roles(factory, uid) -> set[str]:
    with system_session(factory) as s:
        return set(rbac.roles_of(s.get(User, uid), s))


def _role(factory, name) -> int:
    with system_session(factory) as s:
        return rbac.role_id(s, name)


def _grants(factory, role_name) -> set[str]:
    with system_session(factory) as s:
        rid = rbac.role_id(s, role_name)
        return {n for (n,) in s.query(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
                .filter(RolePermission.role_id == rid)}


def _audit(factory) -> list[AuditLog]:
    with system_session(factory) as s:
        return s.query(AuditLog).order_by(AuditLog.id).all()


# ---- the catalog and the route table ------------------------------------------------------

def test_every_guarded_route_has_a_permission_rule():
    """Fail closed: a member/admin route missing from ROUTE_PERMISSIONS would be refused at
    runtime -- this catches it at test time instead."""
    import main

    def deps(dependant, found):
        for sub in dependant.dependencies:
            found.add(getattr(sub.call, "__name__", ""))
            deps(sub, found)
        return found

    def walk(routes):
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                yield from walk(route.original_router.routes)
            elif hasattr(route, "dependant"):
                yield route

    missing = []
    for route in walk(main.app.router.routes):
        if deps(route.dependant, set()) & {"require_member", "member_session", "admin_session", "require_admin"}:
            for method in route.methods - {"HEAD"}:
                if rbac.rule_for(method, route.path) is rbac._MISSING:
                    missing.append(f"{method} {route.path}")
    assert missing == []
    for perm in rbac.ROUTE_PERMISSIONS:
        assert perm is None or perm in rbac.ALL_PERMISSIONS


def test_seeding_is_repeatable_and_keeps_admin_changes(world):
    _, factory = world
    with system_session(factory) as s:
        s.query(RolePermission).filter(RolePermission.role_id == rbac.role_id(s, "JOB_SEEKER"),
                                       RolePermission.permission_id == s.query(Permission.id).filter(
                                           Permission.name == "jobs.save").scalar_subquery()).delete(
            synchronize_session=False)
        s.commit()
        rbac.sync_catalog(s.connection())
        rbac.sync_catalog(s.connection())
        s.commit()
        assert s.query(Permission).count() == len(rbac.ALL_PERMISSIONS)
        assert s.query(RbacRole).filter(RbacRole.is_system_role.is_(True)).count() == 4
        assert s.query(FeatureFlag).count() == len(rbac.FEATURES)
    assert "jobs.save" not in _grants(factory, "JOB_SEEKER")  # an admin's removal survives a re-sync


# ---- guests, registration and default roles -------------------------------------------------

def test_guests_get_no_job_search(world):
    http, _ = world
    _as(None)
    assert http.get("/jobs", follow_redirects=False).headers["location"].startswith("/login")
    assert http.get("/api/jobs/search?q=python").status_code == 401
    assert http.get("/api/admin/roles").status_code == 401
    assert http.get("/").status_code == 200  # the landing page stays public


def test_public_registration_gives_job_seeker_and_never_admin(world, monkeypatch):
    http, factory = world
    from app.auth import email as mailer
    from tests.integration.test_auth import STRONG, _code

    import main
    from app.auth.deps import current_user

    main.app.dependency_overrides.pop(current_user)  # real sign-in for this one
    monkeypatch.setattr(mailer, "outbox", [])
    http.post("/api/auth/register/start", json={"email": "new@example.com"})
    http.post("/api/auth/verify-email", json={"email": "new@example.com", "code": _code(mailer.outbox)})
    done = http.post("/api/auth/register/complete", json={
        "first_name": "New", "last_name": "Person", "password": STRONG, "confirm_password": STRONG,
        "roles": ["SUPER_ADMIN"], "role": "admin", "account_type": "employer"})  # ignored
    assert done.status_code == 200, done.text
    with system_session(factory) as s:
        user = s.query(User).filter(User.email == "new@example.com").one()
        assert set(rbac.roles_of(user, s)) == {"JOB_SEEKER"}
    me = http.get("/api/auth/me").json()
    assert me["roles"] == ["JOB_SEEKER"] and "jobs.view" in me["permissions"]
    assert "admin.dashboard.view" not in me["permissions"] and "saved_jobs" in me["features"]
    assert http.get("/admin").status_code == 404


def test_employer_accounts_get_the_employer_role():
    assert rbac.default_role_for("employer") == "EMPLOYER"
    assert rbac.default_role_for("job_seeker") == "JOB_SEEKER"
    assert rbac.default_role_for("admin") == "JOB_SEEKER"  # anything else: never an admin role


# ---- access by role --------------------------------------------------------------------------

ADMIN_PAGES = ["/admin", "/admin/users", "/admin/roles", "/admin/permissions", "/admin/features", "/admin/audit",
               "/admin/activity", "/admin/sources", "/admin/settings", "/admin/health"]


def test_super_admin_reaches_everything(world):
    http, _ = world
    _as(SUPER)
    for path in ADMIN_PAGES + [f"/admin/roles/{1}", f"/admin/users/{SEEKER}", "/profile", "/saved", "/watchlist"]:
        assert http.get(path).status_code == 200, path
    page = http.get("/admin").text
    assert "Roles &amp; permissions" in page and "Audit log" in page


def test_job_seeker_is_kept_out_of_admin_and_employer_apis(world):
    http, _ = world
    _as(SEEKER)
    assert http.get("/admin").status_code == 404  # admin pages aren't advertised
    assert http.get("/admin/users").status_code == 404
    response = http.get("/api/admin/roles")
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
    assert http.put(f"/api/admin/users/{SEEKER}/roles", json={"roles": ["SUPER_ADMIN"]}).status_code == 403
    assert _roles(world[1], SEEKER) == {"JOB_SEEKER"}
    assert "Admin dashboard" not in http.get("/profile").text


def test_admin_has_admin_pages_but_not_access_control(world):
    http, factory = world
    _as(ADMIN)
    for path in ("/admin", "/admin/users", "/admin/roles", "/admin/activity", "/admin/audit"):
        assert http.get(path).status_code == 200, path
    # ADMIN can view roles but not create them or change their permissions by default
    page = http.get(f"/admin/roles/{_role(factory, 'JOB_SEEKER')}").text
    assert "permissions.manage" in page and "Save changes" not in page
    assert http.post("/api/admin/roles", json={"name": "SNEAKY"}).status_code == 403
    assert http.put(f"/api/admin/roles/{_role(factory, 'JOB_SEEKER')}/permissions",
                    json={"permissions": []}).status_code == 403
    assert http.post("/admin/settings/platform", data={}).status_code == 403  # settings.manage is Super Admin's
    # Not a job seeker: the job-seeker pages are refused with an access-denied page
    denied = http.get("/saved")
    assert denied.status_code == 403 and "Access denied" in denied.text
    assert "Saved Jobs" not in http.get("/admin").text


# ---- assigning roles -------------------------------------------------------------------------

def test_admin_assigns_ordinary_roles_but_not_administrative_ones(world):
    http, factory = world
    _as(ADMIN)
    ok = http.put(f"/api/admin/users/{OTHER}/roles", json={"roles": ["JOB_SEEKER", "REVIEWER"]})
    assert ok.status_code == 200 and ok.json()["added"] == ["REVIEWER"]
    assert _roles(factory, OTHER) == {"JOB_SEEKER", "REVIEWER"}
    for wanted in (["JOB_SEEKER", "ADMIN"], ["JOB_SEEKER", "SUPER_ADMIN"]):
        refused = http.put(f"/api/admin/users/{OTHER}/roles", json={"roles": wanted})
        assert refused.status_code == 403, wanted
    # ...and can't touch another administrator, or themself
    assert http.put(f"/api/admin/users/{SUPER}/roles", json={"roles": []}).status_code == 403
    assert http.put(f"/api/admin/users/{ADMIN}/roles", json={"roles": ["ADMIN", "JOB_SEEKER"]}).status_code == 403
    assert "Only a Super Admin" in http.post(f"/admin/users/{SUPER}/suspend").text
    assert _roles(factory, SUPER) == {"SUPER_ADMIN"}
    assert [e.action for e in _audit(factory)] == ["user.roles.update"]


def test_super_admin_assigns_admin_and_the_new_admin_gets_in(world):
    http, factory = world
    _as(SUPER)
    response = http.post(f"/admin/users/{OTHER}/roles", data={"roles": ["JOB_SEEKER", "ADMIN"]},
                         follow_redirects=False)
    assert "Roles+saved" in response.headers["location"]
    _as(OTHER)
    assert http.get("/admin").status_code == 200
    assert http.get("/saved").status_code == 200  # still a job seeker too
    entry = _audit(factory)[-1]
    assert entry.action == "user.roles.update" and entry.actor_user_id == SUPER
    assert entry.old_value == {"roles": ["JOB_SEEKER"]} and entry.new_value == {"roles": ["ADMIN", "JOB_SEEKER"]}


def test_the_last_super_admin_is_protected(world):
    http, factory = world
    _as(SUPER)
    assert http.put(f"/api/admin/users/{SUPER}/roles", json={"roles": []}).status_code == 403  # not their own
    with system_session(factory) as s:  # a second Super Admin may not remove the only active one either...
        s.add(UserRole(user_id=ADMIN, role_id=rbac.role_id(s, "SUPER_ADMIN")))
        s.query(User).filter(User.id == ADMIN).update({User.is_active: False})
        s.commit()
    with system_session(factory) as s:
        from app.admin import access

        actor, target = s.get(User, ADMIN), s.get(User, SUPER)
        rbac.preset(actor, ["SUPER_ADMIN"])  # acting as the (suspended) second Super Admin
        with pytest.raises(access.AccessError, match="last active Super Admin"):
            access.set_user_roles(s, actor, target, {"JOB_SEEKER"})
        with pytest.raises(access.AccessError, match="last active Super Admin"):
            access.set_user_active(s, actor, target, False)
    # ...and can't delete their own account
    from app.auth.service import AuthError, delete_account

    with pytest.raises(AuthError, match="only Super Admin"):
        delete_account(factory, SUPER)
    with system_session(factory) as s:
        assert s.get(User, SUPER) is not None


def test_a_user_with_several_roles_gets_all_their_permissions(world):
    _, factory = world
    with system_session(factory) as s:
        both = s.get(User, BOTH)
        perms = rbac.permissions_of(both, s)
    assert {"jobs.save", "resumes.upload"} <= perms  # JOB_SEEKER
    assert {"jobs.create", "employer.dashboard.view"} <= perms  # EMPLOYER
    assert "admin.dashboard.view" not in perms


# ---- changing what a role may do -----------------------------------------------------------

def test_removing_a_permission_takes_effect_immediately_and_restoring_brings_it_back(world):
    http, factory = world
    _as(SEEKER)
    page = http.get("/profile").text
    assert 'href="/saved"' in page and http.get("/saved").status_code == 200

    _as(SUPER)
    seeker = _role(factory, "JOB_SEEKER")
    without = sorted(_grants(factory, "JOB_SEEKER") - {"jobs.save"})
    saved = http.put(f"/api/admin/roles/{seeker}/permissions", json={"permissions": without})
    assert saved.status_code == 200 and saved.json()["removed"] == ["jobs.save"]

    _as(SEEKER)  # the very next request, no sign-out needed
    assert http.get("/saved").status_code == 403
    assert http.get("/api/saved-jobs").status_code == 403
    page = http.get("/profile").text
    assert 'href="/saved"' not in page  # the menu item and the header icon are gone
    assert "jobs.save" not in http.get("/api/auth/me").json()["permissions"]

    _as(SUPER)
    http.post(f"/admin/roles/{seeker}/permissions", data={"permissions": without + ["jobs.save"]})
    _as(SEEKER)
    assert http.get("/saved").status_code == 200 and 'href="/saved"' in http.get("/profile").text
    actions = [e.action for e in _audit(factory)]
    assert actions == ["role.permissions.update", "role.permissions.update"]


def test_nobody_grants_what_they_may_not(world):
    http, factory = world
    with system_session(factory) as s:  # let ADMIN manage permissions
        rid = rbac.role_id(s, "ADMIN")
        pid = s.query(Permission.id).filter(Permission.name == "permissions.manage").scalar()
        s.add(RolePermission(role_id=rid, permission_id=pid))
        s.commit()
    _as(ADMIN)
    reviewer = _role(factory, "REVIEWER")
    assert http.put(f"/api/admin/roles/{reviewer}/permissions",
                    json={"permissions": ["sources.view"]}).status_code == 200  # an ADMIN holds sources.view
    for perm in ("resumes.upload", "roles.create", "admin.dashboard.view"):  # not held / protected
        response = http.put(f"/api/admin/roles/{reviewer}/permissions", json={"permissions": ["sources.view", perm]})
        assert response.status_code == 403, perm
    assert _grants(factory, "REVIEWER") == {"sources.view"}
    super_role = _role(factory, "SUPER_ADMIN")
    _as(SUPER)
    assert http.put(f"/api/admin/roles/{super_role}/permissions", json={"permissions": []}).status_code == 400


def test_custom_roles_are_created_edited_and_deleted_only_when_unused(world):
    http, factory = world
    _as(SUPER)
    created = http.post("/api/admin/roles", json={"name": "content reviewer", "description": "Checks sources"})
    assert created.status_code == 201 and created.json()["name"] == "CONTENT_REVIEWER"
    rid = created.json()["id"]
    assert http.post("/api/admin/roles", json={"name": "CONTENT_REVIEWER"}).status_code == 409
    assert http.put(f"/api/admin/roles/{rid}", json={"name": "SOURCE_REVIEWER"}).json()["name"] == "SOURCE_REVIEWER"
    http.put(f"/api/admin/users/{OTHER}/roles", json={"roles": ["JOB_SEEKER", "SOURCE_REVIEWER"]})
    assert http.delete(f"/api/admin/roles/{rid}").status_code == 409  # still assigned
    http.put(f"/api/admin/users/{OTHER}/roles", json={"roles": ["JOB_SEEKER"]})
    assert http.delete(f"/api/admin/roles/{rid}").status_code == 200
    assert http.delete(f"/api/admin/roles/{_role(factory, 'EMPLOYER')}").status_code == 400  # built-in
    assert http.put(f"/api/admin/roles/{_role(factory, 'ADMIN')}", json={"name": "BOSS"}).status_code == 400
    assert [e.action for e in _audit(factory)] == ["role.create", "role.update", "user.roles.update",
                                                   "user.roles.update", "role.delete"]


# ---- features ----------------------------------------------------------------------------------

def test_turning_a_feature_off_hides_and_blocks_it(world):
    http, factory = world
    _as(SEEKER)
    assert http.get("/quick").status_code == 200 and 'href="/quick"' in http.get("/profile").text
    with system_session(factory) as s:
        fid = s.query(FeatureFlag.id).filter(FeatureFlag.feature_key == "ai_resume_builder").scalar()
    _as(ADMIN)  # features.manage is an ADMIN permission
    assert http.put(f"/api/admin/features/{fid}", json={"enabled": False}).json()["enabled"] is False
    _as(SEEKER)
    blocked = http.get("/quick")
    assert blocked.status_code == 403 and "This feature is unavailable" in blocked.text
    assert 'href="/quick"' not in http.get("/profile").text
    assert "ai_resume_builder" not in http.get("/api/auth/me").json()["features"]
    _as(ADMIN)
    http.post(f"/admin/features/{fid}", data={"enabled": "1"})
    _as(SEEKER)
    assert http.get("/quick").status_code == 200
    assert [e.action for e in _audit(factory)] == ["feature.disable", "feature.enable"]


def test_a_feature_needs_its_permission_as_well(world):
    http, factory = world
    _as(ADMIN)  # the feature is on, but ADMIN lacks resumes.ai.generate
    assert http.get("/quick").status_code == 403


# ---- accounts ------------------------------------------------------------------------------------

def test_suspended_users_lose_access_at_once(world):
    http, factory = world
    with system_session(factory) as s:
        s.add(AuthSession(user_id=OTHER, token_hash="x" * 64, expires_at=datetime(2099, 1, 1)))
        s.commit()
    _as(ADMIN)
    assert "suspended" in http.post(f"/admin/users/{OTHER}/suspend", follow_redirects=True).text
    with system_session(factory) as s:
        other = s.get(User, OTHER)
        assert not other.is_active and rbac.permissions_of(other, s) == frozenset()
        assert s.query(AuthSession).filter(AuthSession.user_id == OTHER).count() == 0
    _as(OTHER)
    assert http.get("/saved", follow_redirects=False).status_code == 303  # treated as signed out
    assert _audit(factory)[-1].action == "user.suspend"


def test_users_page_lists_roles_and_filters(world):
    http, _ = world
    _as(ADMIN)
    page = http.get("/admin/users?role=EMPLOYER").text
    assert "bo@example.com" in page and "sam@example.com" not in page
    assert "EMPLOYER" in page and "JOB_SEEKER" in page
    data = http.get("/api/admin/users?q=sam").json()
    assert data["total"] == 1 and data["users"][0]["roles"] == ["JOB_SEEKER"]
    detail = http.get(f"/api/admin/users/{BOTH}").json()
    assert "jobs.create" in detail["permissions"]
    logs = http.get("/api/admin/audit-logs").json()
    assert logs["total"] == 0
