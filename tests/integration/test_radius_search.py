"""Radius search end to end: "within 60 km of Victoria" on the results page,
the JSON API and smart search. The job board is faked (it measures distance
itself, given `distance`); the job pool is filtered here by
app/modules/jobs/places.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.config.schema import AIConfig, AppConfig, CandidateConfig, GlobalSearchConfig
from app.core.database.base import Base
from app.core.database.session import get_session
from app.modules.jobs.catalog.store import seed_catalog
from app.modules.jobs.pool.models import PoolCompany, PoolJob
from app.modules.jobs.search import clear_cache
from app.modules.usage.models import ActivityEvent, ActivityKind

ASKED: list[dict] = []


class _FakeBoard:
    def __init__(self, app_id, app_key, country, **_):
        pass

    def search_page(self, *, what, where, page, per_page=50, area=(), what_or=None, filters=None):
        ASKED.append({"what": what, "where": where, "area": tuple(area), "filters": dict(filters or {})})
        return []


POOL = [  # (title, location, remote)
    ("Registered Nurse", "Victoria, BC", False),
    ("Registered Nurse", "Duncan, British Columbia, Canada", False),  # ~46 km from Victoria
    ("Registered Nurse", "Nanaimo, BC", False),  # ~92 km
    ("Registered Nurse", "Toronto, ON", False),
    ("Registered Nurse", "Remote Canada", True),
    ("Registered Nurse", "Remote - Ontario, Canada", True),  # not open to BC
]


@pytest.fixture()
def site(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_session():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    import app.core.config.current as current_config
    import app.modules.jobs.service as jobs_service
    import main

    config = AppConfig(candidate=CandidateConfig(name="Jordan Casey", base_resume="x.docx"),
                       search=GlobalSearchConfig(titles=["Backend Engineer"]), ai=AIConfig(provider="mock"))
    monkeypatch.setattr(current_config, "load_app_config", lambda session, path: config)
    monkeypatch.setattr(jobs_service, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key"))
    monkeypatch.setattr(jobs_service, "AdzunaSource", _FakeBoard)
    ASKED.clear()
    clear_cache()
    with SessionLocal() as s:
        seed_catalog(s)
        company = PoolCompany(provider="greenhouse", board="island-health", name="Island Health", enabled=True)
        s.add(company)
        s.flush()
        for n, (title, location, remote) in enumerate(POOL):
            s.add(PoolJob(company_id=company.id, provider="greenhouse", external_id=f"rn-{n}", company="Island Health",
                          title=title, location=location, country="ca", remote=remote, description="Care.",
                          url=f"https://example.com/{n}"))
        s.commit()
    main.app.dependency_overrides[get_session] = override_get_session
    yield SimpleNamespace(http=TestClient(main.app), db=SessionLocal)
    main.app.dependency_overrides.pop(get_session, None)


def _shown_locations(html: str) -> set[str]:
    return {location for _, location, _ in POOL if f"Island Health &middot; {location}" in html}


def test_jobs_within_the_radius_are_shown_and_the_board_is_asked_by_distance(site):
    page = site.http.get("/jobs", params={"q": "Registered Nurse", "l": "Victoria, BC", "radius": "60", "exact": 1})

    assert page.status_code == 200
    assert _shown_locations(page.text) == {"Victoria, BC", "Duncan, British Columbia, Canada", "Remote Canada"}
    assert ASKED == [{"what": "Registered Nurse", "where": "Victoria, BC", "area": (), "filters": {"distance": "60", "max_days_old": "30"}}]
    assert "Within 60 km" in page.text  # the chip
    assert '<option value="60" selected>Within 60 km</option>' in page.text
    assert "radius=60" in page.text  # paging and filters keep the distance


def test_a_wider_radius_reaches_further(site):
    page = site.http.get("/jobs", params={"q": "Registered Nurse", "l": "Victoria, BC", "radius": "100", "exact": 1})

    assert "Nanaimo, BC" in _shown_locations(page.text)
    assert "Toronto, ON" not in _shown_locations(page.text)


def test_without_a_radius_the_place_is_matched_by_name_as_before(site):
    page = site.http.get("/jobs", params={"q": "Registered Nurse", "l": "Victoria, BC", "exact": 1})

    assert _shown_locations(page.text) == {"Victoria, BC"}
    assert ASKED[0]["filters"] == {"max_days_old": "30"}  # only recent ads, never a radius
    assert "Within" not in page.text.split('class="search-chips"')[1].split("</ul>")[0]


def test_a_radius_around_a_province_searches_it_as_a_whole(site):
    page = site.http.get("/jobs", params={"q": "Registered Nurse", "l": "British Columbia", "radius": "60",
                                         "exact": 1})

    assert "Distance is measured from a town or city, so British Columbia was searched as a whole." in page.text
    assert "distance" not in ASKED[0]["filters"]


def test_a_new_distance_is_a_new_search_but_the_same_one_is_free(site):
    for radius in ("25", "25", "60"):
        site.http.get("/jobs", params={"q": "Registered Nurse", "l": "Victoria, BC", "radius": radius, "exact": 1})

    with site.db() as s:
        searches = s.query(ActivityEvent).filter(ActivityEvent.kind == ActivityKind.SEARCH).all()
    assert len(searches) == 2
    assert any("within 60 km" in e.summary for e in searches)


def test_smart_search_reads_the_distance(site):
    response = site.http.get("/jobs", params={"q": "registered nurse within 60 km of Victoria"},
                             follow_redirects=False)

    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["q"] == ["registered nurse"]
    assert params["l"] == ["Victoria, BC"]
    assert params["radius"] == ["60"]


def test_the_api_takes_a_radius(site):
    response = site.http.get("/api/jobs/search", params={"q": "Registered Nurse", "l": "Victoria, BC",
                                                         "radius": "60"})

    assert response.status_code == 200
    locations = {job["location"] for job in response.json()["results"]}
    assert locations == {"Victoria, BC", "Duncan, British Columbia, Canada", "Remote Canada"}
    assert ASKED[0]["filters"] == {"distance": "60", "max_days_old": "30"}


@pytest.mark.parametrize("radius", ["abc", "0", "-10"])
def test_a_bad_radius_is_ignored(site, radius):
    page = site.http.get("/jobs", params={"q": "Registered Nurse", "l": "Victoria, BC", "radius": radius,
                                         "exact": 1})

    assert page.status_code == 200
    assert _shown_locations(page.text) == {"Victoria, BC"}
