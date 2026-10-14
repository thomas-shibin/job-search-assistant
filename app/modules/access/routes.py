"""Admin dashboard for access control -- overview, users and their roles,
roles with the permission matrix, the permission catalog, feature switches
and the audit log -- as pages (/admin/...) and as JSON (/api/admin/...).

Which permission each route needs is in app/modules/access/rbac.py (ROUTE_PERMISSIONS);
the rules on who may grant or assign what are in app/modules/access/service.py.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.modules.access import rbac
from app.modules.access import service as access
from app.modules.access.models import AuditLog, FeatureFlag, RbacRole, UserRole
from app.modules.auth.deps import admin_session, require_admin
from app.modules.auth.models import User
from app.web.templating import templates

router = APIRouter(prefix="/admin")
api = APIRouter(prefix="/api/admin")

PAGE_SIZE = 50


def _back(path: str, msg: str | None = None, error: str | None = None, anchor: str = "") -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("error", error)) if v}
    return RedirectResponse(url=path + (f"?{urlencode(params)}" if params else "") + (f"#{anchor}" if anchor else ""),
                            status_code=303)


def _user_or_404(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user")
    return user


def _role_or_404(session: Session, role_id: int) -> RbacRole:
    try:
        return access.get_role(session, role_id)
    except access.AccessError:
        raise HTTPException(status_code=404, detail="No such role")


def _roles_by_user(session: Session, user_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {uid: [] for uid in user_ids}
    if user_ids:
        rows = session.query(UserRole.user_id, RbacRole.name).join(RbacRole, RbacRole.id == UserRole.role_id).filter(
            UserRole.user_id.in_(user_ids)).order_by(RbacRole.name)
        for uid, name in rows:
            result[uid].append(name)
    return result


def _status(user: User) -> str:
    if not user.is_active:
        return "suspended"
    if not user.email_verified:
        return "unconfirmed"
    return "active"


def _users_query(session: Session, q: str = "", role: str = "", status: str = ""):
    query = session.query(User).filter(User.role != "guest")
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(User.email.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like),
                                 (User.first_name + " " + User.last_name).ilike(like)))
    if role:
        query = query.filter(User.id.in_(session.query(UserRole.user_id).join(RbacRole, RbacRole.id == UserRole.role_id)
                                         .filter(RbacRole.name == role)))
    if status == "active":
        query = query.filter(User.is_active.is_(True), User.email_verified_at.is_not(None))
    elif status == "suspended":
        query = query.filter(User.is_active.is_(False))
    elif status == "unconfirmed":
        query = query.filter(User.email_verified_at.is_(None))
    return query


def _audit_rows(session: Session, entries: list[AuditLog]) -> list[dict]:
    actor_ids = {e.actor_user_id for e in entries if e.actor_user_id}
    actors = {u.id: u for u in session.query(User).filter(User.id.in_(actor_ids))} if actor_ids else {}
    return [{"entry": e, "actor": actors.get(e.actor_user_id)} for e in entries]


# ---- overview -------------------------------------------------------------------------

@router.get("")
def overview(request: Request, session: Session = Depends(admin_session)):
    now = rbac.utcnow()
    users = session.query(User).filter(User.role != "guest")
    feats = rbac.features(session)
    recent = session.query(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(8).all()
    return templates.TemplateResponse(request, "admin/overview.html", {
        "totals": {
            "users": users.count(),
            "active": users.filter(User.is_active.is_(True), User.email_verified_at.is_not(None)).count(),
            "suspended": users.filter(User.is_active.is_(False)).count(),
            "new_week": users.filter(User.created_at >= now - timedelta(days=7)).count(),
            "features_on": sum(1 for f in feats.values() if f.enabled), "features": len(feats),
        },
        "roles": access.roles_with_counts(session),
        "recent": _audit_rows(session, recent),
    })


# ---- users ----------------------------------------------------------------------------

@router.get("/users")
def users_page(request: Request, q: str = "", role: str = "", status: str = "", page: int = 1,
               session: Session = Depends(admin_session)):
    page = max(1, page)
    query = _users_query(session, q[:100], role, status)
    total = query.count()
    users = query.order_by(User.created_at.desc(), User.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    roles = _roles_by_user(session, [u.id for u in users])
    return templates.TemplateResponse(request, "admin/users.html", {
        "rows": [{"user": u, "roles": roles[u.id], "status": _status(u)} for u in users],
        "total": total, "page": page, "pages": max(1, -(-total // PAGE_SIZE)),
        "q": q, "role": role, "status": status, "role_names": [r["role"].name for r in access.roles_with_counts(session)],
    })


@router.post("/users/{user_id}/roles")
async def set_user_roles(request: Request, user_id: int, session: Session = Depends(admin_session),
                         admin: User = Depends(require_admin)):
    target = _user_or_404(session, user_id)
    wanted = set((await request.form()).getlist("roles"))
    try:
        added, removed = access.set_user_roles(session, admin, target, wanted, request)
    except access.AccessError as exc:
        session.rollback()
        return _back(f"/admin/users/{user_id}", error=str(exc), anchor="roles")
    if not (added or removed):
        return _back(f"/admin/users/{user_id}", msg="No changes to save.", anchor="roles")
    return _back(f"/admin/users/{user_id}", msg="Roles saved. They apply from the user's next page.", anchor="roles")


# ---- roles and the permission matrix ----------------------------------------------------

@router.get("/roles")
def roles_page(request: Request, msg: str | None = None, error: str | None = None,
               session: Session = Depends(admin_session)):
    return templates.TemplateResponse(request, "admin/roles.html", {
        "roles": access.roles_with_counts(session), "message": msg, "error": error,
    })


@router.post("/roles")
def create_role(request: Request, name: str = Form(""), description: str = Form(""),
                session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    try:
        role = access.create_role(session, admin, name, description, request)
    except access.AccessError as exc:
        session.rollback()
        return _back("/admin/roles", error=str(exc))
    return _back(f"/admin/roles/{role.id}", msg=f"Role {role.name} created. Choose its permissions below.")


@router.get("/roles/{role_id}")
def role_page(request: Request, role_id: int, msg: str | None = None, error: str | None = None,
              session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    role = _role_or_404(session, role_id)
    held = access.role_permissions(session, role)
    editable = (role.name != rbac.SUPER_ADMIN and admin.can("permissions.manage"))
    groups = [(resource, [{"perm": p, "on": p.name in held,
                           "locked": not editable or not access.grantable(admin, p.name),
                           "protected": p.name in rbac.PROTECTED_PERMISSIONS} for p in perms])
              for resource, perms in access.permission_groups(session)]
    return templates.TemplateResponse(request, "admin/role.html", {
        "role": role, "groups": groups, "editable": editable, "held": len(held),
        "roles": access.roles_with_counts(session), "message": msg, "error": error,
        "holders": session.query(UserRole).filter(UserRole.role_id == role.id).count(),
        "can_rename": not role.is_system_role and admin.can("roles.update"),
        "can_delete": not role.is_system_role and admin.can("roles.delete"),
    })


@router.post("/roles/{role_id}")
def update_role(request: Request, role_id: int, name: str = Form(None), description: str = Form(""),
                session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    role = _role_or_404(session, role_id)
    try:
        access.update_role(session, admin, role, name, description, request)
    except access.AccessError as exc:
        session.rollback()
        return _back(f"/admin/roles/{role_id}", error=str(exc))
    return _back(f"/admin/roles/{role_id}", msg="Role details saved.")


@router.post("/roles/{role_id}/permissions")
async def save_role_permissions(request: Request, role_id: int, session: Session = Depends(admin_session),
                                admin: User = Depends(require_admin)):
    role = _role_or_404(session, role_id)
    wanted = set((await request.form()).getlist("permissions"))
    # Permissions the admin can't change are shown locked (and not submitted): keep them as they are.
    held = access.role_permissions(session, role)
    wanted |= {p for p in held if not access.grantable(admin, p)}
    try:
        added, removed = access.set_role_permissions(session, admin, role, wanted, request)
    except access.AccessError as exc:
        session.rollback()
        return _back(f"/admin/roles/{role_id}", error=str(exc))
    if not (added or removed):
        return _back(f"/admin/roles/{role_id}", msg="No changes to save.")
    return _back(f"/admin/roles/{role_id}", msg=f"Saved: {len(added)} added, {len(removed)} removed. "
                                                 "Users with this role get the change on their next page.")


@router.post("/roles/{role_id}/delete")
def delete_role(request: Request, role_id: int, session: Session = Depends(admin_session),
                admin: User = Depends(require_admin)):
    role = _role_or_404(session, role_id)
    name = role.name
    try:
        access.delete_role(session, admin, role, request)
    except access.AccessError as exc:
        session.rollback()
        return _back(f"/admin/roles/{role_id}", error=str(exc))
    return _back("/admin/roles", msg=f"Role {name} deleted.")


@router.get("/permissions")
def permissions_page(request: Request, session: Session = Depends(admin_session)):
    roles = [r["role"] for r in access.roles_with_counts(session)]
    holders = {role.name: access.role_permissions(session, role) for role in roles}
    return templates.TemplateResponse(request, "admin/permissions.html", {
        "groups": access.permission_groups(session), "roles": roles, "holders": holders,
        "protected": rbac.PROTECTED_PERMISSIONS,
    })


# ---- features -------------------------------------------------------------------------

def _feature_rows(session: Session) -> list[dict]:
    roles = [r["role"] for r in access.roles_with_counts(session)]
    holders = {role.name: access.role_permissions(session, role) for role in roles}
    rows = []
    order = list(rbac.FEATURES)
    for flag in sorted(session.query(FeatureFlag), key=lambda f: (order.index(f.feature_key)
                                                                   if f.feature_key in order else 99, f.feature_key)):
        required = list(flag.required_permissions or [])
        rows.append({"flag": flag, "required": required,
                     "roles": [name for name, perms in holders.items() if set(required) <= perms]})
    return rows


@router.get("/features")
def features_page(request: Request, msg: str | None = None, error: str | None = None,
                  session: Session = Depends(admin_session)):
    return templates.TemplateResponse(request, "admin/features.html", {
        "rows": _feature_rows(session), "message": msg, "error": error,
    })


@router.post("/features/{feature_id}")
def toggle_feature(request: Request, feature_id: int, enabled: str = Form(""),
                   session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    flag = session.get(FeatureFlag, feature_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="No such feature")
    on = enabled.lower() in {"1", "true", "on", "yes"}
    try:
        access.set_feature(session, admin, flag, on, request)
    except access.AccessError as exc:
        session.rollback()
        return _back("/admin/features", error=str(exc))
    return _back("/admin/features", msg=f"{flag.name} turned {'on' if on else 'off'}.")


# ---- audit log ------------------------------------------------------------------------

def _audit_query(session: Session, action: str = "", actor: str = ""):
    query = session.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action.like(f"{action}%"))
    if actor.strip():
        like = f"%{actor.strip()}%"
        query = query.filter(AuditLog.actor_user_id.in_(session.query(User.id).filter(User.email.ilike(like))))
    return query


@router.get("/audit")
def audit_page(request: Request, action: str = "", actor: str = "", page: int = 1,
               session: Session = Depends(admin_session)):
    page = max(1, page)
    query = _audit_query(session, action[:60], actor[:100])
    total = query.count()
    entries = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(
        (page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    actions = sorted({a.split(".")[0] for (a,) in session.query(AuditLog.action).distinct()})
    return templates.TemplateResponse(request, "admin/audit.html", {
        "rows": _audit_rows(session, entries), "total": total, "page": page,
        "pages": max(1, -(-total // PAGE_SIZE)), "action": action, "actor": actor, "actions": actions,
    })


# ---- JSON API -------------------------------------------------------------------------

def _error(exc: access.AccessError) -> JSONResponse:
    code = {400: "invalid", 404: "not_found", 409: "conflict"}.get(exc.status, "forbidden")
    return JSONResponse({"error": {"code": code, "message": str(exc)}}, status_code=exc.status)


def _role_json(session: Session, role: RbacRole, users: int | None = None) -> dict:
    return {"id": role.id, "name": role.name, "description": role.description,
            "is_system_role": role.is_system_role,
            "permissions": sorted(access.role_permissions(session, role)),
            **({"users": users} if users is not None else {})}


def _user_json(session: Session, user: User, roles: list[str] | None = None) -> dict:
    roles = roles if roles is not None else list(rbac.roles_of(user, session))
    return {"id": user.id, "email": user.email, "first_name": user.first_name, "last_name": user.last_name,
            "roles": roles, "status": _status(user),
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None}


class RoleIn(BaseModel):
    name: str | None = None
    description: str | None = None


class PermissionsIn(BaseModel):
    permissions: list[str]


class RolesIn(BaseModel):
    roles: list[str]


class FeatureIn(BaseModel):
    enabled: bool


@api.get("/roles")
def api_roles(session: Session = Depends(admin_session)):
    return [_role_json(session, r["role"], r["users"]) for r in access.roles_with_counts(session)]


@api.post("/roles", status_code=201)
def api_create_role(request: Request, body: RoleIn, session: Session = Depends(admin_session),
                    admin: User = Depends(require_admin)):
    try:
        role = access.create_role(session, admin, body.name or "", body.description or "", request)
    except access.AccessError as exc:
        session.rollback()
        return _error(exc)
    return JSONResponse(_role_json(session, role), status_code=201)


@api.put("/roles/{role_id}")
def api_update_role(request: Request, role_id: int, body: RoleIn, session: Session = Depends(admin_session),
                    admin: User = Depends(require_admin)):
    try:
        role = access.update_role(session, admin, access.get_role(session, role_id), body.name, body.description,
                                  request)
    except access.AccessError as exc:
        session.rollback()
        return _error(exc)
    return _role_json(session, role)


@api.delete("/roles/{role_id}")
def api_delete_role(request: Request, role_id: int, session: Session = Depends(admin_session),
                    admin: User = Depends(require_admin)):
    try:
        access.delete_role(session, admin, access.get_role(session, role_id), request)
    except access.AccessError as exc:
        session.rollback()
        return _error(exc)
    return {"deleted": True}


@api.get("/permissions")
def api_permissions(session: Session = Depends(admin_session)):
    return [{"id": p.id, "name": p.name, "description": p.description, "resource": p.resource, "action": p.action,
             "protected": p.name in rbac.PROTECTED_PERMISSIONS}
            for _, perms in access.permission_groups(session) for p in perms]


@api.get("/roles/{role_id}/permissions")
def api_role_permissions(role_id: int, session: Session = Depends(admin_session)):
    try:
        role = access.get_role(session, role_id)
    except access.AccessError as exc:
        return _error(exc)
    return {"role": role.name, "permissions": sorted(access.role_permissions(session, role))}


@api.put("/roles/{role_id}/permissions")
def api_set_role_permissions(request: Request, role_id: int, body: PermissionsIn,
                             session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    try:
        role = access.get_role(session, role_id)
        added, removed = access.set_role_permissions(session, admin, role, set(body.permissions), request)
    except access.AccessError as exc:
        session.rollback()
        return _error(exc)
    return {"role": role.name, "permissions": sorted(access.role_permissions(session, role)),
            "added": sorted(added), "removed": sorted(removed)}


@api.get("/users")
def api_users(q: str = "", role: str = "", status: str = "", page: int = 1,
              session: Session = Depends(admin_session)):
    page = max(1, page)
    query = _users_query(session, q[:100], role, status)
    total = query.count()
    users = query.order_by(User.created_at.desc(), User.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    roles = _roles_by_user(session, [u.id for u in users])
    return {"total": total, "page": page, "users": [_user_json(session, u, roles[u.id]) for u in users]}


@api.get("/users/{user_id}")
def api_user(user_id: int, session: Session = Depends(admin_session)):
    user = _user_or_404(session, user_id)
    return {**_user_json(session, user), "permissions": sorted(rbac.permissions_of(user, session))}


@api.put("/users/{user_id}/roles")
def api_set_user_roles(request: Request, user_id: int, body: RolesIn, session: Session = Depends(admin_session),
                       admin: User = Depends(require_admin)):
    target = _user_or_404(session, user_id)
    try:
        added, removed = access.set_user_roles(session, admin, target, set(body.roles), request)
    except access.AccessError as exc:
        session.rollback()
        return _error(exc)
    return {**_user_json(session, target), "added": sorted(added), "removed": sorted(removed)}


@api.get("/features")
def api_features(session: Session = Depends(admin_session)):
    return [{"id": r["flag"].id, "feature_key": r["flag"].feature_key, "name": r["flag"].name,
             "description": r["flag"].description, "enabled": r["flag"].enabled,
             "required_permissions": r["required"], "roles": r["roles"]} for r in _feature_rows(session)]


@api.put("/features/{feature_id}")
def api_set_feature(request: Request, feature_id: int, body: FeatureIn, session: Session = Depends(admin_session),
                    admin: User = Depends(require_admin)):
    flag = session.get(FeatureFlag, feature_id)
    if flag is None:
        return _error(access.AccessError("No such feature.", 404))
    try:
        access.set_feature(session, admin, flag, body.enabled, request)
    except access.AccessError as exc:
        session.rollback()
        return _error(exc)
    return {"id": flag.id, "feature_key": flag.feature_key, "enabled": flag.enabled}


@api.get("/audit-logs")
def api_audit_logs(action: str = "", actor: str = "", page: int = 1, session: Session = Depends(admin_session)):
    page = max(1, page)
    query = _audit_query(session, action[:60], actor[:100])
    total = query.count()
    entries = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(
        (page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return {"total": total, "page": page, "entries": [
        {"id": r["entry"].id, "actor": r["actor"].email if r["actor"] else None, "action": r["entry"].action,
         "resource_type": r["entry"].resource_type, "resource_id": r["entry"].resource_id,
         "old_value": r["entry"].old_value, "new_value": r["entry"].new_value,
         "created_at": r["entry"].created_at.isoformat()} for r in _audit_rows(session, entries)]}

