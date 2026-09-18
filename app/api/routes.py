"""Minimal JSON API over the same `app/services/` layer the dashboard uses
(docs/ARCHITECTURE.md: "a future React/Electron/mobile client would talk to
the same API with no backend rework"). Read-only for now -- actions
(approve/skip/...) are exposed through the HTML routes in `app/ui/routes.py`;
adding JSON POST equivalents is straightforward later but out of this
milestone's scope.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.services.jobs import get_job_detail, latest_match_analysis, list_jobs

router = APIRouter(prefix="/api")


class JobSummary(BaseModel):
    id: int
    company: str
    title: str
    location: str | None
    status: str
    overall_score: int | None


@router.get("/jobs", response_model=list[JobSummary])
def api_list_jobs(status: str | None = None, min_score: int | None = None, session: Session = Depends(get_session)):
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


class JobDetailResponse(BaseModel):
    id: int
    company: str
    title: str
    status: str
    overall_score: int | None
    explanation: str | None
    tailored_resume_validation_status: str | None


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def api_get_job(job_id: int, session: Session = Depends(get_session)):
    detail = get_job_detail(session, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    return JobDetailResponse(
        id=detail.job.id,
        company=detail.job.company,
        title=detail.job.title,
        status=detail.job.status.value,
        overall_score=detail.match_analysis.overall_score if detail.match_analysis else None,
        explanation=detail.match_analysis.explanation if detail.match_analysis else None,
        tailored_resume_validation_status=detail.tailored_resume.validation_status if detail.tailored_resume else None,
    )
