"""Retention clean-up: old jobs go (with everything derived from them), jobs you
acted on stay, and deleted postings aren't rediscovered as new."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.applications.folder import get_application_folder
from app.core.config.schema import AppConfig, CandidateConfig
from app.core.database.base import Base
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.usage.ai_calls import AICallLog
from app.modules.jobs.models import Job, JobStatus, JobStatusHistory, JobTombstone
from app.modules.matching.models import MatchAnalysis
from app.modules.resumes.tailoring_models import TailoredBulletProvenance, TailoredResume
from app.modules.alerts.models import WatchlistAlert
from app.modules.applications.retention import purge_old_jobs, was_deleted
from app.integrations.job_boards.base import RawJob

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _config(days=7, keep=True) -> AppConfig:
    config = AppConfig(candidate=CandidateConfig(name="J", base_resume="x.docx"))
    config.retention.delete_jobs_after_days = days
    config.retention.keep_jobs_you_acted_on = keep
    return config


def _raw(n: int) -> RawJob:
    return RawJob(source="adzuna", external_id=f"ext{n}", source_url=f"https://x/{n}", company=f"Co{n}",
                  title=f"Java Developer {n}", location="Toronto, Ontario", description=f"Role number {n} " * 20)


def _job(session, n: int, age_days: int, status=JobStatus.READY_FOR_REVIEW) -> Job:
    job = ingest_raw_job(session, _raw(n)).job
    job.date_discovered = NOW - timedelta(days=age_days)
    job.status = status
    session.add(MatchAnalysis(job_id=job.id, overall_score=80))
    tailored = TailoredResume(job_id=job.id, summary_text="s", skills_order=[], validation_status="PASSED")
    session.add(tailored)
    session.flush()
    session.add(TailoredBulletProvenance(tailored_resume_id=tailored.id, generated_text="b", validation_passed=True))
    session.add(AICallLog(provider="openai", operation="tailor_resume", job_id=job.id, estimated_cost=0.001))
    session.add(WatchlistAlert(job_id=job.id, job_title=job.title, job_company=job.company, alert_key=f"k{n}",
                               watches=["w"], status="sent"))
    (get_application_folder(job) / "tailored_resume.docx").write_bytes(b"docx")
    session.commit()
    return job


def test_old_jobs_are_deleted_with_everything_derived_from_them(session):
    old = _job(session, 1, age_days=10)
    old_id, folder = old.id, get_application_folder(old)
    fresh = _job(session, 2, age_days=2)

    report = purge_old_jobs(session, _config(days=7), now=NOW)

    assert report.deleted == 1 and report.folders_removed == 1
    assert session.get(Job, old_id) is None and session.get(Job, fresh.id) is not None
    assert not folder.exists()
    for model in (MatchAnalysis, TailoredResume, JobStatusHistory):
        assert session.query(model).filter_by(job_id=old_id).count() == 0
    assert session.query(TailoredBulletProvenance).count() == 1  # only the fresh job's
    # Kept, detached: AI spend and the alert log (with its snapshot).
    assert session.query(AICallLog).filter(AICallLog.job_id.is_(None)).count() == 1
    detached = session.query(WatchlistAlert).filter(WatchlistAlert.job_id.is_(None)).one()
    assert detached.job_title == "Java Developer 1"


def test_jobs_you_acted_on_are_kept_unless_told_otherwise(session):
    applied = _job(session, 1, age_days=30, status=JobStatus.APPLIED)
    _job(session, 2, age_days=30, status=JobStatus.SKIPPED)

    report = purge_old_jobs(session, _config(days=7), now=NOW)
    assert (report.deleted, report.kept_acted_on) == (1, 1)
    assert session.get(Job, applied.id) is not None

    assert purge_old_jobs(session, _config(days=7, keep=False), now=NOW).deleted == 1
    assert session.query(Job).count() == 0


def test_zero_days_and_dry_run_delete_nothing(session):
    _job(session, 1, age_days=100)
    assert purge_old_jobs(session, _config(days=0), now=NOW).deleted == 0
    dry = purge_old_jobs(session, _config(days=7), dry_run=True, now=NOW)
    assert dry.deleted == 1 and dry.deleted_titles == ["Java Developer 1 @ Co1"]
    assert session.query(Job).count() == 1


def test_a_deleted_posting_found_again_is_recognised(session):
    _job(session, 1, age_days=10)
    purge_old_jobs(session, _config(days=7), now=NOW)

    assert session.query(JobTombstone).count() == 1
    assert was_deleted(session, _raw(1))                                   # same posting
    assert was_deleted(session, _raw(1).model_copy(update={"external_id": "reposted"}))  # same job, new id
    assert not was_deleted(session, _raw(2))


def test_scheduled_scan_skips_deleted_postings_and_runs_the_cleanup(session, monkeypatch):
    from types import SimpleNamespace

    import app.workers.run as run_module

    _job(session, 1, age_days=10)
    purge_old_jobs(session, _config(days=7), now=NOW)

    class _Source:
        def discover_jobs(self, profile):
            return [_raw(1), _raw(3)]

    monkeypatch.setattr(run_module, "_configured_sources", lambda profile: [("fake", _Source())])
    monkeypatch.setattr(run_module, "_scheduled_search_source", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id=None, app_key=None))
    config = _config(days=7)
    config.search_profiles = [SimpleNamespace(name="p", enabled=True)]

    summary = run_module.run_once(session, config)

    assert summary.jobs_new == 1  # only _raw(3); the deleted posting isn't re-saved
    assert session.query(Job).filter_by(external_id="ext1").count() == 0
