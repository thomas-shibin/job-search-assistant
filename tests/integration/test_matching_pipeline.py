"""End-to-end test of hard filters + scoring against a real sqlite session
(spec sections 9/10/14): a job either gets filtered before any MatchAnalysis
exists, or gets scored and persisted, and status transitions accordingly.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.config.schema import AppConfig, CandidateConfig, MatchingConfig, SearchProfile
from app.database.base import Base
from app.matching.pipeline import analyze_job
from app.models.candidate import CandidateProfile, Employment, Skill
from app.models.job import Job, JobStatus
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


def _config(**matching_overrides) -> AppConfig:
    defaults = dict(
        required_or_high_value_skills=["Java", "Spring Boot"],
        preferred_skills=["AWS", "Kubernetes"],
        minimum_score=70,
    )
    defaults.update(matching_overrides)
    matching = MatchingConfig(**defaults)
    return AppConfig(candidate=CandidateConfig(name="Test", base_resume="x.docx"), matching=matching)


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
