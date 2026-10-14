"""Changing access: roles, their permissions, users' roles, account status and
feature switches. Both the admin pages and /api/admin call these, so the
rules below hold however a change arrives:

- Nobody can grant what they don't hold themselves (no privilege escalation).
- Protected permissions (the admin area and access control itself), and any
  role containing one, can only be granted or assigned by a Super Admin.
- SUPER_ADMIN always holds every permission: its permissions can't be edited,
  and only a Super Admin can give or take the role.
- Only a Super Admin can change an administrator's roles or status, nobody
  can change their own, and the last active Super Admin can't lose the role
  or be suspended.
- System roles can't be renamed or deleted; custom roles only when unused.

Every change is written to the audit log.
"""
from __future__ import annotations

import re

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.modules.access import rbac
from app.modules.access.models import AuditLog, FeatureFlag, Permission, RbacRole, RolePermission, UserRole
from app.modules.auth.models import AuthSession
from app.modules.auth.models import User
from app.modules.auth.sessions import client_ip

ROLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")


class AccessError(AppError):
    """A change that isn't allowed; the message is safe to show the admin."""
    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


def audit(session: Session, actor: User | None, action: str, resource_type: str, resource_id=None,
          old=None, new=None, request: Request | None = None) -> None:
    session.add(AuditLog(actor_user_id=actor.id if actor else None, action=action, resource_type=resource_type,
                         resource_id=None if resource_id is None else str(resource_id), old_value=old,
                         new_value=new, ip=client_ip(request) if request else None, created_at=rbac.utcnow()))


# ---- reading ---------------------------------------------------------------------------

def role_permissions(session: Session, role: RbacRole) -> set[str]:
    if role.name == rbac.SUPER_ADMIN:
        return set(session.execute(select(Permission.name)).scalars()) | set(rbac.ALL_PERMISSIONS)
    return set(session.execute(
        select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role.id)).scalars())


def roles_with_counts(session: Session) -> list[dict]:
    users = dict(session.execute(select(UserRole.role_id, func.count()).group_by(UserRole.role_id)).all())
    perms = dict(session.execute(select(RolePermission.role_id, func.count()).group_by(RolePermission.role_id)).all())
    total = session.query(Permission).count()
    order = {name: i for i, name in enumerate(rbac.SYSTEM_ROLES)}
    roles = sorted(session.query(RbacRole), key=lambda r: (order.get(r.name, len(order)), r.name))
    return [{"role": r, "users": users.get(r.id, 0),
             "permissions": total if r.name == rbac.SUPER_ADMIN else perms.get(r.id, 0)} for r in roles]


def permission_groups(session: Session) -> list[tuple[str, list[Permission]]]:
    """Permissions by resource, in catalog order (custom ones last)."""
    order = {f"{r}.{a}": i for i, (r, a, _) in enumerate(rbac.PERMISSIONS)}
    groups: dict[str, list[Permission]] = {}
    for perm in sorted(session.query(Permission), key=lambda p: (order.get(p.name, 10_000), p.name)):
        groups.setdefault(perm.resource, []).append(perm)
    return list(groups.items())


def get_role(session: Session, role_id: int) -> RbacRole:
    role = session.get(RbacRole, role_id)
    if role is None:
        raise AccessError("No such role.", 404)
    return role


# ---- what the actor may touch -----------------------------------------------------------

def _require(actor: User, permission: str) -> None:
    if not actor.can(permission):
        raise AccessError("You don't have permission to do this.")


def grantable(actor: User, permission: str) -> bool:
    """May `actor` add this permission to a role (or take it away)?"""
    if rbac.is_super(actor):
        return True
    return permission not in rbac.PROTECTED_PERMISSIONS and actor.can(permission)


