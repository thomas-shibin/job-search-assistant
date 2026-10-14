"""The shared job pool: postings pulled from companies' public careers-site
feeds (app/services/pool.py). Shared by every user like the search catalog --
public job ads, nobody's data -- so these tables are not `TenantOwned`. A
posting only becomes a user's own `Job` when they analyze or track it."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PoolCompany(Base):
    """One company's careers feed, managed by admins (Platform -> Job sources)."""

    __tablename__ = "pool_companies"
    __table_args__ = (UniqueConstraint("provider", "board", name="uq_pool_company_provider_board"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))  # greenhouse | lever | ashby
    board: Mapped[str] = mapped_column(String(120))     # the company's slug on that provider
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ok | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    canada_count: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def label(self) -> str:
        return self.name or self.board


class PoolJob(Base):
    __tablename__ = "pool_jobs"
    __table_args__ = (
        UniqueConstraint("company_id", "external_id", name="uq_pool_job_company_external_id"),
        Index("ix_pool_jobs_country_posted", "country", "date_posted"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("pool_companies.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(120))
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)  # ISO code when known, lower-case
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    employment_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workplace: Mapped[str | None] = mapped_column(String(8), nullable=True)  # remote | hybrid | onsite
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1000))
    date_posted: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
