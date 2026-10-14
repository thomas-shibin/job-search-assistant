"""Deterministic, weighted match scoring (spec section 9). No AI/LLM call
happens anywhere in this module -- see docs/ARCHITECTURE.md's
deterministic-vs-AI table. The score is a pure function of the candidate
profile, the job's own text, the search profile (if any), and the configured
weights/skill lists, so it is fully reproducible and unit-testable, and an
unchanged job never needs re-scoring.

Requirement recognition, without AI, is limited to what's either explicitly
configured (`matching.required_or_high_value_skills`,
`matching.preferred_skills`) or detectable with simple, generic regexes (a
"N+ years" pattern, a seniority keyword) -- a job asking for a skill nobody
has configured (e.g. "Terraform" when it's not in your skill lists) is
otherwise invisible to scoring. Milestone 5 (`app/ai/service.py`,
`JobRequirements`, cached by `description_hash`) closes that gap: `score_job`
takes the AI-extracted requirements as an optional `requirements` parameter
and uses them only to *extend* the regex/configured-list detection above --
never to override an explicit signal the job's own text (or
`Job.remote_type`) already provides. `requirements=None` (the default when
`ai.enabled` is false in config.yaml) reproduces pre-Milestone-5 behavior
exactly, so scores should still be read as "how well you match on the things
we know to look for" whenever AI extraction is off.

Two documented neutral defaults are used instead of guessing when data is
ambiguous, per the spec's "avoid false precision" instruction:

- `NO_REQUIREMENT_SCORE` (100): the posting doesn't state a requirement for
  this dimension at all (no years-of-experience number, no degree
  requirement), so there is nothing to fail against -- full credit rather
  than a penalty for the requirement's absence.
- `UNKNOWN_FIT_SCORE` (50): we genuinely cannot tell whether this dimension
  fits (no search profile to check location against, no configured skills
  were even mentioned in the posting, no title data to compare). This is
  distinct from "requirement satisfied" -- it's "insufficient signal either
  way" -- and is called out explicitly in the explanation text.
"""
from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.core.config.schema import MatchingConfig, SearchProfile
from app.ingestion.filters import location_matches
from app.models.candidate import CandidateProfile
from app.models.job import Job

if TYPE_CHECKING:
    from app.integrations.ai.base import ExtractedJobRequirements

NO_REQUIREMENT_SCORE = 100
UNKNOWN_FIT_SCORE = 50

_STOPWORDS = frozenset(
    """
    a an the and or but if of to in on for with as at by from is are was were
    be been being this that these those we you they it our your their will
    can may must should would have has had not no do does did
    """.split()
)

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", re.IGNORECASE)
_DEGREE_RE = re.compile(
    r"\b(bachelor'?s?|master'?s?|b\.?s\.?|m\.?s\.?|ph\.?d\.?|degree)\b", re.IGNORECASE
)

_SENIORITY_LEVELS: list[tuple[str, int]] = [
    ("intern", 0),
    ("junior", 1),
    ("associate", 1),
    ("mid", 2),
    ("intermediate", 2),
    ("senior", 3),
    ("staff", 4),
    ("lead", 4),
    ("principal", 5),
    ("director", 5),
]


class MatchScoreBreakdown(BaseModel):
    """Explainable output of the deterministic scoring formula (spec §9). Never produced
    by asking an LLM for a percentage directly.
    """

    overall_score: int
    technical_score: int
    experience_score: int
    title_score: int
    seniority_score: int
    platform_score: int
    location_score: int
    domain_score: int
    education_score: int
    strong_matches: list[str] = Field(default_factory=list)
    partial_matches: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    critical_missing_requirements: list[str] = Field(default_factory=list)
    explanation: str = ""


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _skill_mentioned(skill: str, text_lower: str) -> bool:
    return re.search(rf"\b{re.escape(skill.lower())}\b", text_lower) is not None


def _candidate_skill_names(candidate: CandidateProfile) -> set[str]:
    return {s.name.lower() for s in candidate.skills}


def _candidate_has_skill(skill: str, candidate_skills: set[str]) -> bool:
    return skill.lower() in candidate_skills


def _merge_extra_skills(mentioned: list[str], extra: list[str]) -> list[str]:
    """Appends `extra` (e.g. AI-extracted skills, already known to have come
    from this job's own text -- see `app/ai/base.py`) onto `mentioned`,
    skipping anything already covered case-insensitively.
    """
    if not extra:
        return mentioned
    seen = {s.lower() for s in mentioned}
    merged = list(mentioned)
    for skill in extra:
        if skill and skill.lower() not in seen:
            merged.append(skill)
            seen.add(skill.lower())
    return merged


