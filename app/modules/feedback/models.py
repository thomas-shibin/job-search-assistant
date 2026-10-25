"""What members tell us: star-rating reviews (the popup that asks once) and
messages sent from the Contact us page. Both belong to the member, so
deleting the account deletes them too."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


class Review(TenantOwned, Base):
    """One review: 1-5 stars per question (any may be skipped) and a comment."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    jobs_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)        # enjoying the jobs
    enough_jobs_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # finding enough jobs
    resume_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)       # the resume builder
    features_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)     # everything else
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    page: Mapped[str | None] = mapped_column(String(200), nullable=True)  # where it was sent from
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class ContactMessage(TenantOwned, Base):
    """A message from the Contact us page, emailed to us and kept here. The
    name and email are as they were on the account when it was sent."""

    __tablename__ = "contact_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(320))
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)  # handed to the mail server
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # an admin marked it done
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
