"""A Word resume is always tailored in its own design: the original file is kept
in the database, a lost file is restored from it, and tailoring never silently
switches to a different layout."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
import app.resume.tailor as tailor_module
from app.config.schema import AIConfig, AppConfig, CandidateConfig
from app.database.base import Base
from app.ingestion.pipeline import ingest_raw_job
from app.resume.parser import import_resume
from app.resume.tailor import BaseResumeMissingError, tailor_resume
from app.sources.base import RawJob

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_resume.docx"


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    monkeypatch.setattr(tailor_module, "RESTORED_DIR", tmp_path / "restored")
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _job(session):
    job = ingest_raw_job(session, RawJob(source="manual", source_url="https://x/1", company="Acme", title="Engineer",
                                         description="Python and Java services. " * 10)).job
    session.commit()
    return job


def _config():
    return AppConfig(candidate=CandidateConfig(name="", base_resume="x.docx"), ai=AIConfig(provider="mock"))


def test_a_lost_word_resume_is_restored_from_the_database(session, tmp_path):
    resume = tmp_path / "resume.docx"
    shutil.copy(SAMPLE, resume)
    profile = import_resume(session, resume).profile
    assert profile.source_file == SAMPLE.read_bytes()
    resume.unlink()  # e.g. the app folder moved

    result = tailor_resume(session, _job(session), _config())

    restored = tmp_path / "restored" / f"user_{profile.user_id}" / f"resume_v{profile.version}_{profile.id}.docx"
    assert restored.read_bytes() == SAMPLE.read_bytes()
    # Tailored in place from the original, not laid out afresh: the base's styles are all there.
    assert Path(result.docx_path).stat().st_size > 10_000


def test_a_word_resume_that_cannot_be_found_is_never_reformatted(session, tmp_path):
    resume = tmp_path / "resume.docx"
    shutil.copy(SAMPLE, resume)
    profile = import_resume(session, resume).profile
    profile.source_file = None  # imported before files were kept
    session.commit()
    resume.unlink()

    with pytest.raises(BaseResumeMissingError, match="Upload it again"):
        tailor_resume(session, _job(session), _config())


def test_uploading_the_same_resume_again_replaces_a_lost_file(session, tmp_path):
    first = tmp_path / "first.docx"
    shutil.copy(SAMPLE, first)
    profile = import_resume(session, first).profile
    profile.source_file = None
    session.commit()
    first.unlink()

    again = tmp_path / "again.docx"
    shutil.copy(SAMPLE, again)
    result = import_resume(session, again)

    assert not result.created  # same content: still the same profile version
    assert result.profile.raw_resume_path == str(again)
    assert result.profile.source_file == SAMPLE.read_bytes()
