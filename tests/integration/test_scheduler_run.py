"""End-to-end test of the Milestone 11 scheduler run: discover -> analyze ->
tailor across configured sources, with per-source failure isolation and the
run-lock. Sources are faked (no real HTTP) by monkeypatching the adapter
classes `app/workers/run.py` imports.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config.schema import AIConfig, AppConfig, CandidateConfig, MatchingConfig, SearchProfile
from app.core.database.base import Base
from app.integrations.job_boards.base import RawJob
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.jobs.models import Job, JobStatus
from app.modules.resumes.models import CandidateProfile, Employment, Skill
from app.workers.models import SchedulerRun, SourceRunResult


@pytest.fixture()
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    # Real Windows toast notifications (via win10toast) block for their visible
    # duration and depend on an OS window session -- notification behavior itself
    # is covered by tests/unit/test_notifications.py, so keep it out of these
    # scheduler-orchestration tests.
    import app.workers.run as run_module

    monkeypatch.setattr(run_module, "_build_notification_provider", lambda: None)
    # Never reach the real Adzuna API (or read real .env keys); Adzuna tests opt back in.
    monkeypatch.setattr(run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id=None, app_key=None))

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_candidate(session: Session) -> None:
    profile = CandidateProfile(raw_resume_path="x.pdf", source_hash="h", is_active=True)
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


class _FakeAdzuna:
    """Records each (what, where) query `GlobalSearchSource` sends."""

    def __init__(self, jobs):
        self._jobs = jobs
        self.queries = []
        self.calls = []

    def search_page(self, *, what, where, page, per_page, area=(), what_or=None, filters=None):
        self.queries.append((what, where))
        self.calls.append({"what": what, "where": where, "what_or": what_or, "filters": filters or {}})
        return self._jobs


class _FailingSource:
    def discover_jobs(self, search_profile):
        raise RuntimeError("connection refused")


def test_run_once_discovers_analyzes_and_tailors_qualified_jobs(session, monkeypatch):
    import app.workers.run as run_module

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
    import app.workers.run as run_module

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
    import app.workers.run as run_module

    (tmp_path / ".scheduler.lock").write_text("123")

    summary = run_module.run_once(session, _config([]))

    assert summary.skipped is True
    assert session.query(SchedulerRun).count() == 0


def test_run_once_skips_disabled_search_profiles(session, monkeypatch):
    import app.workers.run as run_module

    monkeypatch.setattr(run_module, "GreenhouseSource", lambda board_token: pytest.fail("should not be called"))
    config = _config([SearchProfile(name="SP", address_profile="x", enabled=False, greenhouse_boards=["acme"])])

    summary = run_module.run_once(session, config)

    assert summary.jobs_discovered == 0


def test_run_once_searches_every_global_title_and_location(session, monkeypatch):
    import app.workers.run as run_module
    from app.core.config.schema import GlobalSearchConfig

    _seed_candidate(session)
    raw_job = RawJob(
        external_id="a1", source="adzuna", source_url="https://example.com/3", company="Gamma",
        title="Senior Backend Engineer", description="Java role.",
    )
    fake = _FakeAdzuna([raw_job])
    monkeypatch.setattr(run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key"))
    monkeypatch.setattr(run_module, "AdzunaSource", lambda app_id, app_key, country: fake)

    config = _config([])
    config.search = GlobalSearchConfig(titles=["Backend", "Java"], locations=["Victoria, BC", "Remote"])
    config.scheduler.interval_minutes = 60  # hourly: all 4 combinations fit the daily Adzuna budget

    summary = run_module.run_once(session, config)

    assert sorted(fake.queries) == [
        ("Backend", "Victoria, BC"), ("Backend remote", None), ("Java", "Victoria, BC"), ("Java remote", None),
    ]
    assert summary.jobs_new == 1  # the same posting from 4 queries is ingested once
    source_result = session.query(SourceRunResult).one()
    assert source_result.source_name == "adzuna"
    assert source_result.status == "OK"


def test_run_once_skips_adzuna_when_credentials_missing(session, monkeypatch):
    import app.workers.run as run_module

    _seed_candidate(session)
    monkeypatch.setattr(
        run_module, "AdzunaSource", lambda app_id, app_key, country: pytest.fail("should not be called")
    )

    config = _config([SearchProfile(name="SP", address_profile="x")])

    summary = run_module.run_once(session, config)

    assert summary.jobs_discovered == 0
    assert session.query(SourceRunResult).count() == 0


def test_run_once_sends_watchlist_alerts_for_new_matching_jobs(session, monkeypatch):
    import app.workers.run as run_module
    from app.core.config.schema import Watch, WatchlistConfig
    from app.modules.alerts.watchlist import WatchlistAlerter

    _seed_candidate(session)
    raw_jobs = [
        RawJob(source="greenhouse", source_url="https://example.com/1", company="Acme",
               title="Senior Backend Engineer", description="Java role."),
        RawJob(source="greenhouse", source_url="https://example.com/2", company="Other Co",
               title="Senior Backend Engineer", description="Java role."),
    ]
    monkeypatch.setattr(run_module, "GreenhouseSource", lambda board_token: _FakeSource(raw_jobs))

    sent = []
    fake_client = SimpleNamespace(send_message=lambda chat_id, text: sent.append(text))
    watchlist = WatchlistConfig(enabled=True, watches=[Watch(name="Acme watch", companies=["acme"])])
    monkeypatch.setattr(
        run_module, "build_watchlist_alerter", lambda config, **kwargs: WatchlistAlerter(watchlist, fake_client, "42")
    )

    config = _config([SearchProfile(name="SP", address_profile="x", greenhouse_boards=["acme"])])
    run_module.run_once(session, config)
    assert len(sent) == 1
    assert "Acme watch" in sent[0] and "/100" in sent[0]

    # A second scan sees the same jobs as duplicates -- no repeat alert.
    run_module.run_once(session, config)
    assert len(sent) == 1


def test_watches_run_their_own_searches_alongside_the_job_search(session, monkeypatch):
    import app.workers.run as run_module
    from app.core.config.schema import GlobalSearchConfig, Watch

    _seed_candidate(session)
    raw_job = RawJob(source="adzuna", source_url="https://example.com/w", company="Acme",
                     title="Java Contractor", description="Java role.")
    fake = _FakeAdzuna([raw_job])
    monkeypatch.setattr(run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key"))
    monkeypatch.setattr(run_module, "AdzunaSource", lambda app_id, app_key, country: fake)

    config = _config([])
    config.search = GlobalSearchConfig(titles=["Backend"], locations=["Toronto, ON"])
    config.watchlist.watches = [Watch(name="BC contracts", locations=["Vancouver, BC"], keywords=["Java"],
                                      min_salary=90000, job_types=["contract"])]
    config.scheduler.interval_minutes = 60

    run_module.run_once(session, config)

    by_where = {call["where"]: call for call in fake.calls}
    assert set(by_where) == {"Toronto, ON", "Vancouver, BC"}
    assert by_where["Toronto, ON"]["filters"] == {"max_days_old": "30"}  # scans skip old ads too
    assert by_where["Vancouver, BC"]["what_or"] == "Java"
    assert by_where["Vancouver, BC"]["filters"] == {"salary_min": "90000", "contract": "1", "max_days_old": "30"}


def _pool_posting(session, external_id, title, location, first_seen):
    from datetime import datetime, timezone

    from app.modules.jobs.pool.models import PoolCompany, PoolJob

    company = session.query(PoolCompany).first()
    if company is None:
        company = PoolCompany(provider="greenhouse", board="acme", name="Acme", last_status="ok")
        session.add(company)
        session.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(PoolJob(company_id=company.id, provider="greenhouse", external_id=external_id, company="Acme",
                        title=title, location=location, country="ca", description=f"{title}, posting {external_id}.",
                        url=f"https://example.com/{external_id}", date_posted=now, first_seen_at=first_seen,
                        last_seen_at=now))
    session.commit()


def test_watches_alert_on_new_careers_site_jobs_without_a_job_board(session):
    """The job pool is checked on every scan for free: no Adzuna keys, still alerts."""
    from datetime import datetime, timedelta, timezone

    import app.workers.run as run_module
    from app.core.config.schema import Watch
    from app.modules.alerts.models import WatchlistAlert

    _seed_candidate(session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _pool_posting(session, "1", "Senior Accountant", "Calgary, AB", now - timedelta(hours=2))
    _pool_posting(session, "2", "Software Engineer", "Calgary, AB", now - timedelta(hours=2))  # another title
    _pool_posting(session, "3", "Accountant", "Toronto, ON", now - timedelta(hours=2))  # another place
    _pool_posting(session, "4", "Staff Accountant", "Calgary, AB", now - timedelta(days=3))  # within the first week
    _pool_posting(session, "7", "Lead Accountant", "Calgary, AB", now - timedelta(days=8))  # older than a week

    config = _config([])
    config.watchlist.enabled = True
    config.watchlist.email_alerts = "instant"
    config.watchlist.watches = [Watch(name="Calgary accounting", titles=["Accountant"], locations=["Calgary, AB"])]

    run_module.run_once(session, config)

    alerts = session.query(WatchlistAlert).all()
    assert sorted((a.job_title, a.status, a.channel) for a in alerts) == [
        ("Senior Accountant", "pending", "email"), ("Staff Accountant", "pending", "email")]
    # non-matches never become the member's
    assert sorted(j.title for j in session.query(Job)) == ["Senior Accountant", "Staff Accountant"]
    pool_result = session.query(SourceRunResult).filter_by(source_name="pool").one()
    assert (pool_result.jobs_discovered, pool_result.jobs_new) == (2, 2)

    # The next scan looks back only to this one: a posting from before it isn't news, a new one is.
    session.query(SchedulerRun).update({SchedulerRun.started_at: now - timedelta(minutes=15)})
    session.commit()
    _pool_posting(session, "5", "Accountant II", "Calgary, AB", now - timedelta(hours=1))
    _pool_posting(session, "6", "Junior Accountant", "Calgary, AB", now)
    run_module.run_once(session, config)
    assert [a.job_title for a in session.query(WatchlistAlert).order_by(WatchlistAlert.id)][2:] == ["Junior Accountant"]


def test_a_watch_added_since_the_last_scan_starts_with_the_past_week(session):
    from datetime import datetime, timedelta, timezone

    import app.workers.run as run_module
    from app.core.config.models import WatchlistEntry
    from app.core.config.schema import Watch
    from app.modules.alerts.models import WatchlistAlert

    _seed_candidate(session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(SchedulerRun(status="COMPLETED", started_at=now - timedelta(minutes=15)))
    session.add_all([WatchlistEntry(name="Old watch", titles=["Accountant"], created_at=now - timedelta(days=2)),
                     WatchlistEntry(name="New watch", titles=["Bookkeeper"], created_at=now - timedelta(minutes=5))])
    session.commit()
    _pool_posting(session, "1", "Accountant", "Calgary, AB", now - timedelta(days=2))
    _pool_posting(session, "2", "Bookkeeper", "Calgary, AB", now - timedelta(days=2))
    config = _config([])
    config.watchlist.enabled = True
    config.watchlist.email_alerts = "instant"
    config.watchlist.watches = [Watch(name="Old watch", titles=["Accountant"]),
                                Watch(name="New watch", titles=["Bookkeeper"])]

    run_module.run_once(session, config)

    # The old watch already had its look at the accountant job; the new one gets the week's bookkeeper job.
    assert [a.job_title for a in session.query(WatchlistAlert)] == ["Bookkeeper"]


def test_pool_alerts_still_apply_the_minimum_match_score(session):
    from datetime import datetime, timezone

    import app.workers.run as run_module
    from app.core.config.schema import Watch
    from app.modules.alerts.models import WatchlistAlert

    _seed_candidate(session)
    _pool_posting(session, "1", "Senior Accountant", "Calgary, AB", datetime.now(timezone.utc).replace(tzinfo=None))
    config = _config([])
    config.watchlist.enabled = True
    config.watchlist.email_alerts = "instant"
    config.watchlist.watches = [Watch(name="Strict", titles=["Accountant"], min_score=100)]

    run_module.run_once(session, config)

    assert session.query(Job).count() == 1  # fetched (the score needs the job) ...
    assert session.query(WatchlistAlert).count() == 0  # ... but scored below 100: no alert
