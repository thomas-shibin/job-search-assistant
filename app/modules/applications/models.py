"""Records of the application files generated for a job (`ApplicationArtifactSet`)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class ApplicationArtifactSet(TenantOwned, Base):
    """Points at the generated file bundle for one job under data/applications/... (see
    docs/ARCHITECTURE.md §File Organization). One row per job.
    """

    __tablename__ = "application_artifact_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    folder_path: Mapped[str] = mapped_column(String(1024))
    job_json_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    job_description_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    match_analysis_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tailored_resume_docx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tailored_resume_pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cover_letter_docx_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
