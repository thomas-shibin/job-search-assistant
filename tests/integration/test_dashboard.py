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
    candidate = CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True, summary="Original summary")
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


def test_health_endpoint_returns_the_report(client):
    test_client, _ = client
    response = test_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert body["checks"][0]["name"] == "Database" and body["checks"][0]["status"] == "ok"


def test_job_list_blank_min_score_is_no_filter(client):
    """The filter form always submits min_score, blank when unset -- that must
    not be a 422."""
    test_client, SessionLocal = client
    _seed_job_with_resume(SessionLocal, status=JobStatus.APPLIED)

    response = test_client.get("/?status=APPLIED&min_score=")

    assert response.status_code == 200
    assert "Senior Engineer" in response.text


def test_tracked_jobs_partial_filters_without_the_page(client):
    test_client, SessionLocal = client
    _seed_job_with_resume(SessionLocal, status=JobStatus.APPLIED)  # scored 88

    for query, shown in [
        ("status=APPLIED&min_score=", True),
        ("status=&min_score=80", True),
        ("status=&min_score=90", False),
        ("status=SKIPPED&min_score=", False),
        ("status=bogus&min_score=abc", True),
    ]:
        response = test_client.get(f"/tracked-jobs?{query}")
        assert response.status_code == 200, query
        assert 'id="tracked-jobs"' in response.text
        assert "Find your next role" not in response.text  # just the table, no search
        assert ("Senior Engineer" in response.text) is shown, query


def test_job_detail_page_shows_match_and_resume_diff(client):
    test_client, SessionLocal = client
    job_id = _seed_job_with_resume(SessionLocal)

    response = test_client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert "Great fit" in response.text
    assert "88/100" in response.text
    assert "What changed in your resume" in response.text
    assert "0 of 2 bullets reworded" in response.text and "Summary rewritten" in response.text
    assert "1 bullet kept exactly as in your resume" in response.text


def test_download_links_are_excluded_from_hx_boost(client):
    """Regression test: hx-boost="true" on <body> (base.html) intercepts link
    clicks as AJAX/JS fetches, which silently breaks a real file download --
    the browser never sees the response's Content-Disposition header. Download
    links must opt out individually with hx-boost="false".
    """
    test_client, SessionLocal = client
    job_id = _seed_job_with_resume(SessionLocal)

    response = test_client.get(f"/jobs/{job_id}")

    assert 'href="/jobs/1/resume.docx" hx-boost="false"' in response.text
    assert "<del>Original</del> summary" in response.text and "<ins>Tailored</ins> summary" in response.text
    assert "your original wording is used: unknown source_bullet_id" in response.text  # the rejected bullet


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
