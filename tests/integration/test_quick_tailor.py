"""End-to-end test of the "quick tailor" flow: upload a resume + paste a job
description in one request, get redirected straight to the tailored-resume
detail page -- no CLI commands, no pre-existing CandidateProfile/Job needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.config.schema import AIConfig, AppConfig, CandidateConfig, MatchingConfig
from app.database.base import Base
from app.database.session import get_session

FIXTURE_RESUME = Path(__file__).resolve().parent.parent / "fixtures" / "sample_resume.docx"

JOB_DESCRIPTION = """Senior Backend Engineer at Acme

Requirements:
- Java
- Spring Boot
"""


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

    import app.ui.routes as ui_routes
    import main

    main.app.dependency_overrides[get_session] = override_get_session

    config = AppConfig(
        candidate=CandidateConfig(name="Jordan Casey", base_resume="x.docx"),
        matching=MatchingConfig(minimum_score=999),  # deliberately unreachable -- quick-tailor must ignore it
        ai=AIConfig(provider="mock"),
    )
    dummy_config_path = tmp_path / "config.yaml"
    dummy_config_path.write_text("placeholder -- load_config is mocked below", encoding="utf-8")
    monkeypatch.setattr(ui_routes, "DEFAULT_CONFIG_PATH", dummy_config_path)
    monkeypatch.setattr(ui_routes, "load_config", lambda path: config)

    yield TestClient(main.app), TestSessionLocal
    main.app.dependency_overrides.clear()


def test_quick_tailor_form_renders(client):
    test_client, _ = client
    response = test_client.get("/quick")
    assert response.status_code == 200
    assert "Tailor my resume" in response.text


def test_quick_tailor_rejects_non_resume_file(client):
    test_client, _ = client
    response = test_client.post(
        "/quick",
        data={"description": JOB_DESCRIPTION},
        files={"resume": ("resume.txt", b"not a resume", "text/plain")},
    )
    assert response.status_code == 400
    assert "must be a .docx or .pdf" in response.text


def test_quick_tailor_rejects_empty_description(client):
    test_client, _ = client
    response = test_client.post(
        "/quick",
        data={"description": "   "},
        files={"resume": ("resume.docx", FIXTURE_RESUME.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 400
    assert "Paste the job description" in response.text


def test_quick_tailor_end_to_end_redirects_to_job_detail_with_tailored_resume(client):
    test_client, SessionLocal = client

    response = test_client.post(
        "/quick",
        data={"description": JOB_DESCRIPTION, "company": "Acme", "title": "Senior Backend Engineer"},
        files={
            "resume": (
                "resume.docx",
                FIXTURE_RESUME.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail_url = response.headers["location"]
    assert detail_url.startswith("/jobs/")

    detail_response = test_client.get(detail_url)
    assert detail_response.status_code == 200
    assert "Tailored resume" in detail_response.text
    assert "Download DOCX" in detail_response.text

    from app.models.candidate import CandidateProfile
    from app.models.job import Job, JobStatus

    session = SessionLocal()
    assert session.query(CandidateProfile).filter_by(is_active=True).count() == 1
    job = session.query(Job).one()
    assert job.company == "Acme"
    assert job.status == JobStatus.READY_FOR_REVIEW  # generated despite matching.minimum_score=999
    session.close()


def test_quick_tailor_reuses_existing_job_on_duplicate_description(client):
    test_client, SessionLocal = client

    test_client.post(
        "/quick",
        data={"description": JOB_DESCRIPTION, "company": "Acme", "title": "Senior Backend Engineer"},
        files={"resume": ("resume.docx", FIXTURE_RESUME.read_bytes(), "application/octet-stream")},
    )
    test_client.post(
        "/quick",
        data={"description": JOB_DESCRIPTION, "company": "Acme", "title": "Senior Backend Engineer"},
        files={"resume": ("resume.docx", FIXTURE_RESUME.read_bytes(), "application/octet-stream")},
    )

    from app.models.job import Job

    session = SessionLocal()
    assert session.query(Job).count() == 1
    session.close()
