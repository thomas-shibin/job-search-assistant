from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class JobStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    QUALIFIED = "QUALIFIED"
    RESUME_GENERATED = "RESUME_GENERATED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    APPLICATION_OPENED = "APPLICATION_OPENED"
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"
    ERROR = "ERROR"


class Job(TenantOwned, Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("user_id", "source", "external_id", name="uq_job_user_source_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(String(2048))
    company: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # e.g. "full_time permanent" / "contract" (from the source, when it says)
    employment_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    date_posted: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_discovered: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    search_profile_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus), default=JobStatus.DISCOVERED)

    # Duplicate detection: fingerprint = company+title+location composite; description_hash
    # gates re-analysis so an unchanged job is never re-scored or re-billed to the AI provider.
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    description_hash: Mapped[str] = mapped_column(String(64), index=True)

    status_history: Mapped[list["JobStatusHistory"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    requirements: Mapped["JobRequirements"] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )


class JobStatusHistory(TenantOwned, Base):
    __tablename__ = "job_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    from_status: Mapped[JobStatus | None] = mapped_column(SAEnum(JobStatus), nullable=True)
    to_status: Mapped[JobStatus] = mapped_column(SAEnum(JobStatus))
    changed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)

    job: Mapped["Job"] = relationship(back_populates="status_history")


class JobRequirements(TenantOwned, Base):
    __tablename__ = "job_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    preferred_skills: Mapped[list] = mapped_column(JSON, default=list)
    seniority_level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    min_years_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_type_detected: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    job: Mapped["Job"] = relationship(back_populates="requirements")


class JobTombstone(TenantOwned, Base):
    """What's left of a job the retention clean-up deleted: just enough to
    recognise the same posting if a later scan finds it again, so it isn't
    re-saved, re-scored (AI cost) and re-alerted as if it were new.
    """

    __tablename__ = "job_tombstones"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    description_hash: Mapped[str] = mapped_column(String(64), index=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
