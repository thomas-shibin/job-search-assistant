"""End-to-end tests for the Settings and Watchlist dashboard pages: settings
import from config.yaml on first visit, section saves (and rejected invalid
saves), watch CRUD, and the Telegram buttons (Telegram itself is faked).
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.config.store import load_app_config
from app.core.database.base import Base
from app.core.database.session import get_session
from app.models.settings import WatchlistEntry

YAML = {
    "candidate": {"name": "Test", "base_resume": "x.docx"},
    "address_profiles": {"bc": {"city": "Victoria", "province": "BC", "country": "Canada"}},
    "search_profiles": [{"name": "SP", "address_profile": "bc", "titles": ["Backend"]}],
    "watchlist": {"enabled": True, "watches": [{"name": "Dream", "companies": ["Shopify"]}]},
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_session():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(yaml.safe_dump(YAML), encoding="utf-8")
    import app.ui.settings_routes as settings_routes

    monkeypatch.setattr(settings_routes, "DEFAULT_CONFIG_PATH", yaml_path)
    import app.admin.routes as admin_routes

    monkeypatch.setattr(admin_routes, "DEFAULT_CONFIG_PATH", yaml_path)
    from app.core.config.store import import_yaml_config

    with TestSessionLocal() as session:  # the signed-in test member's own settings
        import_yaml_config(session, yaml_path)

    import main

    main.app.dependency_overrides[get_session] = override_get_session
    yield TestClient(main.app), TestSessionLocal, yaml_path
    main.app.dependency_overrides.clear()


def _config(SessionLocal, yaml_path):
    with SessionLocal() as session:
        return load_app_config(session, yaml_path)


def _as_admin():
    import main
    from app.auth.deps import current_user
    from tests.conftest import make_member

    admin = make_member(role="admin")
    main.app.dependency_overrides[current_user] = lambda: admin


def test_settings_page_renders_every_section(client):
    http, _, _ = client
    response = http.get("/settings")
    assert response.status_code == 200
    for section in ("search", "search_profiles", "matching", "candidate", "address_profiles", "resume"):
        assert f'action="/settings/{section}"' in response.text
    for platform_section in ("scheduler", "ai", "retention", "platform"):  # admins only, elsewhere
        assert f'action="/settings/{platform_section}"' not in response.text
    assert 'name="search_profiles.0.titles:lines"' in response.text


def test_saving_a_section_persists_to_db(client):
    http, SessionLocal, yaml_path = client
    http.get("/settings")

    response = http.post("/settings/matching", data={"matching.minimum_score": "61"}, follow_redirects=False)

    assert response.status_code == 303
    assert _config(SessionLocal, yaml_path).matching.minimum_score == 61


def test_members_cant_change_platform_settings(client):
    http, SessionLocal, yaml_path = client
    assert http.post("/settings/scheduler", data={"scheduler.interval_minutes": "1"}).status_code == 404
    assert http.post("/admin/settings/scheduler", data={"scheduler.interval_minutes": "1"}).status_code == 404
    assert http.get("/admin/settings").status_code == 404
    assert _config(SessionLocal, yaml_path).scheduler.interval_minutes == 15


def test_admin_saves_platform_settings(client):
    http, SessionLocal, yaml_path = client
    _as_admin()
    assert http.get("/admin/settings").status_code == 200

    response = http.post(
        "/admin/settings/scheduler",
        data={"scheduler.enabled:bool": ["false", "true"], "scheduler.interval_minutes": "60"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _config(SessionLocal, yaml_path).scheduler.interval_minutes == 60


def test_adding_a_search_profile_with_sources(client):
    http, SessionLocal, yaml_path = client
    http.get("/settings")

    http.post("/settings/search_profiles", data={
        "search_profiles.0.name": "SP", "search_profiles.0.address_profile": "bc",
        "search_profiles.1._new": "1", "search_profiles.1.name": "Remote", "search_profiles.1.address_profile": "bc",
        "search_profiles.1.greenhouse_boards:lines": "stripe\nshopify",
    })

    profiles = _config(SessionLocal, yaml_path).search_profiles
    assert [p.name for p in profiles] == ["SP", "Remote"]
    assert profiles[1].greenhouse_boards == ["stripe", "shopify"]


def test_invalid_save_shows_error_and_changes_nothing(client):
    http, SessionLocal, yaml_path = client
    _as_admin()
    http.get("/admin/settings")

    response = http.post("/admin/settings/scheduler", data={"scheduler.interval_minutes": "not a number"})

    assert response.status_code == 400
    assert "interval_minutes" in response.text
    assert _config(SessionLocal, yaml_path).scheduler.interval_minutes == 15


def test_watch_crud(client):
    http, SessionLocal, yaml_path = client
    assert "Dream" in http.get("/watchlist").text  # imported from YAML

    http.post("/watchlist/watches", data={"name": "Remote Java", "keywords": "Java\nKafka", "locations": "Remote",
                                         "enabled": "true"})
    with SessionLocal() as s:
        entry = s.query(WatchlistEntry).filter_by(name="Remote Java").one()
        entry_id = entry.id
    assert entry.keywords == ["Java", "Kafka"]

    http.post(f"/watchlist/watches/{entry_id}/toggle")
    assert [w.name for w in _config(SessionLocal, yaml_path).watchlist.watches] == ["Dream"]

    http.post(f"/watchlist/watches/{entry_id}", data={"name": "Remote Kotlin", "keywords": "Kotlin", "enabled": "true"})
    assert "Remote Kotlin" in [w.name for w in _config(SessionLocal, yaml_path).watchlist.watches]

    http.post(f"/watchlist/watches/{entry_id}/delete")
    with SessionLocal() as s:
        assert s.get(WatchlistEntry, entry_id) is None


def test_watch_without_criteria_is_rejected(client):
    http, SessionLocal, _ = client
    http.get("/watchlist")
    response = http.post("/watchlist/watches", data={"name": "Everything", "enabled": "true"})
    assert response.status_code == 400
    with SessionLocal() as s:
        assert s.query(WatchlistEntry).filter_by(name="Everything").count() == 0


def test_connect_telegram_links_this_users_chat_and_saving_keeps_it(client, monkeypatch):
    http, SessionLocal, yaml_path = client
    import app.services.telegram as telegram_service
    from app.models.user import User
    from app.notifications.telegram_commands import BotActions, reply_for

    with SessionLocal() as s:  # the bot only links verified members
        s.add(User(id=1, email="user1@example.com", first_name="Test", last_name="User1", role="member",
                   email_verified_at=__import__("datetime").datetime(2026, 1, 1)))
        s.commit()
    monkeypatch.setattr(telegram_service, "bot_username", lambda settings=None: "JobBot")
    monkeypatch.setattr(telegram_service, "TelegramSettings", lambda: type("S", (), {"bot_token": "t"})())

    response = http.post("/watchlist/telegram/connect", follow_redirects=False)
    link = response.headers["location"]
    assert link.startswith("https://t.me/JobBot?start=")
    code = link.split("start=", 1)[1]

    # The user taps Start in Telegram: the bot receives "/start <code>" from their chat.
    update = {"update_id": 1, "message": {"chat": {"id": 6348, "type": "private"},
                                          "date": __import__("time").time(), "text": f"/start {code}"}}
    chat_id, reply = reply_for(update, BotActions(SessionLocal, yaml_path))
    assert chat_id == "6348" and "Connected" in reply

    http.post("/settings/watchlist", data={
        "watchlist.enabled:bool": ["false", "true"], "watchlist.max_alerts_per_run": "5",
    })
    config = _config(SessionLocal, yaml_path)
    assert config.watchlist.telegram_chat_id == "6348"
    assert config.watchlist.max_alerts_per_run == 5


def test_job_search_section_uses_catalog_pickers(client):
    http, SessionLocal, yaml_path = client
    page = http.get("/settings").text
    assert 'data-catalog="location" data-name="search.locations:tags"' in page
    assert 'data-catalog="keyword" data-name="search.keywords:tags"' in page

    http.post("/settings/search", data={
        "search.titles:tags": ["", "Java Developer", "QA"],
        "search.locations:tags": ["", "Victoria, BC", "Vancouver, BC", "Remote Canada"],
        "search.keywords:tags": [""],
        "search.country": "ca", "search.adzuna_daily_limit": "250",
        "search.scan_in_background:bool": ["false", "true"],
    })

    search = _config(SessionLocal, yaml_path).search
    assert search.titles == ["Java Developer", "QA"]
    assert search.locations == ["Victoria, BC", "Vancouver, BC", "Remote Canada"]
    assert search.keywords == []
    from app.catalog.store import suggestions
    from app.models.catalog import TermKind

    with SessionLocal() as session:  # custom values are offered in every picker from now on
        assert {"Victoria, BC", "Remote Canada"} <= {t["label"] for t in suggestions(session, TermKind.LOCATION)}


def test_watch_pickers_save_one_value_per_tag(client):
    http, SessionLocal, _ = client
    http.get("/watchlist")
    http.post("/watchlist/watches", data={
        "name": "BC Java", "enabled": "true", "companies": [""], "titles": ["", "Java Developer"],
        "keywords": [""], "locations": ["", "Victoria, BC", "Vancouver, BC"], "exclude_keywords": [""],
    })
    with SessionLocal() as s:
        entry = s.query(WatchlistEntry).filter_by(name="BC Java").one()
    assert entry.locations == ["Victoria, BC", "Vancouver, BC"]
    assert entry.titles == ["Java Developer"]


def test_address_profiles_take_a_full_street_address(client):
    http, SessionLocal, yaml_path = client
    page = http.get("/settings").text
    assert 'name="address_profiles.0.street:opt"' in page and 'name="address_profiles.0.postal_code:opt"' in page

    http.post("/settings/address_profiles", data={
        "address_profiles.0._key": "bc", "address_profiles.0.street:opt": "1234 Main St",
        "address_profiles.0.unit:opt": "", "address_profiles.0.city": "Victoria", "address_profiles.0.province": "BC",
        "address_profiles.0.postal_code:opt": "V8W 1A1", "address_profiles.0.country": "Canada",
    })

    bc = _config(SessionLocal, yaml_path).address_profiles["bc"]
    assert (bc.street, bc.unit, bc.postal_code) == ("1234 Main St", None, "V8W 1A1")
    assert bc.one_line() == "1234 Main St, Victoria, BC V8W 1A1, Canada"


def test_watch_form_saves_the_advanced_filters(client):
    http, SessionLocal, yaml_path = client
    http.get("/watchlist")
    http.post("/watchlist/watches", data={
        "name": "BC contracts", "enabled": "true", "locations": ["", "Vancouver, BC"], "keywords": ["", "Java"],
        "titles": [""], "companies": [""], "exclude_companies": ["", "Staffing Co"], "exclude_keywords": [""],
        "min_score": "80", "min_salary": "90,000", "max_salary": "", "job_types": ["contract", "full_time"],
        "posted_within_days": "7", "search": ["false", "true"],
    })
    watch = next(w for w in _config(SessionLocal, yaml_path).watchlist.watches if w.name == "BC contracts")
    assert (watch.min_score, watch.min_salary, watch.max_salary, watch.posted_within_days) == (80, 90000, None, 7)
    assert watch.job_types == ["contract", "full_time"] and watch.exclude_companies == ["Staffing Co"]
    assert watch.search is True
    page = http.get("/watchlist").text
    assert "Score ≥ 80" in page and "Last 7 days" in page

    bad = http.post("/watchlist/watches", data={"name": "bad", "enabled": "true", "keywords": ["Java"],
                                                "min_salary": "200000", "max_salary": "100000"})
    assert bad.status_code == 400 and "minimum salary is higher than maximum salary" in bad.text


def test_watchlist_page_shows_the_alert_log(client):
    from app.models.job import Job, JobStatus
    from app.models.settings import WatchlistAlert

    http, SessionLocal, _ = client
    with SessionLocal() as s:
        job = Job(source="adzuna", source_url="https://x", company="Acme", title="Java Dev", description="d",
                  status=JobStatus.DISCOVERED, fingerprint="f", description_hash="d")
        s.add(job)
        s.commit()
        s.add_all([WatchlistAlert(job_id=job.id, alert_key="acme|java dev", watches=["Java"], score=88, status="sent"),
                   WatchlistAlert(job_id=job.id, alert_key="acme|java dev", watches=["Java"], score=88,
                                  status="duplicate", detail="already alerted for job #1 on Oct 09")])
        s.commit()
    page = http.get("/watchlist").text
    assert "Recent alerts" in page and "1 sent this week" in page and "1 duplicates skipped" in page
    assert "already alerted for job #1" in page
