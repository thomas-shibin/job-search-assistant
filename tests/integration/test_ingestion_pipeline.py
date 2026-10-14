"""Integration test for the end-to-end manual-import -> normalize -> dedup ->
persist flow (spec section 6, 22). Exercises real SQLAlchemy models against a
temp-file sqlite DB rather than mocking the session, since the whole point of
this milestone is that these pieces work together correctly.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.database.base import Base
from app.ingestion.pipeline import ingest_raw_job
from app.models.job import JobStatus, JobStatusHistory
from app.sources.manual import import_manual_job


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


DESCRIPTION = """Title: Senior Backend Engineer
Company: Example Corp
Location: Vancouver, BC (Hybrid)

We are looking for a Senior Backend Engineer to join our platform team,
working with Java, Spring Boot, PostgreSQL, and Kubernetes to build
distributed systems that serve millions of requests per day.
"""


def test_first_import_creates_job_with_history_and_log_event(session, caplog):
    raw_job = import_manual_job(description=DESCRIPTION, url="https://example.com/jobs/42")

    import logging

    caplog.set_level(logging.INFO, logger="app.ingestion.pipeline")
    result = ingest_raw_job(session, raw_job)

    assert result.created is True
    assert result.job.id is not None
    assert result.job.status == JobStatus.DISCOVERED
    assert result.job.company == "Example Corp"
    assert result.job.title == "Senior Backend Engineer"
    assert result.job.remote_type == "hybrid"

    history_rows = (
        session.query(JobStatusHistory).filter_by(job_id=result.job.id).all()
    )
    assert len(history_rows) == 1
    assert history_rows[0].from_status is None
    assert history_rows[0].to_status == JobStatus.DISCOVERED

    assert any(record.getMessage() == "JOB_DISCOVERED" for record in caplog.records)


def test_reimporting_same_job_returns_existing_row_without_duplicate(session):
    raw_job = import_manual_job(description=DESCRIPTION, url="https://example.com/jobs/42")

    first = ingest_raw_job(session, raw_job)
    second = ingest_raw_job(session, raw_job)

    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id

    from app.models.job import Job

    assert session.query(Job).count() == 1


def test_near_duplicate_manual_paste_detected_end_to_end(session):
    first_raw = import_manual_job(description=DESCRIPTION, url="https://example.com/jobs/42")
    ingest_raw_job(session, first_raw)

    # Tweak the title slightly so the fingerprint (company+title+location) no
    # longer matches exactly, forcing the dedup check down to near-duplicate
    # description similarity rather than the cheaper fingerprint match.
    reworded = (
        DESCRIPTION.replace("Title: Senior Backend Engineer", "Title: Senior Backend Engineer II")
        .replace("platform team,", "platform team ,")
        .replace("millions of requests per day.", "millions of requests per day!")
    )
    second_raw = import_manual_job(description=reworded)  # no URL this time
    result = ingest_raw_job(session, second_raw)

    assert result.created is False
