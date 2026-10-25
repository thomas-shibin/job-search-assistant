"""The free plan's lifetime allowances and the marks that tie accounts held by
the same person together (see free_plan.py).

These tables are not `TenantOwned`: they outlive the account. Deleting an
account keeps its row here, with `user_id` cleared, so deleting and signing up
again doesn't reset the allowance. Nothing readable is kept: a mark is a keyed
one-way hash of an email, a phone number, a resume's text or a name.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


class FreeAccount(Base):
    """One per account that has used a resume action or uploaded a resume."""

    __tablename__ = "free_plan_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)  # None once deleted
    ai_used: Mapped[int] = mapped_column(Integer, default=0)  # resume actions this account has made, ever
    searches_used: Mapped[int] = mapped_column(Integer, default=0)  # job-board searches it has made, ever
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PersonMark(Base):
    """A keyed hash of something that identifies a person, seen on an account."""

    __tablename__ = "free_plan_marks"
    __table_args__ = (
        UniqueConstraint("account_id", "kind", "digest", name="uq_free_plan_marks_account_kind_digest"),
        Index("ix_free_plan_marks_kind_digest", "kind", "digest"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("free_plan_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # email | phone | resume | name
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AccountLink(Base):
    """Two accounts that look like the same person, and why."""

    __tablename__ = "free_plan_links"
    __table_args__ = (UniqueConstraint("account_a", "account_b", name="uq_free_plan_links_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_a: Mapped[int] = mapped_column(ForeignKey("free_plan_accounts.id"), index=True)  # the lower id
    account_b: Mapped[int] = mapped_column(ForeignKey("free_plan_accounts.id"), index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)  # mark kinds both accounts share
    # shared: a strong match, the allowance is shared | review: a name only, for an admin to look at |
    # confirmed: an admin says same person (shared) | dismissed: an admin says different people
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the admin's user id