def _score_technical_and_platform(
    job_text_lower: str,
    candidate_skills: set[str],
    matching_config: MatchingConfig,
    extracted_required: list[str] | None = None,
    extracted_preferred: list[str] | None = None,
) -> tuple[int, int, list[str], list[str], list[str], list[str], bool]:
    """Returns (technical_score, platform_score, strong_matches, partial_matches,
    missing_requirements, critical_missing_requirements, no_skills_detected).

    `no_skills_detected` is tracked explicitly (not inferred from
    `technical_score == UNKNOWN_FIT_SCORE`) since a real ratio-based score can
    legitimately equal that same number by coincidence.

    Required skills mentioned in the posting and matched -> strong. Required
    skills mentioned but absent from the candidate -> missing + critical.
    Preferred skills mentioned and matched -> strong. Preferred skills
    mentioned but absent -> partial (a "nice to have" gap, not disqualifying).

    `extracted_required`/`extracted_preferred` (Milestone 5) extend the
    configured skill lists with whatever the AI provider found in this job's
    own text -- unlike the configured lists, they're not re-verified with
    `_skill_mentioned` since the extraction already read them off this text.
    """
    strong: list[str] = []
    partial: list[str] = []
    missing: list[str] = []
    critical_missing: list[str] = []

    required_mentioned = [s for s in matching_config.required_or_high_value_skills if _skill_mentioned(s, job_text_lower)]
    preferred_mentioned = [s for s in matching_config.preferred_skills if _skill_mentioned(s, job_text_lower)]
    required_mentioned = _merge_extra_skills(required_mentioned, extracted_required or [])
    preferred_mentioned = _merge_extra_skills(preferred_mentioned, extracted_preferred or [])
    required_lower = {s.lower() for s in required_mentioned}
    preferred_mentioned = [s for s in preferred_mentioned if s.lower() not in required_lower]

    for skill in required_mentioned:
        if _candidate_has_skill(skill, candidate_skills):
            strong.append(skill)
        else:
            missing.append(skill)
            critical_missing.append(skill)

    for skill in preferred_mentioned:
        if _candidate_has_skill(skill, candidate_skills):
            strong.append(skill)
        else:
            partial.append(skill)

    total_relevant = len(required_mentioned) + len(preferred_mentioned)
    no_skills_detected = total_relevant == 0
    if no_skills_detected:
        technical_score = UNKNOWN_FIT_SCORE
    else:
        # Required matches count double weight relative to preferred within
        # this dimension, reflecting that they matter more than "nice to have".
        weighted_matched = 2 * sum(1 for s in required_mentioned if _candidate_has_skill(s, candidate_skills)) + sum(
            1 for s in preferred_mentioned if _candidate_has_skill(s, candidate_skills)
        )
        weighted_total = 2 * len(required_mentioned) + len(preferred_mentioned)
        technical_score = round(100 * weighted_matched / weighted_total)

    if not preferred_mentioned:
        platform_score = NO_REQUIREMENT_SCORE
    else:
        matched_preferred = sum(1 for s in preferred_mentioned if _candidate_has_skill(s, candidate_skills))
        platform_score = round(100 * matched_preferred / len(preferred_mentioned))

    return technical_score, platform_score, strong, partial, missing, critical_missing, no_skills_detected


def _total_years_experience(candidate: CandidateProfile) -> float | None:
    spans: list[tuple[date, date]] = []
    for emp in candidate.employments:
        if emp.start_date is None:
            continue
        end = emp.end_date or date.today()
        spans.append((emp.start_date, end))
    if not spans:
        return None
    total_days = sum((end - start).days for start, end in spans)
    return round(total_days / 365.25, 1)


def _score_experience(
    job_text_lower: str, candidate: CandidateProfile, extracted_min_years: int | None = None
) -> tuple[int, bool]:
    """Returns (score, no_requirement_stated) -- the flag is tracked explicitly
    rather than inferred from `score == NO_REQUIREMENT_SCORE`, since a genuine
    strong match can also legitimately score 100.

    `extracted_min_years` (Milestone 5) is used only when the posting's own
    text doesn't match the generic "N+ years" regex -- it never overrides an
    explicit in-text number.
    """
    match = _YEARS_RE.search(job_text_lower)
    if not match:
        if extracted_min_years is None:
            return NO_REQUIREMENT_SCORE, True
        required_years = extracted_min_years
    else:
        required_years = int(match.group(1))

    candidate_years = _total_years_experience(candidate)
    if candidate_years is None:
        return UNKNOWN_FIT_SCORE, False
    if candidate_years >= required_years or required_years == 0:
        return 100, False
    return max(0, round(100 * candidate_years / required_years)), False


