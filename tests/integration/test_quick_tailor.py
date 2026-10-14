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
from app.core.config.schema import AIConfig, AppConfig, CandidateConfig, MatchingConfig
from app.core.database.base import Base
from app.core.database.session import get_session

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

    import app.core.config.current as current_config
    import main

    main.app.dependency_overrides[get_session] = override_get_session

    config = AppConfig(
        candidate=CandidateConfig(name="Jordan Casey", base_resume="x.docx"),
        matching=MatchingConfig(minimum_score=999),  # deliberately unreachable -- quick-tailor must ignore it
        ai=AIConfig(provider="mock"),
    )
    monkeypatch.setattr(current_config, "load_app_config", lambda session, path: config)

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

    from app.modules.resumes.models import CandidateProfile
    from app.modules.jobs.models import Job, JobStatus

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

    from app.modules.jobs.models import Job

    session = SessionLocal()
    assert session.query(Job).count() == 1
    session.close()


def _docx_upload():
    return {"resume": ("my resume.docx", FIXTURE_RESUME.read_bytes(),
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}


def test_my_resume_page_uploads_shows_and_downloads_the_base_resume(client):
    http, SessionLocal = client
    assert "No resume yet" in http.get("/resume").text

    response = http.post("/resume", files=_docx_upload(), follow_redirects=False)
    assert response.status_code == 303 and "Resume+uploaded+and+in+use" in response.headers["location"]

    page = http.get("/resume").text
    assert "What the app read from it" in page and "Version 1" in page
    assert "Reduced p95 API latency by 40%" in page and "Acme Cloud Systems" in page  # parsed from the fixture

    again = http.post("/resume", files=_docx_upload(), follow_redirects=False)
    assert "same+content" in again.headers["location"]  # identical file: no new version

    download = http.get("/resume/file")
    assert download.status_code == 200 and download.content == FIXTURE_RESUME.read_bytes()


def test_my_resume_page_rejects_unreadable_files(client):
    http, SessionLocal = client
    bad = http.post("/resume", files={"resume": ("resume.docx", b"not really a docx", "application/octet-stream")},
                    follow_redirects=False)
    assert "Couldn%27t+read+that+resume" in bad.headers["location"] or "Couldn't" in bad.headers["location"]
    wrong_type = http.post("/resume", files={"resume": ("resume.txt", b"text", "text/plain")}, follow_redirects=False)
    assert ".docx+or+.pdf" in wrong_type.headers["location"]
    from app.modules.resumes.models import CandidateProfile

    with SessionLocal() as session:
        assert session.query(CandidateProfile).count() == 0


def test_quick_tailor_uses_the_saved_resume_when_no_file_is_chosen(client):
    http, SessionLocal = client
    no_resume = http.post("/quick", data={"description": "Senior Backend Engineer with Java."})
    assert "Upload your resume" in no_resume.text

    http.post("/resume", files=_docx_upload())
    response = http.post("/quick", data={"description": "Senior Backend Engineer with Java and AWS."},
                         files={"resume": ("", b"", "application/octet-stream")}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/jobs/")
