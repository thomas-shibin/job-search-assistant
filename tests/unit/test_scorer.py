"""Matching-calculation tests -- the most important test file for this
milestone per spec section 25. Builds in-memory ORM objects directly (no DB
session needed since `score_job` is a pure function over its arguments).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.core.config.schema import MatchingConfig, MatchingWeights, SearchProfile
from app.integrations.ai.base import ExtractedJobRequirements
from app.modules.jobs.models import Job, JobStatus
from app.modules.matching.scorer import NO_REQUIREMENT_SCORE, UNKNOWN_FIT_SCORE, score_job
from app.modules.resumes.models import AccomplishmentBullet, CandidateProfile, Employment, Skill


def _candidate(skills: list[str], employments: list[Employment] | None = None) -> CandidateProfile:
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h")
    profile.skills = [Skill(name=name) for name in skills]
    profile.employments = employments or [
        Employment(
            company="Prior Co",
            title="Senior Backend Engineer",
            start_date=date(2019, 1, 1),
            end_date=None,
            is_current=True,
            bullets=[AccomplishmentBullet(text="Built distributed systems serving millions of requests", order_index=0)],
        )
    ]
    profile.projects = []
    profile.education = []
    return profile


def _job(title: str, description: str, location: str | None = "Vancouver, BC") -> Job:
    return Job(
        id=1,
        source="manual",
        source_url="https://example.com",
        company="Example Corp",
        title=title,
        location=location,
        remote_type=None,
        description=description,
        status=JobStatus.DISCOVERED,
        fingerprint="fp",
        description_hash="dh",
    )


def _config(**overrides) -> MatchingConfig:
    defaults = dict(
        required_or_high_value_skills=["Java", "Spring Boot", "Distributed Systems"],
        preferred_skills=["AWS", "Kubernetes", "Docker", "Kafka"],
    )
    defaults.update(overrides)
    return MatchingConfig(**defaults)


STRONG_JOB_DESCRIPTION = (
    "Senior Backend Engineer with 5+ years experience in Java, Spring Boot, "
    "AWS, and Kubernetes, building distributed systems."
)


def test_strong_match_scores_high_with_correct_strong_matches():
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems", "AWS", "Kubernetes"])
    sp = SearchProfile(name="BC Backend", address_profile="bc", locations=["Vancouver, BC"], titles=["Senior Backend Engineer"])
    breakdown = score_job(candidate, _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION), sp, _config())

    assert breakdown.overall_score >= 85
    assert set(breakdown.strong_matches) >= {"Java", "Spring Boot", "AWS", "Kubernetes"}
    assert breakdown.critical_missing_requirements == []


def test_missing_required_skill_lowers_technical_score_and_is_reported():
    # Candidate lacks "Distributed Systems", which the posting explicitly mentions.
    candidate = _candidate(["Java", "Spring Boot", "AWS", "Kubernetes"])
    breakdown = score_job(candidate, _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION), None, _config())

    assert "Distributed Systems" in breakdown.missing_requirements
    assert "Distributed Systems" in breakdown.critical_missing_requirements
    assert breakdown.technical_score < 100


def test_critical_missing_requirements_populated_for_absent_required_skill():
    candidate = _candidate(["AWS", "Kubernetes"])  # no required skills at all
    breakdown = score_job(candidate, _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION), None, _config())

    assert set(breakdown.critical_missing_requirements) == {"Java", "Spring Boot", "Distributed Systems"}


def test_preferred_skill_missing_is_partial_not_critical():
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems", "AWS"])  # missing Kubernetes (preferred)
    breakdown = score_job(candidate, _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION), None, _config())

    assert "Kubernetes" in breakdown.partial_matches
    assert "Kubernetes" not in breakdown.critical_missing_requirements


def test_weights_change_shifts_overall_score():
    candidate = _candidate(["Java"])  # weak match on everything except a lone required skill
    job = _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION)

    heavy_technical = _config(weights=MatchingWeights(
        technical_skills=90, relevant_experience=0, title_similarity=0, seniority=0,
        platform=0, location=0, domain_experience=0, education=10,
    ))
    heavy_location = _config(weights=MatchingWeights(
        technical_skills=0, relevant_experience=0, title_similarity=0, seniority=0,
        platform=0, location=90, domain_experience=0, education=10,
    ))

    score_a = score_job(candidate, job, None, heavy_technical).overall_score
    score_b = score_job(candidate, job, None, heavy_location).overall_score
    assert score_a != score_b


def test_ambiguous_data_uses_documented_neutral_defaults_not_false_confidence():
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems"])
    # No years-of-experience number, no degree keyword, no search profile at all.
    job = _job("Senior Backend Engineer", "Join our team working with Java and Spring Boot.", location=None)
    breakdown = score_job(candidate, job, None, _config())

    assert breakdown.experience_score == NO_REQUIREMENT_SCORE
    assert breakdown.education_score == NO_REQUIREMENT_SCORE
    assert breakdown.location_score == UNKNOWN_FIT_SCORE


def test_no_configured_skills_mentioned_in_posting_is_unknown_not_zero():
    candidate = _candidate(["Java", "Spring Boot"])
    job = _job("Office Manager", "Manage office supplies and schedule meetings.")
    breakdown = score_job(candidate, job, None, _config())

    assert breakdown.technical_score == UNKNOWN_FIT_SCORE
    assert breakdown.strong_matches == []
    assert breakdown.critical_missing_requirements == []


def test_explanation_is_nonempty_and_mentions_key_skills():
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems", "AWS", "Kubernetes"])
    breakdown = score_job(candidate, _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION), None, _config())

    assert breakdown.explanation
    assert "Java" in breakdown.explanation


def test_stated_and_satisfied_requirements_are_not_flagged_ambiguous():
    """Regression test: a job stating '5+ years' and requiring a degree, both
    satisfied by the candidate, legitimately scores 100 on those dimensions --
    that must not be confused with the NO_REQUIREMENT_SCORE=100 default used
    when the posting states no requirement at all. Caught during manual
    verification when the explanation wrongly claimed no requirement was
    stated for a posting that explicitly said "5+ years" and "Bachelor's
    degree required".
    """
    from app.modules.resumes.models import Education

    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems"])
    candidate.employments = [
        Employment(company="X", title="Senior Backend Engineer", start_date=date(2015, 1, 1), is_current=True)
    ]
    candidate.education = [Education(institution="Some University", degree="BSc Computer Science")]

    job = _job(
        "Senior Backend Engineer",
        "5+ years experience with Java, Spring Boot, and distributed systems. "
        "Bachelor's degree in Computer Science required.",
    )
    breakdown = score_job(candidate, job, None, _config())

    assert breakdown.experience_score == 100
    assert breakdown.education_score == 100
    assert "no years-of-experience requirement stated" not in breakdown.explanation
    assert "no degree requirement stated" not in breakdown.explanation


def test_seniority_mismatch_scores_lower_than_exact_match():
    senior_candidate = _candidate(
        ["Java"],
        employments=[Employment(company="X", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True)],
    )
    junior_candidate = _candidate(
        ["Java"],
        employments=[Employment(company="X", title="Junior Backend Engineer", start_date=date(2023, 1, 1), is_current=True)],
    )
    job = _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION)

    senior_score = score_job(senior_candidate, job, None, _config()).seniority_score
    junior_score = score_job(junior_candidate, job, None, _config()).seniority_score
    assert senior_score > junior_score


# --- Milestone 5: AI-extracted requirements extend, never override, the
# deterministic signals above. `requirements=None` (the default, exercised by
# every test above) must remain byte-for-byte identical to pre-Milestone-5
# behavior -- these tests only cover the additive `requirements is not None`
# path.


def test_extracted_skill_not_in_configured_lists_is_recognized():
    # "Terraform" isn't in the candidate's configured required/preferred
    # lists at all -- exactly the gap Milestone 5's docstring says AI
    # extraction closes (see app/matching/scorer.py module docstring).
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems", "Terraform"])
    job = _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION + " Also requires Terraform.")
    requirements = ExtractedJobRequirements(required_skills=["Terraform"])

    without_ai = score_job(candidate, job, None, _config())
    with_ai = score_job(candidate, job, None, _config(), requirements)

    assert "Terraform" not in without_ai.strong_matches
    assert "Terraform" in with_ai.strong_matches


def test_extracted_required_skill_missing_from_candidate_is_critical_missing():
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems"])
    job = _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION + " Also requires Terraform.")
    requirements = ExtractedJobRequirements(required_skills=["Terraform"])

    breakdown = score_job(candidate, job, None, _config(), requirements)

    assert "Terraform" in breakdown.critical_missing_requirements


def test_extracted_years_used_only_when_text_has_no_generic_years_pattern():
    candidate = _candidate(
        ["Java"],
        employments=[Employment(company="X", title="Senior Backend Engineer", start_date=date(2023, 1, 1), is_current=True)],
    )
    job = _job("Senior Backend Engineer", "Join our team working with Java. No prior experience number stated.")

    without_ai = score_job(candidate, job, None, _config())
    with_ai = score_job(candidate, job, None, _config(), ExtractedJobRequirements(min_years_experience=10))

    assert without_ai.experience_score == NO_REQUIREMENT_SCORE
    assert with_ai.experience_score < 100  # candidate has ~2 years, well under the extracted 10


def test_extracted_years_does_not_override_explicit_text_years():
    candidate = _candidate(
        ["Java"],
        employments=[Employment(company="X", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True)],
    )
    job = _job("Senior Backend Engineer", STRONG_JOB_DESCRIPTION)  # states "5+ years" explicitly

    # A conflicting extraction (e.g. a provider hallucination) must not
    # override what the posting's own text explicitly says.
    breakdown = score_job(candidate, job, None, _config(), ExtractedJobRequirements(min_years_experience=20))

    assert breakdown.experience_score == 100


def test_extracted_seniority_used_only_when_title_has_no_keyword():
    candidate = _candidate(
        ["Java"],
        employments=[Employment(company="X", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True)],
    )
    job = _job("Backend Engineer II", STRONG_JOB_DESCRIPTION)  # title carries no seniority keyword

    without_ai = score_job(candidate, job, None, _config())
    with_ai = score_job(candidate, job, None, _config(), ExtractedJobRequirements(seniority_level="senior"))

    assert without_ai.seniority_score == UNKNOWN_FIT_SCORE
    assert with_ai.seniority_score == 100


def test_extracted_remote_type_used_only_when_job_remote_type_is_unset():
    job = Job(
        id=1, source="manual", source_url="https://example.com", company="Example Corp",
        title="Senior Backend Engineer", location=None, remote_type=None,
        description=STRONG_JOB_DESCRIPTION, status=JobStatus.DISCOVERED,
        fingerprint="fp", description_hash="dh",
    )
    candidate = _candidate(["Java", "Spring Boot", "Distributed Systems", "AWS", "Kubernetes"])
    sp = SearchProfile(name="Remote", address_profile="bc", locations=["Remote Canada"])

    without_ai = score_job(candidate, job, sp, _config())
    with_ai = score_job(candidate, job, sp, _config(), ExtractedJobRequirements(remote_type_detected="remote"))

    assert without_ai.location_score == 0
    assert with_ai.location_score == 100
