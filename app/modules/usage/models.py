"""The activity log (`ActivityEvent`): what each user did, for the admin
dashboard and the hourly limits."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


class ActivityKind:
    """What a user did. `SEARCH` and the AI kinds also count toward the hourly
    limits (app/modules/usage/quota.py)."""

    SIGN_IN = "sign_in"
    SEARCH = "search"              # a new job search -- counts toward the search limit
    SEARCH_MORE = "search_more"    # more pages / a repeat of a search already run this hour (free)
    AI_ANALYSIS = "ai_analysis"    # match analysis
    AI_TAILOR = "ai_tailor"        # tailored resume (analyze & tailor, re-tailor)
    QUICK_TAILOR = "quick_tailor"
    LIMIT_HIT = "limit_hit"        # blocked by an hourly limit
    RESUME_UPLOAD = "resume_upload"
    RESUME_EDIT = "resume_edit"
    JOB_TRACKED = "job_tracked"
    JOB_SAVED = "job_saved"
    JOB_STATUS = "job_status"

    AI_KINDS = (AI_ANALYSIS, AI_TAILOR, QUICK_TAILOR)

    LABELS = {
        SIGN_IN: "Signed in", SEARCH: "Searched", SEARCH_MORE: "Browsed results", AI_ANALYSIS: "Match analysis",
        AI_TAILOR: "Tailored resume", QUICK_TAILOR: "Quick tailor", LIMIT_HIT: "Hit a limit",
        RESUME_UPLOAD: "Uploaded resume", RESUME_EDIT: "Edited resume", JOB_TRACKED: "Tracked a job", JOB_SAVED: "Saved a job",
        JOB_STATUS: "Changed job status",
    }


class ActivityEvent(TenantOwned, Base):
    """One thing a user did, for the hourly limits and the admin activity
    dashboard. Holds what was done (search terms, job title) -- never resume
    content."""

    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_events_user_kind_time", "user_id", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # A search's fingerprint, so paging or re-running it within the hour is free.
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provider_calls: Mapped[int] = mapped_column(Integer, default=0)  # job-listing requests it cost
    ai_calls: Mapped[int] = mapped_column(Integer, default=0)        # paid analysis calls it made
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
