"""End-to-end test of hard filters + scoring against a real sqlite session
(spec sections 9/10/14): a job either gets filtered before any MatchAnalysis
exists, or gets scored and persisted, and status transitions accordingly.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.ai.mock import MockAIProvider
from app.config.schema import AIConfig, AppConfig, CandidateConfig, MatchingConfig, SearchProfile
from app.database.base import Base
from app.matching.pipeline import analyze_job
from app.models.ai_usage import AICallLog
from app.models.candidate import CandidateProfile, Employment, Skill
from app.models.job import Job, JobStatus
from app.models.job import JobRequirements
from app.models.matching import MatchAnalysis


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_candidate(session: Session) -> CandidateProfile:
    from datetime import date

    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True)
    profile.skills = [Skill(name=n) for n in ["Java", "Spring Boot", "AWS", "Kubernetes"]]
    profile.employments = [
        Employment(company="Prior Co", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True)
    ]
    session.add(profile)
    session.commit()
    return profile


def _seed_job(session: Session, **overrides) -> Job:
    defaults = dict(
        source="manual",
        source_url="https://example.com/job",
        company="Example Corp",
        title="Senior Backend Engineer",
        location="Vancouver, BC",
        description="Senior Backend Engineer with Java, Spring Boot, AWS, and Kubernetes experience.",
        status=JobStatus.DISCOVERED,
        fingerprint="fp-" + overrides.get("title", "job"),
        description_hash="dh-" + overrides.get("title", "job"),
    )
    defaults.update(overrides)
    job = Job(**defaults)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _config(ai: AIConfig | None = None, **matching_overrides) -> AppConfig:
    defaults = dict(
        required_or_high_value_skills=["Java", "Spring Boot"],
        preferred_skills=["AWS", "Kubernetes"],
        minimum_score=70,
    )
    defaults.update(matching_overrides)
    matching = MatchingConfig(**defaults)
    return AppConfig(
        candidate=CandidateConfig(name="Test", base_resume="x.docx"),
        matching=matching,
        ai=ai or AIConfig(),
    )


def test_qualified_job_gets_match_analysis_and_status_progression(session):
    _seed_candidate(session)
    job = _seed_job(session)
    config = _config()

    result = analyze_job(session, job, config)

    assert result.filter_result.passed is True
    assert result.qualified is True
    assert job.status == JobStatus.QUALIFIED

    analyses = session.query(MatchAnalysis).filter_by(job_id=job.id).all()
    assert len(analyses) == 1
    assert analyses[0].overall_score == result.breakdown.overall_score

    statuses = [h.to_status for h in job.status_history]
    assert JobStatus.ANALYZED in statuses
    assert JobStatus.QUALIFIED in statuses


def test_filtered_job_never_gets_match_analysis(session):
    _seed_candidate(session)
    job = _seed_job(session, title="PHP Developer", description="Looking for a PHP Developer.")
    config = _config(negative_keywords=["PHP Developer"])

    result = analyze_job(session, job, config)

    assert result.filter_result.passed is False
    assert result.breakdown is None
    assert session.query(MatchAnalysis).filter_by(job_id=job.id).count() == 0
    assert job.status == JobStatus.DISCOVERED  # unchanged


def test_below_threshold_job_gets_analysis_but_not_qualified(session):
    _seed_candidate(session)
    job = _seed_job(
        session,
        title="Office Manager",
        description="Manage office supplies and schedule meetings.",
        location="Nowhere",
    )
    config = _config(minimum_score=70)

    result = analyze_job(session, job, config)

    assert result.filter_result.passed is True
    assert result.breakdown is not None
    assert session.query(MatchAnalysis).filter_by(job_id=job.id).count() == 1
    assert job.status == JobStatus.ANALYZED
    assert result.qualified is False


# --- Milestone 5: AI-based requirement extraction wired into analyze_job,
# using MockAIProvider so these stay free/deterministic/offline.


def test_ai_enabled_persists_job_requirements_and_call_log(session):
    _seed_candidate(session)
    job = _seed_job(
        session,
        description=(
            "Senior Backend Engineer.\n\nRequirements:\n- Java\n- Spring Boot\n- Terraform\n\n"
            "Preferred:\n- AWS\n"
        ),
    )
    config = _config(ai=AIConfig(enabled=True, provider="mock"))

    result = analyze_job(session, job, config, ai_provider=MockAIProvider())

    assert result.filter_result.passed is True
    req = session.query(JobRequirements).filter_by(job_id=job.id).one()
    assert req.required_skills == ["Java", "Spring Boot", "Terraform"]

    call_log = session.query(AICallLog).filter_by(job_id=job.id).one()
    assert call_log.operation == "extract_job_requirements"
    assert call_log.cache_hit is False


def test_ai_extraction_recognizes_skill_outside_configured_lists(session):
    _seed_candidate(session)  # has Java, Spring Boot, AWS, Kubernetes
    candidate = session.query(CandidateProfile).filter_by(is_active=True).one()
    candidate.skills.append(Skill(name="Terraform"))
    session.commit()

    job = _seed_job(
        session,
        description="Senior Backend Engineer.\n\nRequirements:\n- Java\n- Spring Boot\n- Terraform\n",
    )
    # Default configured lists (Java, Spring Boot / AWS, Kubernetes) don't
    # mention Terraform at all -- only the AI extraction can surface it.
    config = _config(ai=AIConfig(enabled=True, provider="mock"))

    result = analyze_job(session, job, config, ai_provider=MockAIProvider())

    assert "Terraform" in result.breakdown.strong_matches


def test_ai_disabled_by_default_never_creates_job_requirements_or_call_log(session):
    _seed_candidate(session)
    job = _seed_job(session)
    config = _config()  # AIConfig() default: enabled=False

    analyze_job(session, job, config)

    assert session.query(JobRequirements).count() == 0
    assert session.query(AICallLog).count() == 0


def test_second_analyze_of_same_job_reuses_cached_requirements(session):
    _seed_candidate(session)
    job = _seed_job(session, description="Senior Backend Engineer.\n\nRequirements:\n- Java\n")
    config = _config(ai=AIConfig(enabled=True, provider="mock"))
    provider = MockAIProvider()

    analyze_job(session, job, config, ai_provider=provider)
    job.status = JobStatus.DISCOVERED  # allow re-analysis for this test
    session.commit()
    analyze_job(session, job, config, ai_provider=provider)

    assert session.query(JobRequirements).filter_by(job_id=job.id).count() == 1
    call_logs = session.query(AICallLog).filter_by(job_id=job.id).all()
    assert len(call_logs) == 2
    assert call_logs[0].cache_hit is False
    assert call_logs[1].cache_hit is True


def test_same_description_hash_across_jobs_reuses_requirements_without_new_ai_call(session):
    _seed_candidate(session)
    description = "Senior Backend Engineer.\n\nRequirements:\n- Java\n- Kafka\n"
    job_a = _seed_job(session, title="Job A", description=description, description_hash="dh-shared")
    job_b = _seed_job(session, title="Job B", description=description, description_hash="dh-shared")
    config = _config(ai=AIConfig(enabled=True, provider="mock"))
    provider = MockAIProvider()

    analyze_job(session, job_a, config, ai_provider=provider)
    analyze_job(session, job_b, config, ai_provider=provider)

    req_b = session.query(JobRequirements).filter_by(job_id=job_b.id).one()
    assert req_b.required_skills == ["Java", "Kafka"]

    log_b = session.query(AICallLog).filter_by(job_id=job_b.id).one()
    assert log_b.cache_hit is True


def test_generate_match_explanations_off_by_default_keeps_deterministic_text(session):
    _seed_candidate(session)
    job = _seed_job(session)
    config = _config()  # generate_match_explanations defaults to False

    result = analyze_job(session, job, config)

    assert session.query(AICallLog).filter_by(operation="generate_match_explanation").count() == 0
    assert result.breakdown.explanation
