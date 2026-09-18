from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.ai.base import TailoredResumeDraft


class BulletValidationResult(BaseModel):
    text: str
    source_bullet_id: int | None
    accepted: bool
    rejection_reason: str | None = None


class ValidationResult(BaseModel):
    bullets: list[BulletValidationResult] = Field(default_factory=list)
    summary_accepted: bool
    skills_accepted: list[str] = Field(default_factory=list)
    skills_rejected: list[str] = Field(default_factory=list)


def validate_tailored_resume(draft: TailoredResumeDraft, candidate: Any) -> ValidationResult:
    """Deterministic truthfulness guardrail (spec §8): every generated bullet's cited
    source_bullet_id must exist on the candidate, and its claims (numbers, skill names)
    must be traceable to that source bullet's text. A bullet that fails falls back to the
    original source text verbatim rather than being silently included. Implemented in
    Milestone 6.
    """
    raise NotImplementedError("validate_tailored_resume will be implemented in Milestone 6")
