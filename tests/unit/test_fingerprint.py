from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.database.base import Base
from app.ingestion.fingerprint import compute_fingerprint, find_duplicate
from app.ingestion.normalize import normalize_raw_job
from app.ingestion.pipeline import ingest_raw_job
from app.integrations.job_boards.base import RawJob


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _raw_job(**overrides) -> RawJob:
    defaults = dict(
        external_id=None,
        source="manual",
        source_url="https://example.com/job/1",
        company="Acme Corp",
        title="Senior Backend Engineer",
        location="Vancouver, BC",
        description=(
            "We are looking for a Senior Backend Engineer to join our platform team. "
            "You will work with Java, Spring Boot, and Kubernetes to build distributed systems."
        ),
        date_posted=None,
        raw_payload={},
    )
    defaults.update(overrides)
    return RawJob(**defaults)


def test_fingerprint_stable_across_case_and_whitespace():
    fp1 = compute_fingerprint("Acme Corp", "Senior Backend Engineer", "Vancouver, BC")
    fp2 = compute_fingerprint("  ACME CORP  ", "senior backend engineer", "vancouver, bc")
    assert fp1 == fp2


def test_duplicate_detected_by_same_source_and_external_id(session):
    first = _raw_job(external_id="job-123", source="greenhouse")
    ingest_raw_job(session, first)

    second = _raw_job(
        external_id="job-123",
        source="greenhouse",
        title="Senior Backend Engineer (Updated)",
        description="A totally different description now.",
    )
    result = ingest_raw_job(session, second)

    assert result.created is False


def test_duplicate_detected_by_fingerprint_across_different_sources(session):
    first = _raw_job(source="manual", external_id=None)
    ingest_raw_job(session, first)

    second = _raw_job(source="greenhouse", external_id="gh-999")
    result = ingest_raw_job(session, second)

    assert result.created is False


def test_near_duplicate_description_detected(session):
    first = _raw_job(description="We are hiring a Senior Backend Engineer to build APIs with Java and Spring Boot.")
    ingest_raw_job(session, first)

    second = _raw_job(
        title="Senior Backend Engineer II",
        description="We are hiring a  Senior Backend Engineer  to build APIs with Java and Spring Boot!",
    )
    result = ingest_raw_job(session, second)

    assert result.created is False


def test_different_jobs_at_same_company_not_flagged_duplicate(session):
    first = _raw_job(title="Senior Backend Engineer", description="Backend role focused on Java and Kubernetes.")
    ingest_raw_job(session, first)

    second = _raw_job(
        title="Senior Frontend Engineer",
        description="Frontend role focused on React, TypeScript, and accessibility.",
    )
    result = ingest_raw_job(session, second)

    assert result.created is True


def test_find_duplicate_returns_none_for_first_occurrence(session):
    raw = _raw_job()
    normalized = normalize_raw_job(raw)
    assert find_duplicate(session, raw, normalized) is None
