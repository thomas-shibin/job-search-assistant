"""Tests for the free, deterministic `MockAIProvider` (Milestone 5) -- both
its own extraction heuristic and its "structurally satisfies AIProvider"
contract.
"""
from __future__ import annotations

from app.integrations.ai.base import ExtractedJobRequirements
from app.integrations.ai.mock import MockAIProvider
from app.modules.matching.scorer import MatchScoreBreakdown

STRUCTURED_POSTING = """
Senior Backend Engineer

About us: we build distributed systems at scale.

Requirements:
- 5+ years of backend engineering experience
- Java
- Spring Boot
- Distributed Systems

Preferred:
- AWS
- Kubernetes

This is a fully remote position.
"""

UNSTRUCTURED_POSTING = """
We're looking for someone who knows Java and has built distributed systems
before. Come join our senior backend team!
"""


def test_extracts_required_and_preferred_skills_from_structured_sections():
    result = MockAIProvider().extract_job_requirements(STRUCTURED_POSTING)

    assert result.required_skills == ["Java", "Spring Boot", "Distributed Systems"]
    assert result.preferred_skills == ["AWS", "Kubernetes"]


def test_detects_years_seniority_and_remote_type():
    result = MockAIProvider().extract_job_requirements(STRUCTURED_POSTING)

    assert result.min_years_experience == 5
    assert result.seniority_level == "senior"
    assert result.remote_type_detected == "remote"


def test_unstructured_posting_yields_no_guessed_skills():
    """Honest limitation: without a recognized "Requirements:"-style section,
    the heuristic reports no skills rather than guessing from prose.
    """
    result = MockAIProvider().extract_job_requirements(UNSTRUCTURED_POSTING)

    assert result.required_skills == []
    assert result.preferred_skills == []


def test_returns_extracted_job_requirements_instance():
    result = MockAIProvider().extract_job_requirements(STRUCTURED_POSTING)
    assert isinstance(result, ExtractedJobRequirements)


def test_last_usage_is_free_after_extraction():
    provider = MockAIProvider()
    provider.extract_job_requirements(STRUCTURED_POSTING)

    assert provider.last_usage is not None
    assert provider.last_usage.tokens_in is None
    assert provider.last_usage.estimated_cost is None


def test_generate_match_explanation_passes_through_deterministic_text():
    breakdown = MatchScoreBreakdown(
        overall_score=80, technical_score=80, experience_score=100, title_score=80,
        seniority_score=100, platform_score=100, location_score=100, domain_score=50,
        education_score=100, explanation="Strong match on Java.",
    )
    result = MockAIProvider().generate_match_explanation(breakdown)
    assert result == "Strong match on Java."


def _candidate_with_bullets():
    from app.modules.resumes.models import AccomplishmentBullet, CandidateProfile, Employment, Skill

    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", summary="Backend engineer.")
    bullet = AccomplishmentBullet(id=1, text="Built a service handling 1M requests/day", order_index=0)
    profile.employments = [Employment(id=1, company="Acme", title="Engineer", bullets=[bullet])]
    profile.skills = [Skill(name="Java"), Skill(name="AWS")]
    return profile


def _job(title="Senior Backend Engineer"):
    from app.modules.jobs.models import Job, JobStatus

    return Job(
        id=1, source="manual", source_url="https://example.com", company="Acme",
        title=title, description="", status=JobStatus.DISCOVERED, fingerprint="fp", description_hash="dh",
    )


def test_tailor_resume_keeps_every_bullet_verbatim_citing_its_source():
    draft = MockAIProvider().tailor_resume(_candidate_with_bullets(), _job(), None, None)

    assert len(draft.bullets) == 1
    assert draft.bullets[0].source_bullet_id == 1
    assert draft.bullets[0].text == "Built a service handling 1M requests/day"


def test_tailor_resume_prioritizes_skills_the_job_asks_for():
    requirements = ExtractedJobRequirements(required_skills=["AWS"])
    draft = MockAIProvider().tailor_resume(_candidate_with_bullets(), _job(), requirements, None)

    assert draft.skills_order[0] == "AWS"
    assert set(draft.skills_order) == {"Java", "AWS"}
