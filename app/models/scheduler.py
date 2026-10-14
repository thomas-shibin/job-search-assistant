from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.tenancy import TenantOwned


class SchedulerRun(TenantOwned, Base):
    __tablename__ = "scheduler_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")

    source_results: Mapped[list["SourceRunResult"]] = relationship(
        back_populates="scheduler_run", cascade="all, delete-orphan"
    )


class SourceRunResult(TenantOwned, Base):
    """Per-source outcome for one scheduler run, so one failing source never hides the
    others' results (spec §21 — pipeline continues on per-source failure).
    """

    __tablename__ = "source_run_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduler_run_id: Mapped[int] = mapped_column(ForeignKey("scheduler_runs.id"))
    source_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    jobs_discovered: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    jobs_qualified: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    scheduler_run: Mapped["SchedulerRun"] = relationship(back_populates="source_results")