def assignable(session: Session, actor: User, role: RbacRole) -> bool:
    """May `actor` give this role to someone (or take it away)?"""
    if rbac.is_super(actor):
        return True
    if role.name == rbac.SUPER_ADMIN:
        return False
    perms = role_permissions(session, role)
    return not (perms & rbac.PROTECTED_PERMISSIONS) and perms <= actor.permissions


def _is_administrator(session: Session, user: User) -> bool:
    perms = rbac.permissions_of(user, session)
    return bool(perms & rbac.PROTECTED_PERMISSIONS)


def manageable(session: Session, actor: User, target: User) -> str | None:
    """Why `actor` may not change `target`'s roles or status (None: they may)."""
    if target.id == actor.id:
        return "You can't change your own roles or status. Ask another administrator."
    if not rbac.is_super(actor) and (rbac.is_super(target) or _is_administrator(session, target)):
        return "Only a Super Admin can change an administrator's account."
    return None


# ---- roles ------------------------------------------------------------------------------

def _clean_name(name: str) -> str:
    name = re.sub(r"[\s-]+", "_", (name or "").strip()).upper()
    if not ROLE_NAME.match(name):
        raise AccessError("Role names use capital letters, digits and underscores (2-40 characters), "
                          "e.g. CONTENT_REVIEWER.", 400)
    return name


def create_role(session: Session, actor: User, name: str, description: str = "",
                request: Request | None = None) -> RbacRole:
    _require(actor, "roles.create")
    name = _clean_name(name)
    if session.query(RbacRole).filter(func.upper(RbacRole.name) == name).count():
        raise AccessError("A role with that name already exists.", 409)
    role = RbacRole(name=name, description=(description or "").strip()[:300], is_system_role=False)
    session.add(role)
    session.flush()
    audit(session, actor, "role.create", "role", role.id, None, {"name": name, "description": role.description},
          request)
    session.commit()
    return role


def update_role(session: Session, actor: User, role: RbacRole, name: str | None = None,
                description: str | None = None, request: Request | None = None) -> RbacRole:
    _require(actor, "roles.update")
    old = {"name": role.name, "description": role.description}
    if role.name == rbac.SUPER_ADMIN and not rbac.is_super(actor):
        raise AccessError("Only a Super Admin can edit the Super Admin role.")
    if name is not None and _clean_name(name) != role.name:
        if role.is_system_role:
            raise AccessError("Built-in roles can't be renamed.", 400)
        new_name = _clean_name(name)
        if session.query(RbacRole).filter(func.upper(RbacRole.name) == new_name, RbacRole.id != role.id).count():
            raise AccessError("A role with that name already exists.", 409)
        role.name = new_name
    if description is not None:
        role.description = description.strip()[:300]
    audit(session, actor, "role.update", "role", role.id, old, {"name": role.name, "description": role.description},
          request)
    session.commit()
    return role


def delete_role(session: Session, actor: User, role: RbacRole, request: Request | None = None) -> None:
    _require(actor, "roles.delete")
    if role.is_system_role:
        raise AccessError("Built-in roles can't be deleted.", 400)
    holders = session.query(UserRole).filter(UserRole.role_id == role.id).count()
    if holders:
        raise AccessError(f"{holders} user{'s' if holders != 1 else ''} still have this role. "
                          "Remove it from them first.", 409)
    old = {"name": role.name, "permissions": sorted(role_permissions(session, role))}
    session.query(RolePermission).filter(RolePermission.role_id == role.id).delete(synchronize_session=False)
    session.delete(role)
    audit(session, actor, "role.delete", "role", role.id, old, None, request)
    session.commit()


