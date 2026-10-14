"""Stored settings: the platform's sections (`AppSetting`), each user's own
(`UserSetting`) and their job-alert watches (`WatchlistEntry`, part of
`AppConfig.watchlist`), as validated by app/core/config/schema.py."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class AppSetting(Base):
    """Platform settings, shared by every user and edited only by admins: one
    row per platform section (`app.core.config.store.PLATFORM_SECTIONS`, e.g. "ai",
    "scheduler") holding that section's JSON. Each user's own sections live in
    `UserSetting`. Secrets never land here; they stay in .env.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class UserSetting(TenantOwned, Base):
    """One user's value for one of their settings sections (`app.core.config.store.
    USER_SECTIONS`, e.g. "search", "matching"), as JSON."""

    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict | list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class WatchlistEntry(TenantOwned, Base):
    """A watchlist watch (`app.core.config.schema.Watch`) as its own row, so watches
    can be added/edited/paused individually from the dashboard.
    """

    __tablename__ = "watchlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    companies: Mapped[list] = mapped_column(JSON, default=list)
    titles: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    locations: Mapped[list] = mapped_column(JSON, default=list)
    exclude_keywords: Mapped[list] = mapped_column(JSON, default=list)
    exclude_companies: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    min_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_salary: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_salary: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_types: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    posted_within_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
