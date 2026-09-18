"""Deterministic truthfulness guardrail (spec §8): every generated bullet's
cited source_bullet_id must exist on the candidate, and any number it states
must already appear in that source bullet's own text -- fabricated numbers
are the sharpest, most checkable form of invented experience an AI rewrite
can introduce, so that's what this validator enforces mechanically rather
than trying to semantically judge "truthfulness" in general.

A bullet that cites an unknown source_bullet_id has nothing to fall back to,
so it's dropped entirely (empty `text`) rather than shown unverified. A
bullet that cites a real source but invents a number falls back to that
source bullet's own original text verbatim -- the achievement is still real,
only the AI's specific phrasing is rejected.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.ai.base import TailoredResumeDraft

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?\+?")


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


def _numbers_in(text: str) -> set[str]:
    return {m.group() for m in _NUMBER_RE.finditer(text)}


def _validate_bullet(draft_bullet: Any, source_bullets_by_id: dict[int, Any]) -> BulletValidationResult:
    source = source_bullets_by_id.get(draft_bullet.source_bullet_id)
    if source is None:
        return BulletValidationResult(
            text="",
            source_bullet_id=draft_bullet.source_bullet_id,
            accepted=False,
            rejection_reason="source_bullet_id does not match any bullet on the candidate profile",
        )

    invented_numbers = _numbers_in(draft_bullet.text) - _numbers_in(source.text)
    if invented_numbers:
        return BulletValidationResult(
            text=source.text,
            source_bullet_id=source.id,
            accepted=False,
            rejection_reason=(
                "generated text contains number(s) not present in the source bullet: "
                + ", ".join(sorted(invented_numbers))
            ),
        )

    return BulletValidationResult(text=draft_bullet.text, source_bullet_id=source.id, accepted=True)


def _validate_summary(summary: str, candidate: Any) -> bool:
    source_text = " ".join(
        [candidate.summary or ""] + [b.text for emp in candidate.employments for b in emp.bullets]
    )
    return _numbers_in(summary).issubset(_numbers_in(source_text))


def _validate_skills(skills_order: list[str], candidate: Any) -> tuple[list[str], list[str]]:
    """Every tailored skill must already be on the candidate profile -- tailoring
    may only reorder/select, never invent a new skill (spec §8, resume.
    never_invent_experience). Canonical casing from the profile is used for
    accepted skills so a case-insensitive AI match doesn't alter spelling.
    """
    by_lower = {s.name.lower(): s.name for s in candidate.skills}
    accepted: list[str] = []
    rejected: list[str] = []
    for skill in skills_order:
        canonical = by_lower.get(skill.lower())
        if canonical is not None:
            if canonical not in accepted:
                accepted.append(canonical)
        else:
            rejected.append(skill)
    return accepted, rejected


def validate_tailored_resume(draft: TailoredResumeDraft, candidate: Any) -> ValidationResult:
    """Deterministic truthfulness guardrail (spec §8): every generated bullet's cited
    source_bullet_id must exist on the candidate, and its claims (numbers, skill names)
    must be traceable to that source bullet's text. A bullet that fails falls back to the
    original source text verbatim rather than being silently included.
    """
    source_bullets_by_id = {b.id: b for emp in candidate.employments for b in emp.bullets}
    bullets = [_validate_bullet(bd, source_bullets_by_id) for bd in draft.bullets]
    summary_accepted = _validate_summary(draft.summary, candidate)
    skills_accepted, skills_rejected = _validate_skills(draft.skills_order, candidate)

    return ValidationResult(
        bullets=bullets,
        summary_accepted=summary_accepted,
        skills_accepted=skills_accepted,
        skills_rejected=skills_rejected,
    )
