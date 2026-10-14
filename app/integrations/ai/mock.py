"""Free, local, deterministic stand-in for a real AI provider (spec §19: lets
the whole pipeline -- including Milestone 5's requirement-extraction step --
be exercised in tests and by users without an OpenAI key/API cost).

This is a heuristic, not a language model: it can only recognize a
"Requirements:"/"Preferred:"-style bulleted posting, not extract skills from
arbitrary prose. That's an intentional, honest limitation (see
`app/matching/scorer.py`'s module docstring on the same theme) -- postings
that don't use this structure yield empty extracted-skill lists here, same as
if Milestone 5's AI step had never run, rather than guessing.
"""
from __future__ import annotations

import re

from app.integrations.ai.base import ExtractedJobRequirements, FitAssessmentResult, TailoredBulletDraft, TailoredResumeDraft
from app.integrations.ai.usage import AIUsage
from app.modules.matching.scorer import MatchScoreBreakdown

_REQUIRED_HEADERS = ("requirements", "must have", "must-have", "qualifications", "what you'll need")
_PREFERRED_HEADERS = ("preferred", "nice to have", "nice-to-have", "bonus", "plus")
_OTHER_HEADERS = (
    "responsibilities", "about", "benefits", "what you'll do", "who you are",
    "summary", "overview", "why join", "compensation", "perks",
)

_BULLET_RE = re.compile(r"^[\s\-*•●◦‣·]+")
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", re.IGNORECASE)
_DEGREE_RE = re.compile(r"\b(bachelor'?s?|master'?s?|b\.?s\.?|m\.?s\.?|ph\.?d\.?|degree)\b", re.IGNORECASE)
_SENIORITY_KEYWORDS: list[tuple[str, str]] = [
    ("intern", "intern"),
    ("junior", "junior"),
    ("associate", "junior"),
    ("mid-level", "mid"),
    ("mid level", "mid"),
    ("intermediate", "mid"),
    ("senior", "senior"),
    ("staff", "staff"),
    ("lead", "lead"),
    ("principal", "principal"),
    ("director", "director"),
]
_REMOTE_KEYWORDS: list[tuple[str, str]] = [
    ("fully remote", "remote"),
    ("remote", "remote"),
    ("hybrid", "hybrid"),
    ("on-site", "onsite"),
    ("onsite", "onsite"),
    ("in office", "onsite"),
    ("in-office", "onsite"),
]


def _classify_header(line: str) -> str | None:
    lowered = line.strip().lower().rstrip(":")
    if len(lowered) > 60:
        return None
    if any(lowered == h or lowered.startswith(h) for h in _REQUIRED_HEADERS):
        return "required"
    if any(lowered == h or lowered.startswith(h) for h in _PREFERRED_HEADERS):
        return "preferred"
    if any(lowered == h or lowered.startswith(h) for h in _OTHER_HEADERS):
        return "other"
    return None


def _split_skill_tokens(line: str) -> list[str]:
    stripped = _BULLET_RE.sub("", line).strip()
    if not stripped:
        return []
    parts = re.split(r"[,;]", stripped)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def _extract_skill_buckets(text: str) -> tuple[list[str], list[str]]:
    required: list[str] = []
    preferred: list[str] = []
    current: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            # A blank line closes out whatever bulleted section is open --
            # real postings almost always separate sections this way, and it
            # keeps a trailing sentence after a bullet list (e.g. "This is a
            # fully remote position.") from being misread as one more skill.
            current = None
            continue
        header = _classify_header(line)
        if header is not None:
            current = header
            continue
        if current is None:
            continue
        # A years-of-experience line ("5+ years of backend engineering
        # experience") or a degree line describes tenure/education, not a
        # skill name -- `_detect_min_years`/callers read those separately, so
        # skip it here rather than adding it as a bogus "skill".
        if _YEARS_RE.search(line) or _DEGREE_RE.search(line):
            continue
        if current == "required":
            required.extend(_split_skill_tokens(line))
        elif current == "preferred":
            preferred.extend(_split_skill_tokens(line))
        # Lines under "other" headers (or before any recognized header) are
        # ignored -- not a source of skill claims this heuristic trusts.

    return required, preferred


