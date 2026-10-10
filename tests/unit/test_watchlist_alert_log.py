"""Watchlist alerts are logged and never repeated for the same role."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.config.schema import Watch, WatchlistConfig
from app.database.base import Base
from app.models.job import Job, JobStatus
from app.models.settings import WatchlistAlert
from app.notifications.watchlist import WatchlistAlerter, alert_key


class _Telegram:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send_message(self, chat_id, text):
        if self.fail:
            raise ConnectionError("telegram down")
        self.sent.append(text)


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


CONFIG = WatchlistConfig(enabled=True, watches=[Watch(name="Java", keywords=["Java"], min_score=80)])


def _job(session, title, location="Mississauga, Peel region", n=[0]):
    n[0] += 1
    job = Job(source="adzuna", source_url=f"https://x/{n[0]}", company="Citigroup", title=title, location=location,
              description="Java services.", status=JobStatus.DISCOVERED, fingerprint=f"f{n[0]}", description_hash=f"d{n[0]}")
    session.add(job)
    session.commit()
    return job


def _alerter(session, telegram):
    return WatchlistAlerter(CONFIG, telegram, "42", province_resolver=lambda job: "ON", session=session)


def test_same_role_reposted_later_is_not_alerted_again(session):
    telegram = _Telegram()
    first = _alerter(session, telegram)                      # hourly scan 1
    first.consider(_job(session, "Senior Java Developer - Vice President"), score=88)

    second = _alerter(session, telegram)                     # hourly scan 2: reposted, different punctuation
    second.consider(_job(session, "Senior Java Developer, Vice President"), score=90)

    assert len(telegram.sent) == 1
    statuses = [(a.status, a.detail) for a in session.query(WatchlistAlert).order_by(WatchlistAlert.id)]
    assert statuses[0] == ("sent", None)
    assert statuses[1][0] == "duplicate" and "already alerted for job #" in statuses[1][1]


def test_same_role_twice_in_one_scan_alerts_once(session):
    telegram = _Telegram()
    alerter = _alerter(session, telegram)
    alerter.consider(_job(session, "Staff Software Engineer", "Greater Vancouver, British Columbia"), score=85)
    alerter.consider(_job(session, "Staff Software Engineer", "Vancouver, Greater Vancouver"), score=85)
    assert len(telegram.sent) == 1
    assert [a.status for a in session.query(WatchlistAlert)] == ["sent", "duplicate"]


def test_below_minimum_score_is_never_sent_or_logged(session):
    telegram = _Telegram()
    _alerter(session, telegram).consider(_job(session, "Java Developer"), score=79)
    assert telegram.sent == [] and session.query(WatchlistAlert).count() == 0


def test_failed_alerts_are_retried_once_on_the_next_scan(session):
    down = _Telegram(fail=True)
    _alerter(session, down).consider(_job(session, "Java Developer"), score=95)
    assert session.query(WatchlistAlert).one().status == "failed"

    up = _Telegram()
    assert _alerter(session, up).retry_failed() == 1
    assert len(up.sent) == 1
    assert session.query(WatchlistAlert).one().status == "sent"
    assert _alerter(session, up).retry_failed() == 0      # never sent twice


def test_alert_key_ignores_punctuation_and_case():
    a = Job(company="Citigroup", title="Senior Java Developer - Vice President")
    b = Job(company="CITIGROUP", title="Senior Java Developer, Vice President")
    assert alert_key(a) == alert_key(b)