def _most_recent_title(candidate: CandidateProfile) -> str | None:
    dated = [e for e in candidate.employments if e.start_date is not None]
    if not dated:
        return candidate.employments[0].title if candidate.employments else None
    return max(dated, key=lambda e: (e.is_current, e.start_date)).title


def _score_title(job: Job, candidate: CandidateProfile, search_profile: SearchProfile | None) -> int:
    reference_titles: list[str] = []
    if search_profile is not None and search_profile.titles:
        reference_titles = search_profile.titles
    else:
        recent = _most_recent_title(candidate)
        if recent:
            reference_titles = [recent]

    if not reference_titles:
        return UNKNOWN_FIT_SCORE

    job_tokens = _tokenize(job.title)
    if not job_tokens:
        return UNKNOWN_FIT_SCORE

    best = 0.0
    for ref in reference_titles:
        ref_tokens = _tokenize(ref)
        if not ref_tokens:
            continue
        overlap = len(job_tokens & ref_tokens) / len(job_tokens | ref_tokens)
        best = max(best, overlap)
    return round(best * 100)


def _detect_seniority_level(text: str) -> int | None:
    text_lower = text.lower()
    for keyword, level in sorted(_SENIORITY_LEVELS, key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{keyword}\b", text_lower):
            return level
    return None


def _score_seniority(job: Job, candidate: CandidateProfile, extracted_seniority: str | None = None) -> int:
    job_level = _detect_seniority_level(job.title)
    if job_level is None and extracted_seniority:
        # Fallback only: the title itself carries no seniority keyword (e.g.
        # "Backend Engineer" with seniority only stated in the body text),
        # which Milestone 5's extraction can see and the title can't.
        job_level = _detect_seniority_level(extracted_seniority)
    candidate_title = _most_recent_title(candidate)
    candidate_level = _detect_seniority_level(candidate_title) if candidate_title else None

    if job_level is None or candidate_level is None:
        return UNKNOWN_FIT_SCORE

    diff = abs(job_level - candidate_level)
    if diff == 0:
        return 100
    if diff == 1:
        return 70
    return 30


def _score_location(
    job: Job, search_profile: SearchProfile | None, extracted_remote_type: str | None = None
) -> tuple[int, bool]:
    """Returns (score, no_search_profile) -- see `_score_experience` docstring.

    `extracted_remote_type` (Milestone 5) only fills in when the job's own
    deterministic `remote_type` (set during ingestion normalization) is
    unset -- it never overrides a value normalization already determined.
    """
    if search_profile is None or not search_profile.locations:
        return UNKNOWN_FIT_SCORE, True
    remote_type = job.remote_type or extracted_remote_type
    return (100 if location_matches(job.location, remote_type, search_profile.locations) else 0), False


def _score_domain(job: Job, candidate: CandidateProfile) -> int:
    job_tokens = _tokenize(job.description)
    candidate_text = " ".join(
        bullet.text for emp in candidate.employments for bullet in emp.bullets
    ) + " " + " ".join(p.description or "" for p in candidate.projects)
    candidate_tokens = _tokenize(candidate_text)

    if not job_tokens or not candidate_tokens:
        return UNKNOWN_FIT_SCORE

    overlap = len(job_tokens & candidate_tokens) / len(job_tokens | candidate_tokens)
    # Jaccard similarity over free text is naturally low even for a strong
    # match (shared vocabulary is a small fraction of either document), so
    # scale against an empirically reasonable ceiling rather than reporting
    # the raw ratio, which would make every job look like a poor domain fit.
    scaled = min(100, round(overlap * 400))
    return scaled


def _score_education(job_text_lower: str, candidate: CandidateProfile) -> tuple[int, bool]:
    """Returns (score, no_requirement_stated) -- see `_score_experience` docstring."""
    if not _DEGREE_RE.search(job_text_lower):
        return NO_REQUIREMENT_SCORE, True
    return (100, False) if candidate.education else (UNKNOWN_FIT_SCORE, False)


def _build_explanation(breakdown_fields: dict[str, Any]) -> str:
    parts: list[str] = []
    if breakdown_fields["strong_matches"]:
        parts.append(f"Strong match on {', '.join(breakdown_fields['strong_matches'])}.")
    if breakdown_fields["partial_matches"]:
        parts.append(f"Partially covered: {', '.join(breakdown_fields['partial_matches'])} (preferred, not required).")
    if breakdown_fields["critical_missing_requirements"]:
        parts.append(f"Missing required skills: {', '.join(breakdown_fields['critical_missing_requirements'])}.")
    if not parts:
        parts.append("No configured required/preferred skills were detected in this posting's text.")

    ambiguous_dims = [name for name, val in breakdown_fields["ambiguity"] if val]
    if ambiguous_dims:
        parts.append(
            "Note: "
            + "; ".join(ambiguous_dims)
            + ". These dimensions use a neutral default rather than a guessed score."
        )
    return " ".join(parts)


def score_job(
    candidate: CandidateProfile,
    job: Job,
    search_profile: SearchProfile | None,
    matching_config: MatchingConfig,
    requirements: ExtractedJobRequirements | None = None,
) -> MatchScoreBreakdown:
    """Pure, deterministic weighted score of `job` against `candidate`.

    Deviates from the Milestone-1 stub signature (`score_job(candidate,
    requirements, search_profile, weights)`) by taking the `Job` and the full
    `MatchingConfig` directly instead of a bare weights object: this function
    needs the config's skill lists (not just weights) to do keyword-based
    extraction for whatever `requirements` doesn't cover. See the module
    docstring.

    `requirements` (Milestone 5, optional -- `None` reproduces pre-Milestone-5
    behavior exactly) is the AI-extracted `JobRequirements` for this job, used
    only to *extend* what the deterministic regexes/configured skill lists can
    already see (extra recognized skills, and a fallback when the posting's
    own text doesn't match the generic years/seniority/remote-type patterns),
    never to override an explicit, deterministic signal.
    """
    job_text_lower = f"{job.title}\n{job.description}".lower()
    candidate_skills = _candidate_skill_names(candidate)

    extracted_required = requirements.required_skills if requirements else None
    extracted_preferred = requirements.preferred_skills if requirements else None
    extracted_min_years = requirements.min_years_experience if requirements else None
    extracted_seniority = requirements.seniority_level if requirements else None
    extracted_remote_type = requirements.remote_type_detected if requirements else None

    technical_score, platform_score, strong, partial, missing, critical_missing, no_skills_detected = (
        _score_technical_and_platform(
            job_text_lower, candidate_skills, matching_config, extracted_required, extracted_preferred
        )
    )
    experience_score, no_experience_requirement = _score_experience(job_text_lower, candidate, extracted_min_years)
    title_score = _score_title(job, candidate, search_profile)
    seniority_score = _score_seniority(job, candidate, extracted_seniority)
    location_score, no_search_profile = _score_location(job, search_profile, extracted_remote_type)
    domain_score = _score_domain(job, candidate)
    education_score, no_education_requirement = _score_education(job_text_lower, candidate)

    weights = matching_config.weights
    overall_score = round(
        (
            weights.technical_skills * technical_score
            + weights.relevant_experience * experience_score
            + weights.title_similarity * title_score
            + weights.seniority * seniority_score
            + weights.platform * platform_score
            + weights.location * location_score
            + weights.domain_experience * domain_score
            + weights.education * education_score
        )
        / 100
    )

    ambiguity = [
        (text, True)
        for text, flag in (
            ("no years-of-experience requirement stated", no_experience_requirement),
            ("no degree requirement stated", no_education_requirement),
            ("no search profile location to compare against", no_search_profile),
            ("no configured skills were detected in this posting", no_skills_detected),
        )
        if flag
    ]

    fields = dict(
        strong_matches=strong,
        partial_matches=partial,
        missing_requirements=missing,
        critical_missing_requirements=critical_missing,
        ambiguity=ambiguity,
    )

    return MatchScoreBreakdown(
        overall_score=overall_score,
        technical_score=technical_score,
        experience_score=experience_score,
        title_score=title_score,
        seniority_score=seniority_score,
        platform_score=platform_score,
        location_score=location_score,
        domain_score=domain_score,
        education_score=education_score,
        strong_matches=strong,
        partial_matches=partial,
        missing_requirements=missing,
        critical_missing_requirements=critical_missing,
        explanation=_build_explanation(fields),
    )
