"""Job alerts by email end to end: scans queue watch matches, `send_due` emails
them (now, or as a daily summary), and the unsubscribe link turns them off
without signing in. Mail is captured, never sent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.config.current import user_config
from app.core.config.schema import Watch, WatchlistConfig
from app.core.config.store import save_section
from app.core.database.base import Base
from app.core.database.session import get_session
from app.core.tenancy import system_session, tenant_session
from app.modules.alerts import email_alerts
from app.modules.alerts.models import WatchlistAlert
from app.modules.alerts.watchlist import WatchlistAlerter, build_watchlist_alerter
from app.modules.auth.models import User
from app.modules.jobs.models import Job, JobStatus

TORONTO = timezone(timedelta(hours=-4))
WATCHES = WatchlistConfig(enabled=True, watches=[Watch(name="HR in BC", titles=["HR"])])


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import app.integrations.email as email

    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with system_session(factory) as s:
        s.add(User(id=1, email="jordan.casey@example.com", first_name="Jordan", last_name="Casey", role="member",
                   is_active=True, email_verified_at=datetime(2026, 1, 1), failed_logins=0))
        s.commit()
    outbox: list[EmailMessage] = []
    monkeypatch.setattr(email, "outbox", outbox)
    monkeypatch.setenv("PUBLIC_URL", "https://jobs.example.com")
    monkeypatch.setenv("MAIL_POSTAL_ADDRESS", "123 Main St, Victoria, BC V8W 1A1")
    return factory, outbox


def _settings(session, **watchlist):
    save_section(session, "watchlist", {**WatchlistConfig(enabled=True).model_dump(exclude={"watches"}), **watchlist})
    return user_config(session)


def _queue(session, *titles: str) -> list[Job]:
    jobs = []
    for title in titles:
        job = Job(source="adzuna", source_url=f"https://board.example.com/{len(title)}{title[:3]}", company="Acme",
                  title=title, location="Victoria, BC", description="People work.", status=JobStatus.DISCOVERED,
                  fingerprint=f"f-{title}", description_hash=f"d-{title}", salary_min=60000, salary_max=70000)
        session.add(job)
        session.commit()
        jobs.append(job)
    alerter = WatchlistAlerter(WATCHES, None, None, province_resolver=lambda job: "BC", session=session, email=True)
    for job in jobs:
        alerter.consider(job, score=84)
    return jobs


def _statuses(session) -> list[str]:
    return [a.status for a in session.query(WatchlistAlert).order_by(WatchlistAlert.id)]


def test_matches_are_queued_for_email_once_per_role(world):
    factory, _ = world
    with tenant_session(factory, 1) as s:
        _queue(s, "HR Generalist")
        _queue(s, "HR Generalist")  # the same role found again by a later scan

        rows = s.query(WatchlistAlert).order_by(WatchlistAlert.id).all()
        assert [(r.channel, r.status) for r in rows] == [("email", "pending"), ("email", "duplicate")]


def test_email_only_members_get_an_alerter_and_off_means_none(world):
    factory, _ = world
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="daily")
        config.watchlist.watches = WATCHES.watches
        assert build_watchlist_alerter(config, session=s) is not None  # no Telegram needed
        config.watchlist.email_alerts = "off"
        assert build_watchlist_alerter(config, session=s) is None


def test_instant_alerts_email_every_pending_match_once(world):
    factory, outbox = world
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="instant")
        jobs = _queue(s, "HR Generalist", "HR Manager")
        user = s.get(User, 1)

        assert email_alerts.send_due(s, user, config) == 2
        assert email_alerts.send_due(s, user, config) == 0  # nothing left to send

        assert len(outbox) == 1
        message = outbox[0]
        assert message["To"] == "jordan.casey@example.com"
        assert message["Subject"] == "2 new jobs match your watches"
        text = message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        for job in jobs:
            assert job.title in text and job.title in html
            assert f"https://jobs.example.com/jobs/{job.id}" in text
        assert "Acme · Victoria, BC · $60,000–$70,000 · match 84/100" in text
        assert "123 Main St, Victoria, BC V8W 1A1" in text  # CASL: the sender's mailing address
        unsubscribe = f"https://jobs.example.com/alerts/unsubscribe/1.{user_config(s).watchlist.email_unsubscribe_key}"
        assert unsubscribe in text and unsubscribe in html
        assert message["List-Unsubscribe"] == f"<{unsubscribe}>"
        assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert _statuses(s) == ["sent", "sent"]
        assert all(a.sent_at for a in s.query(WatchlistAlert))


def test_a_daily_summary_goes_out_once_a_day_from_eight(world):
    factory, outbox = world
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="daily")
        user = s.get(User, 1)
        _queue(s, "HR Generalist")
        sent = datetime.now(TORONTO)  # the send is stamped with the real time, so the test works from it
        assert email_alerts.send_due(s, user, config, now=sent) == 1  # the first one: straight away

        _queue(s, "HR Advisor")
        next_eight = email_alerts.period_start("daily", sent) + timedelta(days=1)
        assert email_alerts.send_due(s, user, config, now=max(sent, next_eight - timedelta(minutes=5))) == 0
        assert _statuses(s)[-1] == "pending"
        assert email_alerts.send_due(s, user, config, now=next_eight + timedelta(minutes=5)) == 1
        assert len(outbox) == 2
        assert outbox[1]["Subject"] == "New job: HR Advisor at Acme"


def test_a_failed_send_keeps_the_matches_for_next_time(world, monkeypatch):
    factory, outbox = world

    def down(*args, **kwargs):
        raise ConnectionError("mail server down")

    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="instant")
        _queue(s, "HR Generalist")
        monkeypatch.setattr(email_alerts, "send_email", down)
        assert email_alerts.send_due(s, s.get(User, 1), config) == 0
        assert _statuses(s) == ["pending"]


def test_old_matches_expire_instead_of_arriving_late(world):
    factory, outbox = world
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="instant")
        _queue(s, "HR Generalist")
        s.query(WatchlistAlert).update({WatchlistAlert.created_at: datetime(2026, 1, 1)})
        s.commit()

        assert email_alerts.send_due(s, s.get(User, 1), config) == 0
        assert outbox == []
        assert _statuses(s) == ["expired"]


def test_a_long_list_is_cut_short_with_a_count(world, monkeypatch):
    factory, outbox = world
    monkeypatch.setattr(email_alerts, "MAX_JOBS_PER_EMAIL", 2)
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="instant")
        _queue(s, "HR Generalist", "HR Manager", "HR Advisor", "HR Assistant")
        assert email_alerts.send_due(s, s.get(User, 1), config) == 4
        assert outbox[0]["Subject"] == "4 new jobs match your watches"  # all of them, not just those listed
        assert "And 2 more: https://jobs.example.com/watchlist#alerts" in outbox[0].get_body(("plain",)).get_content()
        assert _statuses(s) == ["sent"] * 4


def test_unverified_addresses_and_paused_alerts_get_nothing(world):
    factory, outbox = world
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="instant", enabled=False)
        _queue(s, "HR Generalist")
        assert email_alerts.send_due(s, s.get(User, 1), config) == 0
        config = _settings(s, email_alerts="instant")
        user = s.get(User, 1)
        user.email_verified_at = None
        assert email_alerts.send_due(s, user, config) == 0
        assert outbox == []


@pytest.fixture()
def http(world):
    import main

    factory, _ = world

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[get_session] = override
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(get_session, None)


def _token(factory) -> str:
    with tenant_session(factory, 1) as s:
        config = _settings(s, email_alerts="daily")
        _queue(s, "HR Generalist")
        return email_alerts.unsubscribe_token(s, s.get(User, 1), config)


def test_unsubscribe_asks_first_then_turns_email_off(world, http):
    factory, _ = world
    token = _token(factory)

    page = http.get(f"/alerts/unsubscribe/{token}")
    assert page.status_code == 200
    assert "Stop job alert emails?" in page.text and "j***********@example.com" in page.text
    with tenant_session(factory, 1) as s:
        assert user_config(s).watchlist.email_alerts == "daily"  # opening the link changes nothing

    done = http.post(f"/alerts/unsubscribe/{token}")
    assert done.status_code == 200 and "You're unsubscribed" in done.text
    with tenant_session(factory, 1) as s:
        assert user_config(s).watchlist.email_alerts == "off"
        assert _statuses(s) == ["cancelled"]  # nothing queued goes out afterwards
    assert "You're unsubscribed" in http.get(f"/alerts/unsubscribe/{token}").text


def test_mail_apps_one_click_unsubscribe(world, http):
    factory, _ = world
    token = _token(factory)

    # RFC 8058: the mail app POSTs this body, with no browser Origin and no sign-in.
    response = http.post(f"/alerts/unsubscribe/{token}", content="List-Unsubscribe=One-Click",
                         headers={"Content-Type": "application/x-www-form-urlencoded"})

    assert response.status_code == 200
    with tenant_session(factory, 1) as s:
        assert user_config(s).watchlist.email_alerts == "off"


@pytest.mark.parametrize("token", ["1.wrong-key-wrong-key-wrong", "2.abcdefghijklmnopqrstuvwx", "nonsense", "1."])
def test_bad_unsubscribe_links_change_nothing(world, http, token):
    factory, _ = world
    _token(factory)

    assert http.get(f"/alerts/unsubscribe/{token}").status_code == 404
    assert http.post(f"/alerts/unsubscribe/{token}").status_code == 404
    with tenant_session(factory, 1) as s:
        assert user_config(s).watchlist.email_alerts == "daily"
