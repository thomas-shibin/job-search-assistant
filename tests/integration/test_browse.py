"""End-to-end test of the dashboard's "Search for jobs" section (merged into
`/` rather than a separate page): paginated live Adzuna results that are
never saved to the DB until the user explicitly tracks one (`/browse/track`),
as opposed to the automated scheduler's `adzuna_enabled` path which ingests
everything it finds. Adzuna itself is faked -- no real HTTP call is made.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.config.schema import AIConfig, AppConfig, CandidateConfig, MatchingConfig
from app.database.base import Base
from app.database.session import get_session
from app.sources.base import RawJob

RESULT_1 = RawJob(
    external_id="111",
    source="adzuna",
    source_url="https://www.adzuna.ca/land/ad/111",
    company="Acme Corp",
    title="Senior Backend Engineer",
    location="Victoria, BC",
    description="Java, Spring Boot, 5+ years.",
)


class _FakeAdzunaSource:
    def __init__(self, app_id, app_key, country):
        self.app_id = app_id
        self.app_key = app_key
        self.country = country

    def search_page(self, *, what, where, page):
        return [RESULT_1] if page == 1 else []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_session():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    import app.ui.routes as ui_routes
    import main

    main.app.dependency_overrides[get_session] = override_get_session

    config = AppConfig(
        candidate=CandidateConfig(name="Jordan Casey", base_resume="x.docx"),
        matching=MatchingConfig(minimum_score=999),  # deliberately unreachable
        ai=AIConfig(provider="mock"),
    )
    dummy_config_path = tmp_path / "config.yaml"
    dummy_config_path.write_text("placeholder -- load_config is mocked below", encoding="utf-8")
    monkeypatch.setattr(ui_routes, "DEFAULT_CONFIG_PATH", dummy_config_path)
    monkeypatch.setattr(ui_routes, "load_config", lambda path: config)
    monkeypatch.setattr(ui_routes, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key"))
    monkeypatch.setattr(ui_routes, "AdzunaSource", _FakeAdzunaSource)

    from app.models.candidate import CandidateProfile, Employment, Skill

    session = TestSessionLocal()
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True)
    profile.skills = [Skill(name="Java")]
    profile.employments = [
        Employment(company="Prior Co", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True)
    ]
    session.add(profile)
    session.commit()
    session.close()

    yield TestClient(main.app), TestSessionLocal
    main.app.dependency_overrides.clear()


def test_dashboard_without_search_params_does_not_call_adzuna(client):
    """A plain visit to `/` must not trigger a live search -- only submitting
    the search form (which always sends `what`, even blank) does.
    """
    test_client, _ = client

    response = test_client.get("/")

    assert response.status_code == 200
    assert "Search for jobs" in response.text
    assert "browse-results" not in response.text  # results list isn't even rendered
    assert "No results on this page." not in response.text


def test_dashboard_search_shows_results_without_saving_to_db(client):
    test_client, SessionLocal = client

    response = test_client.get("/", params={"what": "Backend Engineer", "page": 1})

    assert response.status_code == 200
    assert "Senior Backend Engineer" in response.text
    assert "Track &amp; tailor" in response.text
    assert "Next" in response.text  # page had a result -> has_next

    from app.models.job import Job

    session = SessionLocal()
    assert session.query(Job).count() == 0  # nothing saved just from searching
    session.close()


def test_dashboard_search_with_no_results_shows_empty_state_and_no_next(client):
    test_client, _ = client

    response = test_client.get("/", params={"what": "Backend Engineer", "page": 2})

    assert response.status_code == 200
    assert "No results on this page." in response.text
    assert "Next" not in response.text
    assert "Previous" in response.text  # page 2 -> previous still offered


def test_dashboard_search_missing_credentials_shows_setup_message(client, monkeypatch):
    import app.ui.routes as ui_routes

    monkeypatch.setattr(ui_routes, "AdzunaSettings", lambda: SimpleNamespace(app_id=None, app_key=None))
    test_client, _ = client

    response = test_client.get("/", params={"what": ""})

    assert response.status_code == 200
    assert "ADZUNA_APP_ID" in response.text


def test_browse_track_saves_job_and_redirects_to_detail(client):
    test_client, SessionLocal = client

    response = test_client.post(
        "/browse/track",
        data={
            "external_id": "111",
            "source_url": "https://www.adzuna.ca/land/ad/111",
            "company": "Acme Corp",
            "title": "Senior Backend Engineer",
            "location": "Victoria, BC",
            "description": "Java, Spring Boot, 5+ years.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail_url = response.headers["location"]
    assert detail_url.startswith("/jobs/")

    detail_response = test_client.get(detail_url)
    assert detail_response.status_code == 200

    from app.models.job import Job

    session = SessionLocal()
    job = session.query(Job).one()
    assert job.source == "adzuna"
    assert job.external_id == "111"
    session.close()


def test_browse_track_same_posting_twice_does_not_duplicate(client):
    test_client, SessionLocal = client
    payload = {
        "external_id": "111",
        "source_url": "https://www.adzuna.ca/land/ad/111",
        "company": "Acme Corp",
        "title": "Senior Backend Engineer",
        "location": "Victoria, BC",
        "description": "Java, Spring Boot, 5+ years.",
    }

    test_client.post("/browse/track", data=payload)
    test_client.post("/browse/track", data=payload)

    from app.models.job import Job

    session = SessionLocal()
    assert session.query(Job).count() == 1
    session.close()
