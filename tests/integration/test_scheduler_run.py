"""End-to-end test of the Milestone 11 scheduler run: discover -> analyze ->
tailor across configured sources, with per-source failure isolation and the
run-lock. Sources are faked (no real HTTP) by monkeypatching the adapter
classes `app/scheduler/run.py` imports.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.config.schema import AIConfig, AppConfig, CandidateConfig, MatchingConfig, SearchProfile
from app.database.base import Base
from app.models.candidate import CandidateProfile, Employment, Skill
from app.models.job import Job, JobStatus
from app.models.scheduler import SchedulerRun, SourceRunResult
from app.sources.base import RawJob


@pytest.fixture()
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    # Real Windows toast notifications (via win10toast) block for their visible
    # duration and depend on an OS window session -- notification behavior itself
    # is covered by tests/unit/test_notifications.py, so keep it out of these
    # scheduler-orchestration tests.
    import app.scheduler.run as run_module

    monkeypatch.setattr(run_module, "_build_notification_provider", lambda: None)

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_candidate(session: Session) -> None:
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True)
    profile.skills = [Skill(name="Java")]
    profile.employments = [
        Employment(company="Prior Co", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True)
    ]
    session.add(profile)
    session.commit()


def _config(search_profiles) -> AppConfig:
    return AppConfig(
        candidate=CandidateConfig(name="Test", base_resume="x.docx"),
        search_profiles=search_profiles,
        matching=MatchingConfig(required_or_high_value_skills=["Java"], minimum_score=10),
        ai=AIConfig(provider="mock"),  # tailoring always needs *a* provider; mock is free/offline
    )


class _FakeSource:
    def __init__(self, jobs):
        self._jobs = jobs

    def discover_jobs(self, search_profile):
        return self._jobs


class _FailingSource:
    def discover_jobs(self, search_profile):
        raise RuntimeError("connection refused")


def test_run_once_discovers_analyzes_and_tailors_qualified_jobs(session, monkeypatch):
    import app.scheduler.run as run_module

    _seed_candidate(session)
    raw_job = RawJob(
        source="greenhouse", source_url="https://example.com/1", company="Acme",
        title="Senior Backend Engineer", description="Java role.",
    )
    monkeypatch.setattr(run_module, "GreenhouseSource", lambda board_token: _FakeSource([raw_job]))

    config = _config([SearchProfile(name="SP", address_profile="x", greenhouse_boards=["acme"])])

    summary = run_module.run_once(session, config)

    assert summary.jobs_discovered == 1
    assert summary.jobs_new == 1
    assert summary.jobs_qualified == 1
    assert summary.jobs_tailored == 1
    assert summary.source_failures == 0

    job = session.query(Job).one()
    assert job.status == JobStatus.READY_FOR_REVIEW
    assert job.search_profile_name == "SP"

    scheduler_run = session.query(SchedulerRun).one()
    assert scheduler_run.status == "COMPLETED"
    source_result = session.query(SourceRunResult).one()
    assert source_result.status == "OK"
    assert source_result.jobs_new == 1


def test_run_once_isolates_one_failing_source_from_others(session, monkeypatch):
    import app.scheduler.run as run_module

    _seed_candidate(session)
    good_job = RawJob(
        source="lever", source_url="https://example.com/2", company="Beta", title="Backend Engineer",
        description="Java role.",
    )
    monkeypatch.setattr(run_module, "GreenhouseSource", lambda board_token: _FailingSource())
    monkeypatch.setattr(run_module, "LeverSource", lambda company: _FakeSource([good_job]))

    config = _config(
        [SearchProfile(name="SP", address_profile="x", greenhouse_boards=["broken"], lever_companies=["beta"])]
    )

    summary = run_module.run_once(session, config)

    assert summary.source_failures == 1
    assert summary.jobs_new == 1  # the working Lever source still produced a job

    statuses = {r.source_name: r.status for r in session.query(SourceRunResult).all()}
    assert statuses["greenhouse:broken"] == "FAILED"
    assert statuses["lever:beta"] == "OK"

    scheduler_run = session.query(SchedulerRun).one()
    assert scheduler_run.status == "COMPLETED_WITH_ERRORS"


def test_run_once_returns_skipped_when_lock_held(session, tmp_path):
    import app.scheduler.run as run_module

    (tmp_path / ".scheduler.lock").write_text("123")

    summary = run_module.run_once(session, _config([]))

    assert summary.skipped is True
    assert session.query(SchedulerRun).count() == 0


def test_run_once_skips_disabled_search_profiles(session, monkeypatch):
    import app.scheduler.run as run_module

    monkeypatch.setattr(run_module, "GreenhouseSource", lambda board_token: pytest.fail("should not be called"))
    config = _config([SearchProfile(name="SP", address_profile="x", enabled=False, greenhouse_boards=["acme"])])

    summary = run_module.run_once(session, config)

    assert summary.jobs_discovered == 0


def test_run_once_uses_adzuna_source_when_enabled_with_credentials(session, monkeypatch):
    import app.scheduler.run as run_module

    _seed_candidate(session)
    raw_job = RawJob(
        source="adzuna", source_url="https://example.com/3", company="Gamma",
        title="Senior Backend Engineer", description="Java role.",
    )
    monkeypatch.setattr(
        run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key")
    )
    monkeypatch.setattr(
        run_module, "AdzunaSource", lambda app_id, app_key, country: _FakeSource([raw_job])
    )

    config = _config([SearchProfile(name="SP", address_profile="x", adzuna_enabled=True)])

    summary = run_module.run_once(session, config)

    assert summary.jobs_new == 1
    source_result = session.query(SourceRunResult).one()
    assert source_result.source_name == "adzuna"
    assert source_result.status == "OK"


def test_run_once_skips_adzuna_when_credentials_missing(session, monkeypatch):
    import app.scheduler.run as run_module

    _seed_candidate(session)
    monkeypatch.setattr(
        run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id=None, app_key=None)
    )
    monkeypatch.setattr(
        run_module, "AdzunaSource", lambda app_id, app_key, country: pytest.fail("should not be called")
    )

    config = _config([SearchProfile(name="SP", address_profile="x", adzuna_enabled=True)])

    summary = run_module.run_once(session, config)

    assert summary.jobs_discovered == 0
    assert session.query(SourceRunResult).count() == 0
