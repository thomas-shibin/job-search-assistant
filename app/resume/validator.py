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

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?:[kKmM]\b)?%?\+?")
_SCALE = {"k": 1_000, "m": 1_000_000}


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


MAX_LENGTH_GROWTH = 1.25  # a rewritten bullet/summary may be at most 25% longer than the original...
LENGTH_SLACK = 30  # ...or 30 characters, whichever is more (a short line has room to spare)


def _max_length(original: str) -> float:
    return max(MAX_LENGTH_GROWTH * len(original), len(original) + LENGTH_SLACK)


def _numbers_in(text: str) -> set[str]:
    """The numbers a text states, by value: "12+", "12" and "1,000" vs "1000" are
    the same claim, so rephrasing "12+ years" as "over 12 years" isn't "inventing"
    a number. A percentage stays distinct from a plain number ("40%" vs "40").
    """
    found = set()
    for match in _NUMBER_RE.finditer(text):
        token = match.group().rstrip("+")
        percent = token.endswith("%")
        value = token.rstrip("%").replace(",", "")
        if value[-1:].lower() in _SCALE:  # "20K" is the same claim as "20,000"
            value = f"{float(value[:-1]) * _SCALE[value[-1].lower()]:.6f}"
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        found.add(value + ("%" if percent else ""))
    return found


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

    if len(draft_bullet.text) > _max_length(source.text):
        # The tailored resume is the candidate's own document with only the wording
        # swapped: a much longer bullet would wrap onto extra lines and shift the layout.
        return BulletValidationResult(
            text=source.text,
            source_bullet_id=source.id,
            accepted=False,
            rejection_reason=f"generated text is too long for the resume's layout "
                             f"({len(draft_bullet.text)} vs {len(source.text)} characters)",
        )

    return BulletValidationResult(text=draft_bullet.text, source_bullet_id=source.id, accepted=True)


def _validate_summary(summary: str, candidate: Any) -> bool:
    if candidate.summary and len(summary) > _max_length(candidate.summary):
        return False  # would push the rest of the resume down (see _validate_bullet)
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


# --- Passages (layout-independent tailoring of a Word resume) --------------

_NAME_WORD = re.compile(r"[A-Za-z][\w.+#/-]*")
_SENTENCE_START = re.compile(r"(^|[.!?:;]\s+|[(\"'“]\s*)$")


def _new_names(rewrite: str, resume_text: str) -> list[str]:
    """Capitalised words (tools, employers, titles) in `rewrite` that appear
    nowhere in the resume -- i.e. claims the candidate never made. Words that
    start a sentence or follow a label are capitalised by grammar, so skipped."""
    known = resume_text.casefold()
    found = []
    for match in _NAME_WORD.finditer(rewrite):
        word = match.group().rstrip(".")
        if not (word[:1].isupper() or any(c.isupper() for c in word[1:])):
            continue
        if _SENTENCE_START.search(rewrite[:match.start()]):
            continue
        parts = [part for part in re.split(r"[-/]", word) if part]  # "Kafka-based", "CI/CD"
        if any(part[:1].isupper() and part.casefold() not in known for part in parts) \
                and word.casefold() not in known and word not in found:
            found.append(word)
    return found


def validate_passage(original: str, rewrite: str | None, *, resume_text: str, job_title: str = "") -> tuple[str, str | None]:
    """(text to use, rejection reason or None). A rejected rewrite falls back to
    the original passage, so the resume never says anything new."""
    if not rewrite or not rewrite.strip():
        return original, "no rewrite returned"
    rewrite = rewrite.strip()
    invented = _numbers_in(rewrite) - _numbers_in(original)
    if invented:
        return original, "number(s) not in the original: " + ", ".join(sorted(invented))
    if len(rewrite) > _max_length(original):
        return original, f"too long for the resume's layout ({len(rewrite)} vs {len(original)} characters)"
    title = job_title.strip()
    if title and title.casefold() in rewrite.casefold() and title.casefold() not in resume_text.casefold():
        return original, f"claims the job's title ({title})"
    names = _new_names(rewrite, resume_text)
    if names:
        return original, "mentions what the resume doesn't: " + ", ".join(names[:5])
    return rewrite, None
