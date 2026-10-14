"""JSON API for the member's tracked jobs: ``GET /api/jobs`` (optionally by
status and minimum score). Runs in the member's own tenant-bound session."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.modules.applications.schemas import JobSummary
from app.modules.applications.service import latest_match_analysis, list_jobs
from app.modules.auth.deps import member_session

router = APIRouter(prefix="/api")


@router.get("/jobs", response_model=list[JobSummary])  # only the signed-in member's jobs
def api_list_jobs(status: str | None = None, min_score: int | None = None,
                  session: Session = Depends(member_session)):
    jobs = list_jobs(session, status=status, min_score=min_score)
    return [
        JobSummary(
            id=j.id,
            company=j.company,
            title=j.title,
            location=j.location,
            status=j.status.value,
            overall_score=(a.overall_score if (a := latest_match_analysis(session, j.id)) else None),
        )
        for j in jobs
    ]
