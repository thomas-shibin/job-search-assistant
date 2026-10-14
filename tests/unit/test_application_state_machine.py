"""Tests for the Milestone 8 review/apply/outcome state machine: only the
allowed transitions succeed, every transition is recorded, and terminal
statuses can't be moved out of.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.applications.state_machine import (
    InvalidTransitionError,
    approve,
    archive,
    mark_applied,
    mark_ready_for_review,
    mark_rejected,
    skip,
    transition_job,
)
from app.core.database.base import Base
from app.modules.jobs.models import Job, JobStatus


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _job(session: Session, status: JobStatus) -> Job:
    job = Job(
        source="manual", source_url="https://example.com", company="Acme", title="Engineer",
        description="d", status=status, fingerprint="fp", description_hash="dh",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_full_happy_path_progression(session):
    job = _job(session, JobStatus.RESUME_GENERATED)

    mark_ready_for_review(session, job)
    assert job.status == JobStatus.READY_FOR_REVIEW

    approve(session, job)
    assert job.status == JobStatus.APPROVED

    from app.modules.applications.state_machine import mark_application_opened

    mark_application_opened(session, job)
    assert job.status == JobStatus.APPLICATION_OPENED

    mark_applied(session, job)
    assert job.status == JobStatus.APPLIED

    assert len(job.status_history) == 4


def test_skip_available_from_any_non_terminal_stage(session):
    job = _job(session, JobStatus.APPROVED)
    skip(session, job)
    assert job.status == JobStatus.SKIPPED


def test_cannot_skip_a_terminal_job(session):
    job = _job(session, JobStatus.SKIPPED)
    with pytest.raises(InvalidTransitionError):
        skip(session, job)


def test_cannot_jump_stages(session):
    job = _job(session, JobStatus.READY_FOR_REVIEW)
    with pytest.raises(InvalidTransitionError):
        mark_applied(session, job)  # must go through APPROVED, APPLICATION_OPENED first


def test_rejected_after_applied_is_allowed(session):
    job = _job(session, JobStatus.APPLIED)
    mark_rejected(session, job)
    assert job.status == JobStatus.REJECTED


def test_archive_available_from_applied(session):
    job = _job(session, JobStatus.APPLIED)
    archive(session, job)
    assert job.status == JobStatus.ARCHIVED


def test_transition_records_reason_and_actor(session):
    job = _job(session, JobStatus.RESUME_GENERATED)
    transition_job(session, job, JobStatus.READY_FOR_REVIEW, reason="custom reason", actor="tester")

    history = job.status_history[-1]
    assert history.reason == "custom reason"
    assert history.actor == "tester"
    assert history.from_status == JobStatus.RESUME_GENERATED
    assert history.to_status == JobStatus.READY_FOR_REVIEW