def set_role_permissions(session: Session, actor: User, role: RbacRole, wanted: set[str],
                         request: Request | None = None) -> tuple[set[str], set[str]]:
    """Make the role hold exactly `wanted`. Returns (added, removed)."""
    _require(actor, "permissions.manage")
    if role.name == rbac.SUPER_ADMIN:
        raise AccessError("The Super Admin role always has every permission; it can't be changed.", 400)
    known = dict(session.execute(select(Permission.name, Permission.id)).all())
    unknown = set(wanted) - set(known)
    if unknown:
        raise AccessError(f"Unknown permission: {', '.join(sorted(unknown))}.", 400)
    current = role_permissions(session, role)
    added, removed = set(wanted) - current, current - set(wanted)
    refused = sorted(p for p in added | removed if not grantable(actor, p))
    if refused:
        raise AccessError("You can't grant or remove permissions you aren't allowed to manage: "
                          + ", ".join(refused) + ".")
    for perm in added:
        session.add(RolePermission(role_id=role.id, permission_id=known[perm]))
    if removed:
        session.query(RolePermission).filter(
            RolePermission.role_id == role.id,
            RolePermission.permission_id.in_([known[p] for p in removed])).delete(synchronize_session=False)
    if added or removed:
        audit(session, actor, "role.permissions.update", "role", role.id,
              {"role": role.name, "removed": sorted(removed)}, {"role": role.name, "added": sorted(added)}, request)
        session.commit()
    return added, removed


# ---- users ------------------------------------------------------------------------------

def set_user_roles(session: Session, actor: User, target: User, wanted_names: set[str],
                   request: Request | None = None) -> tuple[set[str], set[str]]:
    """Make `target` hold exactly these roles. Returns (added, removed)."""
    _require(actor, "users.roles.assign")
    problem = manageable(session, actor, target)
    if problem:
        raise AccessError(problem)
    roles = {r.name: r for r in session.query(RbacRole)}
    unknown = set(wanted_names) - set(roles)
    if unknown:
        raise AccessError(f"Unknown role: {', '.join(sorted(unknown))}.", 400)
    current = set(rbac.roles_of(target, session))
    added, removed = set(wanted_names) - current, current - set(wanted_names)
    refused = sorted(name for name in added | removed if not assignable(session, actor, roles[name]))
    if refused:
        raise AccessError("You can't assign or remove these roles: " + ", ".join(refused) + ".")
    if rbac.SUPER_ADMIN in removed and rbac.is_last_super_admin(session, target.id):
        raise AccessError("This is the last active Super Admin; make someone else a Super Admin first.", 409)
    for name in added:
        session.add(UserRole(user_id=target.id, role_id=roles[name].id))
    if removed:
        session.query(UserRole).filter(UserRole.user_id == target.id,
                                       UserRole.role_id.in_([roles[n].id for n in removed])).delete(
            synchronize_session=False)
    if added or removed:
        audit(session, actor, "user.roles.update", "user", target.id, {"roles": sorted(current)},
              {"roles": sorted(set(wanted_names))}, request)
        session.commit()
    rbac.forget(target)
    return added, removed


def set_user_active(session: Session, actor: User, target: User, active: bool,
                    request: Request | None = None) -> None:
    _require(actor, "users.suspend")
    problem = manageable(session, actor, target)
    if problem:
        raise AccessError(problem)
    if not active and rbac.is_last_super_admin(session, target.id):
        raise AccessError("This is the last active Super Admin and can't be suspended.", 409)
    if target.is_active == active:
        return
    target.is_active = active
    if not active:
        session.query(AuthSession).filter(AuthSession.user_id == target.id).delete(synchronize_session=False)
    audit(session, actor, "user.reactivate" if active else "user.suspend", "user", target.id,
          {"active": not active}, {"active": active}, request)
    session.commit()


# ---- features ---------------------------------------------------------------------------

def set_feature(session: Session, actor: User, flag: FeatureFlag, enabled: bool,
                request: Request | None = None) -> FeatureFlag:
    _require(actor, "features.manage")
    if flag.enabled != enabled:
        flag.enabled = enabled
        audit(session, actor, "feature.enable" if enabled else "feature.disable", "feature", flag.feature_key,
              {"enabled": not enabled}, {"enabled": enabled}, request)
        session.commit()
        rbac.forget_features(session)
    return flag
