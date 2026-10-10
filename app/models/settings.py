from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AppSetting(Base):
    """One row per top-level `AppConfig` section (e.g. "matching", "scheduler"),
    holding that section's JSON. The database is the source of truth for every
    setting the dashboard can edit -- config.yaml is only a one-time import
    source (see app/config/store.py). Secrets never land here; they stay in .env.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class WatchlistEntry(Base):
    """A watchlist watch (`app.config.schema.Watch`) as its own row, so watches
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


class WatchlistAlert(Base):
    """One Telegram watchlist alert decision for a job: the record that makes
    "was I told about this already?" checkable (and enforced). `alert_key` is
    the job's company + title, normalized, so a repost with different punctuation
    or location wording counts as the same role.
    """

    __tablename__ = "watchlist_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    alert_key: Mapped[str] = mapped_column(String(512), index=True)
    watches: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # sent | failed | summarized (in an overflow roll-up) | duplicate (skipped)
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
