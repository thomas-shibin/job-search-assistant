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

    def generate_match_explanation(self, score: MatchScoreBreakdown) -> str:
        """Optional AI polish of the deterministic score explanation. Off by default
        (a templated explanation is free). Implemented in Milestone 5.
        """
        ...
