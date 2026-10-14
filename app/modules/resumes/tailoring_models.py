"""Tailored resumes (`TailoredResume`) and where each of their bullets came
from in the original resume (`TailoredBulletProvenance`)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class TailoredResume(TenantOwned, Base):
    __tablename__ = "tailored_resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    docx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    skills_order: Mapped[list] = mapped_column(JSON, default=list)
    validation_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    bullet_provenance: Mapped[list["TailoredBulletProvenance"]] = relationship(
        back_populates="tailored_resume", cascade="all, delete-orphan"
    )


class TailoredBulletProvenance(TenantOwned, Base):
    """Traces every AI-generated bullet back to the base-resume bullet it was derived from.
    This is the auditable half of the truthfulness guardrail (the other half is the
    deterministic validator in app/modules/resumes/validator.py).
    """

    __tablename__ = "tailored_bullet_provenance"

    id: Mapped[int] = mapped_column(primary_key=True)
    tailored_resume_id: Mapped[int] = mapped_column(ForeignKey("tailored_resumes.id"))
    generated_text: Mapped[str] = mapped_column(Text)
    source_bullet_id: Mapped[int | None] = mapped_column(
        ForeignKey("accomplishment_bullets.id"), nullable=True
    )
    validation_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Passage tailoring of a Word resume (no source bullet): the original
    # paragraph text and the heading it sits under.
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The AI may suggest a paragraph is irrelevant to the job; it's only taken
    # out of the resume once the user approves (`removed`).
    suggest_remove: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    remove_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    tailored_resume: Mapped["TailoredResume"] = relationship(back_populates="bullet_provenance")
