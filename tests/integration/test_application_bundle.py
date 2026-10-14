"""Tests for the Milestone 8 artifact-bundle writer: job/match/tailored-resume
data lands on disk under data/applications/... and one ApplicationArtifactSet
row per job tracks the paths, upserted (not duplicated) on repeat calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.applications.bundle import build_artifact_set
from app.core.database.base import Base
from app.models.application import ApplicationArtifactSet
from app.models.job import Job, JobStatus
from app.models.matching import MatchAnalysis
from app.models.resume import TailoredResume


@pytest.fixture()
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _job(session: Session) -> Job:
    job = Job(
        source="manual", source_url="https://example.com", company="Acme", title="Engineer",
        description="posting text", status=JobStatus.RESUME_GENERATED, fingerprint="fp", description_hash="dh",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_build_artifact_set_writes_expected_files(session):
    job = _job(session)
    session.add(MatchAnalysis(job_id=job.id, overall_score=85, technical_score=90))
    tailored_resume = TailoredResume(job_id=job.id, docx_path="x.docx", pdf_path="x.pdf")
    session.add(tailored_resume)
    session.commit()

    artifact_set = build_artifact_set(session, job, tailored_resume)

    job_json = json.loads(Path(artifact_set.job_json_path).read_text())
    assert job_json["company"] == "Acme"

    assert Path(artifact_set.job_description_path).read_text() == "posting text"

    match_json = json.loads(Path(artifact_set.match_analysis_path).read_text())
    assert match_json["overall_score"] == 85

    assert artifact_set.tailored_resume_docx_path == "x.docx"
    assert artifact_set.tailored_resume_pdf_path == "x.pdf"


def test_build_artifact_set_upserts_not_duplicates(session):
    job = _job(session)

    first = build_artifact_set(session, job, None)
    second = build_artifact_set(session, job, None)

    assert first.id == second.id
    assert session.query(ApplicationArtifactSet).filter_by(job_id=job.id).count() == 1
