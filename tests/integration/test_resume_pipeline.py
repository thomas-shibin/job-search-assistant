"""Tests for `app/resume/pipeline.py::generate_resume_bundle` -- the shared
tailor -> PDF -> artifact bundle -> READY_FOR_REVIEW flow used by both the
CLI's `generate-resume` and the scheduler's automated run.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.ai.mock import MockAIProvider
from app.config.schema import AppConfig, CandidateConfig
from app.database.base import Base
from app.documents.libreoffice import PdfConversionError
from app.models.candidate import AccomplishmentBullet, CandidateProfile, Employment, Skill
from app.models.job import Job, JobStatus
from app.resume.pipeline import generate_resume_bundle


@pytest.fixture()
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session) -> Job:
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", summary="Engineer.", is_active=True)
    profile.skills = [Skill(name="Java")]
    profile.employments = [
        Employment(
            company="Acme", title="Engineer", start_date=date(2020, 1, 1), is_current=True,
            bullets=[AccomplishmentBullet(text="Did the thing", order_index=0)],
        )
    ]
    session.add(profile)
    job = Job(
        source="manual", source_url="https://example.com", company="Acme", title="Engineer",
        description="d", status=JobStatus.QUALIFIED, fingerprint="fp", description_hash="dh",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_generate_resume_bundle_ends_with_ready_for_review_and_matching_snapshot(session, monkeypatch):
    # No LibreOffice in the test environment -- PDF conversion degrades gracefully.
    monkeypatch.setattr(
        "app.resume.pipeline.convert_to_pdf",
        lambda path: (_ for _ in ()).throw(PdfConversionError("no soffice")),
    )

    job = _seed(session)
    result = generate_resume_bundle(session, job, AppConfig(candidate=CandidateConfig(name="Jordan", base_resume="x.docx")), ai_provider=MockAIProvider())

    assert job.status == JobStatus.READY_FOR_REVIEW
    assert result.pdf_path is None
    assert result.pdf_error is not None

    job_json = json.loads(Path(result.artifact_set.job_json_path).read_text())
    assert job_json["status"] == "READY_FOR_REVIEW"  # snapshot reflects the final status, not RESUME_GENERATED
