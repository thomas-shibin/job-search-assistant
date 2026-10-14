"""Accounts and authentication. These tables are the tenants themselves, so
they are not `TenantOwned`; only `app/auth/` reads them, always by an explicit
user id, email or hashed token -- never "all users" on behalf of a request.

Secrets are never stored in the clear: passwords are Argon2id hashes, and
session / one-time tokens are stored as SHA-256 hashes (the raw token only
ever lives in the user's cookie or email link).
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base


class Role(str, enum.Enum):
    """The legacy account kind in `users.role`. What an account may do is
    decided by its RBAC roles (app/models/rbac.py), not by this."""

    GUEST = "guest"    # name + email only (no longer created)
    MEMBER = "member"  # a full account
    ADMIN = "admin"    # listed in ADMIN_EMAILS when created


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # One account per email -- guests aren't accounts, so they don't count.
        Index("uq_users_account_email", "email", unique=True,
              sqlite_where=text("role != 'guest'"), postgresql_where=text("role != 'guest'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)  # stored lower-cased
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)  # E.164, e.g. +16045551234
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # None: social sign-in only
    role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # "job_seeker" today; "employer" is reserved for hiring accounts (not open yet).
    account_type: Mapped[str] = mapped_column(String(16), default="job_seeker", server_default="job_seeker")
    # Per-user hourly limits set by an admin; None = the platform default.
    search_limit_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_limit_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    identities: Mapped[list["OAuthIdentity"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_guest(self) -> bool:
        return self.role == Role.GUEST.value

    @property
    def roles(self) -> tuple[str, ...]:
        """RBAC role names (e.g. JOB_SEEKER), read once per request (app/auth/rbac.py)."""
        from app.modules.access import rbac  # rbac imports this module's User

        return rbac.roles_of(self)

    @property
    def permissions(self) -> frozenset[str]:
        from app.modules.access import rbac  # rbac imports this module's User

        return rbac.permissions_of(self)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def is_admin(self) -> bool:
        """May open the admin area. (`role` is the legacy account kind; access
        itself comes from RBAC roles and permissions.)"""
        return self.can("admin.dashboard.view")

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_member(self) -> bool:
        """A full, verified account: may use every feature."""
        return self.is_active and not self.is_guest and self.email_verified


class OAuthIdentity(Base):
    """A Google/Facebook login linked to an account."""

    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))  # the provider's stable user id
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="identities")


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random token; only its hash is here."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class TokenPurpose(str, enum.Enum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"
    OAUTH_STATE = "oauth_state"
    TELEGRAM_LINK = "telegram_link"
    REGISTRATION = "registration"  # an email proven by code, waiting for name + password


class AuthToken(Base):
    """A single-use, expiring token (email verification, password reset, a
    social sign-in in progress, linking Telegram). Stored hashed."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EmailCode(Base):
    """A six-digit code emailed to prove an address (sign-up or sign-in by
    code). Only an HMAC of the code is stored; it expires after a minute,
    allows a few attempts, and issuing a new one retires the old (see
    app/auth/otp.py). Keyed by email, since a new user has no account yet."""

    __tablename__ = "email_codes"
    __table_args__ = (Index("ix_email_codes_email_created", "email", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    purpose: Mapped[str] = mapped_column(String(16))  # register | login
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
