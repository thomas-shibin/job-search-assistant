"""Stored results of matching a job to the resume: the keyword score
(`MatchAnalysis`) and the AI fit assessment (`JobFitAssessment`)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class MatchAnalysis(TenantOwned, Base):
    """One row per scoring run. Rows are never overwritten in place so score history
    survives config/weight changes (see weights_snapshot).
    """

    __tablename__ = "match_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))

    overall_score: Mapped[int] = mapped_column(Integer)
    technical_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experience_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seniority_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    education_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    strong_matches: Mapped[list] = mapped_column(JSON, default=list)
    partial_matches: Mapped[list] = mapped_column(JSON, default=list)
    missing_requirements: Mapped[list] = mapped_column(JSON, default=list)
    critical_missing_requirements: Mapped[list] = mapped_column(JSON, default=list)

    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    weights_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    scored_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class JobFitAssessment(TenantOwned, Base):
    """An AI fit assessment of the active resume against one job ("Analyze with
    AI"). Cached per (job, candidate profile): the same resume is never
    sent for the same job twice unless the user explicitly re-runs it.
    """

    __tablename__ = "job_fit_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    candidate_profile_id: Mapped[int] = mapped_column(ForeignKey("candidate_profiles.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fit_score: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    matched: Mapped[list] = mapped_column(JSON, default=list)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
