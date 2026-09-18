"""End-to-end tests for the dashboard (Milestone 9): job list/detail pages
render, the resume diff view shows accepted vs. fallback bullets, and the
approve/skip/... action routes actually transition the job. Uses FastAPI's
dependency-override mechanism to point the app at an isolated in-memory-style
sqlite DB per test, rather than the process-wide DATABASE_URL.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.database.base import Base
from app.database.session import get_session
from app.models.candidate import AccomplishmentBullet, CandidateProfile, Employment
from app.models.job import Job, JobStatus
from app.models.matching import MatchAnalysis
from app.models.resume import TailoredBulletProvenance, TailoredResume


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_session():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    import main

    main.app.dependency_overrides[get_session] = override_get_session
    yield TestClient(main.app), TestSessionLocal
    main.app.dependency_overrides.clear()


def _seed_job_with_resume(SessionLocal, *, status=JobStatus.READY_FOR_REVIEW) -> int:
    session: Session = SessionLocal()
    candidate = CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True)
    bullet = AccomplishmentBullet(text="Reduced latency by 40%", order_index=0)
    candidate.employments = [Employment(company="Acme", title="Engineer", bullets=[bullet])]
    session.add(candidate)
    session.commit()

    job = Job(
        source="manual", source_url="https://example.com", company="Acme", title="Senior Engineer",
        description="d", status=status, fingerprint="fp1", description_hash="dh1",
    )
    session.add(job)
    session.commit()

    session.add(MatchAnalysis(job_id=job.id, overall_score=88, technical_score=90, explanation="Great fit"))
    tailored = TailoredResume(
        job_id=job.id, summary_text="Tailored summary", skills_order=["Java"],
        validation_status="PARTIAL", docx_path="x.docx",
    )
    session.add(tailored)
    session.commit()
    session.add(
        TailoredBulletProvenance(
            tailored_resume_id=tailored.id, generated_text="Reduced latency by 40%",
            source_bullet_id=bullet.id, validation_passed=True,
        )
    )
    session.add(
        TailoredBulletProvenance(
            tailored_resume_id=tailored.id, generated_text="Reduced latency by 40%",
            source_bullet_id=None, validation_passed=False, rejection_reason="unknown source_bullet_id",
        )
    )
    session.commit()
    job_id = job.id
    session.close()
    return job_id


def test_job_list_page_renders(client):
    test_client, SessionLocal = client
    _seed_job_with_resume(SessionLocal)

    response = test_client.get("/")

    assert response.status_code == 200
    assert "Senior Engineer" in response.text
    assert "Acme" in response.text


def test_job_list_filters_by_status(client):
    test_client, SessionLocal = client
    _seed_job_with_resume(SessionLocal, status=JobStatus.APPLIED)

    matching = test_client.get("/?status=APPLIED")
    assert "Senior Engineer" in matching.text

    non_matching = test_client.get("/?status=SKIPPED")
    assert "No jobs match" in non_matching.text


def test_job_detail_page_shows_match_and_resume_diff(client):
    test_client, SessionLocal = client
    job_id = _seed_job_with_resume(SessionLocal)

    response = test_client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Great fit" in response.text
    assert "88/100" in response.text
    assert "Tailored summary" in response.text
    assert "Kept original wording" in response.text  # the rejected/fallback bullet


def test_unknown_job_returns_404(client):
    test_client, _ = client
    response = test_client.get("/jobs/999")
    assert response.status_code == 404


def test_approve_action_transitions_job_and_redirects(client):
    test_client, SessionLocal = client
    job_id = _seed_job_with_resume(SessionLocal, status=JobStatus.READY_FOR_REVIEW)

    response = test_client.post(f"/jobs/{job_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}"

    session = SessionLocal()
    assert session.get(Job, job_id).status == JobStatus.APPROVED
    session.close()


def test_invalid_action_for_current_status_returns_409(client):
    test_client, SessionLocal = client
    job_id = _seed_job_with_resume(SessionLocal, status=JobStatus.READY_FOR_REVIEW)

    response = test_client.post(f"/jobs/{job_id}/mark-applied")  # must approve + open first

    assert response.status_code == 409


def test_costs_page_renders_with_no_data(client):
    test_client, _ = client
    response = test_client.get("/costs")
    assert response.status_code == 200
    assert "No AI calls logged yet." in response.text


def test_api_list_jobs_returns_json(client):
    test_client, SessionLocal = client
    job_id = _seed_job_with_resume(SessionLocal)

    response = test_client.get("/api/jobs")

    assert response.status_code == 200
    data = response.json()
    assert data[0]["id"] == job_id
    assert data[0]["overall_score"] == 88
