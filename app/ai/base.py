from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.matching.scorer import MatchScoreBreakdown


class ExtractedJobRequirements(BaseModel):
    """Structured output of the one AI call made per unique job description. Cached by
    description_hash so an unchanged job is never re-analyzed (cost control, spec §19).
    """

    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    seniority_level: str | None = None
    min_years_experience: int | None = None
    remote_type_detected: str | None = None


class TailoredBulletDraft(BaseModel):
    text: str
    source_bullet_id: int


class TailoredResumeDraft(BaseModel):
    """Raw AI output before the truthfulness validator runs. Every bullet must cite the
    base-resume AccomplishmentBullet it was derived from.
    """

    summary: str
    skills_order: list[str]
    bullets: list[TailoredBulletDraft]


class FitAssessmentResult(BaseModel):
    """An AI read of how well the candidate's resume fits one job posting -- the
    dashboard's "Analyze with AI". Complements (doesn't replace) the
    deterministic keyword score: the model weighs experience and context that
    keyword matching can't.
    """

    fit_score: int = Field(ge=0, le=100)
    verdict: str  # "strong" | "good" | "partial" | "weak"
    summary: str
    matched: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    recommendation: str = ""


class AIProvider(Protocol):
    """Business logic depends only on this interface, never on a specific vendor SDK."""

    def extract_job_requirements(self, job_description: str) -> ExtractedJobRequirements:
        """Implemented in Milestone 5."""
        ...

    def tailor_resume(
        self,
        candidate: Any,
        job: Any,
        requirements: ExtractedJobRequirements,
        address: Any,
    ) -> TailoredResumeDraft:
        """Implemented in Milestone 6."""
        ...

    def rewrite_passages(self, passages: list[dict], job: Any, requirements: ExtractedJobRequirements | None,
                         candidate_skills: list[str]) -> dict[int, str | dict]:
        """Rewrite each resume passage ({"id", "section", "text"}) for `job`;
        returns {id: new text}, or {id: {"text", "drop_reason"}} where the AI
        suggests the passage is irrelevant to the job (the user decides).
        Layout-independent tailoring of a Word resume."""
        ...

    def assess_fit(self, candidate: dict, job: Any) -> FitAssessmentResult:
        """Judge the resume (`candidate`, see app/ai/fit.py) against `job`."""
        ...

    def generate_match_explanation(self, score: MatchScoreBreakdown) -> str:
        """Optional AI polish of the deterministic score explanation. Off by default
        (a templated explanation is free). Implemented in Milestone 5.
        """
        ...
