"""JSON shapes for tracked jobs in the API (/api/jobs, /api/jobs/{numeric id})."""
from __future__ import annotations

from pydantic import BaseModel


class JobSummary(BaseModel):
    id: int
    company: str
    title: str
    location: str | None
    status: str
    overall_score: int | None


class JobDetailResponse(BaseModel):
    id: int
    company: str
    title: str
    status: str
    overall_score: int | None
    explanation: str | None
    tailored_resume_validation_status: str | None
