"""Role-based access control: roles, permissions, who holds which role, the
feature switches, and the audit log of administrative changes.

These are platform tables (not `TenantOwned`): they decide what a user may
do, so they are read for the signed-in user by `app/auth/rbac.py` and changed
only through `app/admin/access.py`, which enforces the escalation rules.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, event, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


class RbacRole(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)  # e.g. JOB_SEEKER
    description: Mapped[str] = mapped_column(String(300), default="")
    is_system_role: Mapped[bool] = mapped_column(Boolean, default=False)  # built in: can't be renamed or deleted
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)  # resource.action, e.g. jobs.update.own
    description: Mapped[str] = mapped_column(String(300), default="")
    resource: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True)


class FeatureFlag(Base):
    """An optional feature an admin can switch off for everyone. Who may use a
    feature that is on is still decided by its permissions."""

    __tablename__ = "feature_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(String(300), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    required_permissions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    """Who changed what in the access rules or the platform. Append-only."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
                                                      index=True)  # None: the system or the command line
    action: Mapped[str] = mapped_column(String(60), index=True)  # e.g. role.permissions.update
    resource_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)



@event.listens_for(Base.metadata, "after_create")
def _seed_access_rules(target, connection, tables=(), **kw) -> None:
    """A database built from the models (rather than migrations) gets the
    built-in roles, permissions and feature switches straight away."""
    if any(t.name == "roles" for t in tables):
        from app.modules.access.rbac import sync_catalog  # rbac imports these models

        sync_catalog(connection)
