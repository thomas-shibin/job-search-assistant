"""Job alerts already sent (`WatchlistAlert`), so a job is announced once per
watch. The watches themselves are settings: app/core/config/models.py."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class WatchlistAlert(TenantOwned, Base):
    """One Telegram watchlist alert decision for a job: the record that makes
    "was I told about this already?" checkable (and enforced). `alert_key` is
    the job's company + title, normalized, so a repost with different punctuation
    or location wording counts as the same role.
    """

    __tablename__ = "watchlist_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null once the job is deleted by the retention clean-up -- the alert stays,
    # so "already alerted for this role" keeps working.
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    alert_key: Mapped[str] = mapped_column(String(512), index=True)
    watches: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # sent | failed | summarized (in an overflow roll-up) | duplicate (skipped)
    status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