def _detect_min_years(text_lower: str) -> int | None:
    match = _YEARS_RE.search(text_lower)
    return int(match.group(1)) if match else None


def _detect_seniority(text_lower: str) -> str | None:
    for keyword, label in sorted(_SENIORITY_KEYWORDS, key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(keyword)}\b", text_lower):
            return label
    return None


def _detect_remote_type(text_lower: str) -> str | None:
    for keyword, label in sorted(_REMOTE_KEYWORDS, key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(keyword)}\b", text_lower):
            return label
    return None


class MockAIProvider:
    """Structurally satisfies `app.integrations.ai.base.AIProvider` without any network
    call. Also usable as `ai.provider: mock` in config.yaml for a zero-cost
    (if lower-quality) alternative to a real API key.
    """

    def __init__(self) -> None:
        self.last_usage: AIUsage | None = None

    def extract_job_requirements(self, job_description: str) -> ExtractedJobRequirements:
        required, preferred = _extract_skill_buckets(job_description)
        text_lower = job_description.lower()
        result = ExtractedJobRequirements(
            required_skills=required,
            preferred_skills=preferred,
            seniority_level=_detect_seniority(text_lower),
            min_years_experience=_detect_min_years(text_lower),
            remote_type_detected=_detect_remote_type(text_lower),
        )
        self.last_usage = AIUsage()
        return result

    def tailor_resume(self, candidate, job, requirements, address) -> TailoredResumeDraft:
        """Deterministic, zero-cost stand-in for real AI tailoring: keeps every
        bullet verbatim (so nothing can fail truthfulness validation) and only
        reorders skills to put whatever the job asks for first -- no
        network call, so this is also what exercises the full tailoring
        pipeline in tests.
        """
        wanted = {s.lower() for s in (requirements.required_skills if requirements else [])}
        wanted |= {s.lower() for s in (requirements.preferred_skills if requirements else [])}
        candidate_skill_names = [s.name for s in candidate.skills]
        prioritized = [s for s in candidate_skill_names if s.lower() in wanted]
        rest = [s for s in candidate_skill_names if s.lower() not in wanted]

        bullets = [
            TailoredBulletDraft(text=b.text, source_bullet_id=b.id)
            for emp in candidate.employments
            for b in emp.bullets
        ]

        self.last_usage = AIUsage()
        return TailoredResumeDraft(
            summary=candidate.summary or f"Experienced professional applying for {job.title}.",
            skills_order=prioritized + rest,
            bullets=bullets,
        )

    def rewrite_passages(self, passages, job, requirements, candidate_skills) -> dict[int, str]:
        """Zero-cost stand-in: every passage kept verbatim."""
        self.last_usage = AIUsage()
        return {p["id"]: p["text"] for p in passages}

    def assess_fit(self, candidate: dict, job) -> FitAssessmentResult:
        """Zero-cost stand-in: the share of the posting's detected skills that
        appear in the candidate's skill list.
        """
        required, preferred = _extract_skill_buckets(job.description)
        wanted = list(dict.fromkeys(required + preferred))
        have = {s.lower() for s in candidate.get("skills", [])}
        matched = [s for s in wanted if s.lower() in have]
        gaps = [s for s in wanted if s.lower() not in have]
        score = round(100 * len(matched) / len(wanted)) if wanted else 50
        verdict = "strong" if score >= 85 else "good" if score >= 70 else "partial" if score >= 50 else "weak"
        self.last_usage = AIUsage()
        return FitAssessmentResult(
            fit_score=score, verdict=verdict, matched=matched[:8], gaps=gaps[:8],
            summary=f"Mock assessment: you list {len(matched)} of the {len(wanted)} skills detected in this posting.",
            recommendation="This is a quick keyword-based assessment; a detailed one isn't available right now.",
        )

    def generate_match_explanation(self, score: MatchScoreBreakdown) -> str:
        self.last_usage = AIUsage()
        return score.explanation
