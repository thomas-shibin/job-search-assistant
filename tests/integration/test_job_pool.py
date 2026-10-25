"""The shared job pool: companies' careers-site feeds with full descriptions."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database.base import Base
from app.core.database.session import get_session
from app.core.tenancy import system_session, tenant_session
from app.integrations.job_boards.base import RawJob
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.auth.models import User
from app.modules.jobs.models import Job
from app.modules.jobs.pool import service as pool
from app.modules.jobs.pool.models import PoolCompany, PoolJob

MEMBER, ADMIN = 1, 2
FULL = "About the role. " + "You will build data pipelines in Python and SQL. " * 30


def _raw(source="greenhouse", ext="1", title="Data Analyst", location="Toronto, ON", company="acme", **payload):
    return RawJob(external_id=ext, source=source, source_url=f"https://careers.example/{ext}", company=company,
                  title=title, location=location, description=f"{FULL} Role: {title}.", date_posted=datetime(2026, 10, 1),
                  raw_payload=payload)


FEED = {
    ("greenhouse", "acme"): [
        _raw(ext="1", title="Data Analyst", location="Toronto, ON", company_name="Acme Inc."),
        _raw(ext="2", title="Senior Data Engineer", location="Remote - Canada", company_name="Acme Inc."),
        _raw(ext="3", title="Data Analyst", location="San Francisco, CA", company_name="Acme Inc."),
        _raw(ext="4", title="Office Manager", location="Vancouver, BC", company_name="Acme Inc."),
    ],
}


def fake_fetch(provider, board):
    if (provider, board) not in FEED:
        raise RuntimeError("404 Not Found")
    return FEED[(provider, board)]


def test_a_feeds_stated_workplace_outranks_its_remote_flag():
    # Ashby marks a hybrid job that allows remote days isRemote: true; its workplaceType says what it is
    hybrid = _raw(source="ashby", location="Toronto, Canada", isRemote=True, workplaceType="Hybrid")
    assert pool.classify(hybrid) == ("ca", False)
    assert pool.feed_workplace(hybrid) == "hybrid"
    assert pool.feed_workplace(_raw(source="ashby", workplaceType="OnSite")) == "onsite"
    assert pool.feed_workplace(_raw(source="lever", workplaceType="remote")) == "remote"
    assert pool.feed_workplace(_raw(source="lever", workplaceType="unspecified")) is None
    assert pool.feed_workplace(_raw()) is None


def test_where_a_posting_is():
    assert pool.classify(_raw(location="Toronto, ON")) == ("ca", False)
    assert pool.classify(_raw(location="San Francisco, CA")) == ("us", False)
    assert pool.classify(_raw(location="Remote")) == (None, True)
    assert pool.classify(_raw(location="Remote", offices=[{"name": "Canada", "location": None}])) == ("ca", True)
    assert pool.classify(_raw(source="lever", location="London", country="GB")) == ("gb", False)
    assert pool.classify(_raw(location="Remote - USA", offices=[{"name": "Toronto"}])) == ("us", True)
    assert pool.classify(_raw(source="ashby", location="KOHO (CAN)", isRemote=True)) == ("ca", True)
    assert pool.classify(_raw(source="ashby", location="HQ", workplaceType="Remote",
                              address={"postalAddress": {"addressCountry": "Canada"}})) == ("ca", True)
    assert pool.classify(RawJob(source="smartrecruiters", source_url="u", company="c", title="t", description="",
                                location="Lyon", raw_payload={"location": {"country": "fr", "remote": True}})) \
        == ("fr", True)
    assert pool.classify(RawJob(source="workable", source_url="u", company="c", title="t", description="",
                                location="Burnaby, British Columbia, Canada",
                                raw_payload={"locations": [{"countryCode": "CA"}]})) == ("ca", False)
    assert pool.classify(RawJob(source="workable", source_url="u", company="c", title="t", description="",
                                location="Athens, Greece", raw_payload={"locations": [{"countryCode": "GR"}],
                                                                        "telecommuting": True})) == ("gr", True)


@pytest.mark.parametrize("provider, value, expected", [
    ("greenhouse", "acme", ("greenhouse", "acme")),
    ("lever", "https://jobs.lever.co/Acme/123-abc", ("lever", "acme")),
    ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/55", ("greenhouse", "acme")),
    ("lever", "jobs.ashbyhq.com/acme", ("ashby", "acme")),
    ("ashby", "https://boards-api.greenhouse.io/v1/boards/acme/jobs", ("greenhouse", "acme")),
    ("greenhouse", "https://jobs.smartrecruiters.com/Ubisoft2", ("smartrecruiters", "ubisoft2")),
    ("greenhouse", "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings", ("smartrecruiters", "boschgroup")),
    ("greenhouse", "https://apply.workable.com/maple-reinders/", ("workable", "maple-reinders")),
    ("greenhouse", "https://apply.workable.com/maple-reinders/j/958B8634FD/", ("workable", "maple-reinders")),
    ("workable", "maple-reinders", ("workable", "maple-reinders")),
])
def test_board_from_name_or_url(provider, value, expected):
    assert pool.parse_board(provider, value) == expected


def test_bad_board_names_are_refused():
    with pytest.raises(ValueError, match="one Workable job"):
        pool.parse_board("workable", "https://apply.workable.com/j/958B8634FD")
    for value in ("", "a b", "../etc", "https://example.com/"):
        with pytest.raises(ValueError):
            pool.parse_board("greenhouse", value)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        s.add(User(id=MEMBER, email="mia@example.com", first_name="Mia", last_name="M", role="member",
                   email_verified_at=datetime(2026, 1, 1)))
        s.add(User(id=ADMIN, email="boss@example.com", first_name="Ada", last_name="A", role="admin",
                   email_verified_at=datetime(2026, 1, 1)))
        s.commit()
    return factory


def test_add_sync_and_search(factory):
    with factory() as s:
        company = pool.add_company(s, "greenhouse", "https://job-boards.greenhouse.io/acme", fetcher=fake_fetch)
        assert (company.name, company.job_count, company.canada_count) == ("Acme Inc.", 4, 3)
        with pytest.raises(ValueError, match="already in the list"):
            pool.add_company(s, "greenhouse", "acme", fetcher=fake_fetch)
        with pytest.raises(ValueError, match="Couldn't read"):
            pool.add_company(s, "lever", "nobody", fetcher=fake_fetch)

        def titles(**kw):
            args = {"titles": [], "locations": [], "keywords": [], "country": "ca", **kw}
            return sorted(j.title for j in pool.search_pool(s, **args).jobs)

        assert titles() == ["Data Analyst", "Office Manager", "Senior Data Engineer"]  # not the US one
        assert titles(country="us") == ["Data Analyst"]
        assert titles(titles=["data"]) == ["Data Analyst", "Senior Data Engineer"]
        assert titles(locations=["Toronto, ON"]) == ["Data Analyst"]
        assert titles(locations=["Remote Canada"]) == ["Senior Data Engineer"]
        assert titles(titles=["manager"], keywords=["pipelines"]) == ["Office Manager"]
        assert titles(keywords=["kubernetes"]) == []
        assert titles(titles=["100%_"]) == []  # LIKE wildcards are literal
        assert titles(titles=["at"]) == []  # whole words only: not the "at" in "Data"

        # The company stops listing two jobs: they leave the pool. A failed refresh keeps what's there.
        original = FEED[("greenhouse", "acme")]
        FEED[("greenhouse", "acme")] = original[:2]
        try:
            assert pool.sync_company(s, company, fetcher=fake_fetch)
            assert s.query(PoolJob).count() == 2
            company.board = "gone"
            assert not pool.sync_company(s, company, fetcher=fake_fetch)
            assert company.last_status == "error" and s.query(PoolJob).count() == 2
        finally:
            FEED[("greenhouse", "acme")] = original

        company.board = "acme"
        company.enabled = False
        s.commit()
        assert titles() == []  # paused companies don't show


def test_full_description_matches_company_and_title(factory):
    with factory() as s:
        pool.add_company(s, "greenhouse", "acme", fetcher=fake_fetch)
        assert pool.full_description_for(s, "ACME Inc", "data analyst").startswith(FULL)
        assert pool.full_description_for(s, "Acme", "Data Analyst!").startswith(FULL)
        assert pool.full_description_for(s, "Other Co", "Data Analyst") is None


# ---- through the web app ------------------------------------------------------------

_who: dict = {}


def _current(request: Request):
    with system_session(_who["factory"]) as s:
        user = s.get(User, _who["id"])
        s.expunge(user)
    request.state.user = user
    return user


@pytest.fixture()
def http(factory, monkeypatch):
    monkeypatch.setattr(pool, "fetch", fake_fetch)
    with factory() as s:
        pool.add_company(s, "greenhouse", "acme")

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    import main
    from app.modules.auth.deps import current_user

    _who.clear()
    _who.update(id=MEMBER, factory=factory)
    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = _current
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_search_shows_pool_jobs_even_without_the_job_board(http):
    page = http.get("/jobs?titles=Data&country=ca")
    assert page.status_code == 200
    assert "Senior Data Engineer" in page.text and "Company career site" in page.text
    assert "temporarily unavailable" not in page.text
    assert "Greenhouse" not in page.text


def test_pool_and_board_results_merge_without_duplicates(http, factory, monkeypatch):
    import app.modules.jobs.service as jobs_service
    from app.modules.jobs.search import SearchResults

    board = [RawJob(external_id="z1", source="adzuna", source_url="https://board/z1", company="Acme",
                    title="Data Analyst", location="Toronto", description=FULL[:500]),
             RawJob(external_id="z2", source="adzuna", source_url="https://board/z2", company="Beta",
                    title="Data Analyst", location="Toronto", description="Beta preview")]
    monkeypatch.setattr(jobs_service, "AdzunaSettings", lambda: type("S", (), {"app_id": "i", "app_key": "k"})())
    monkeypatch.setattr(jobs_service, "search_jobs", lambda *a, **k: SearchResults(jobs=list(board), requests=1))
    with tenant_session(factory, MEMBER) as s:
        result = jobs_service.search_for_page(s, titles=["Data Analyst"], locations=[], keywords=[], country="ca",
                                            page=1)
    urls = [c["raw"].source_url for c in result["cards"]]
    assert "https://board/z1" not in urls  # the pool has Acme's Data Analyst, with the full text
    assert "https://board/z2" in urls and "https://careers.example/1" in urls


def test_analyzing_uses_the_full_description(http, factory):
    with factory() as s:
        pool_id = s.query(PoolJob).filter_by(external_id="2").one().id
    http.post("/browse/analyze", data={"pool_id": str(pool_id), "source_url": "x", "company": "x", "title": "x",
                                       "description": "tampered"})
    # A job-board card for a job the pool has: analyzed with the pool's full text.
    http.post("/browse/analyze", data={"external_id": "z9", "source_url": "https://board/z9", "company": "Acme",
                                       "title": "Data Analyst", "description": "short preview..."})
    with tenant_session(factory, MEMBER) as s:
        jobs = {j.title: j for j in s.query(Job)}
        assert jobs["Senior Data Engineer"].source == "greenhouse"
        assert jobs["Senior Data Engineer"].description.startswith("About the role.")
        assert jobs["Data Analyst"].source == "adzuna" and jobs["Data Analyst"].description.startswith("About the role.")


def test_job_sources_page_is_admin_only(http):
    assert http.get("/admin/sources").status_code == 404
    assert http.post("/admin/sources/add", data={"provider": "lever", "board": "acme"}).status_code == 404


def test_admin_manages_job_sources(http, factory):
    _who["id"] = ADMIN
    page = http.get("/admin/sources")
    assert page.status_code == 200 and "Acme Inc." in page.text

    bad = http.post("/admin/sources/add", data={"provider": "lever", "board": "nobody"})
    assert "Couldn&#39;t read nobody" in bad.text or "read nobody" in bad.text
    with factory() as s:
        company_id = s.query(PoolCompany).one().id
    http.post(f"/admin/sources/{company_id}/toggle")
    with factory() as s:
        assert s.get(PoolCompany, company_id).enabled is False
    assert "refreshed" in http.post(f"/admin/sources/{company_id}/refresh").text
    http.post(f"/admin/sources/{company_id}/delete")
    with factory() as s:
        assert s.query(PoolCompany).count() == 0 and s.query(PoolJob).count() == 0


def test_greenhouse_descriptions_are_plain_text():
    from app.integrations.job_boards.greenhouse import GreenhouseSource

    raw = GreenhouseSource("acme")._to_raw_job(
        {"id": 1, "title": "T", "absolute_url": "u", "content": "&lt;p&gt;Pay &amp;amp; perks&lt;/p&gt;&lt;li&gt;SQL&lt;/li&gt;"})
    assert raw.description == "Pay & perks\nSQL"


def test_smartrecruiters_descriptions_are_kept_between_refreshes_and_unknown_names_refused(factory):
    asked_known = []

    def fetcher(provider, board, known=None):
        if board == "nobody":
            return []  # SmartRecruiters answers an unknown company with an empty list
        asked_known.append(set(known or ()))
        first = not asked_known[:-1]
        return [RawJob(external_id="11", source="smartrecruiters", source_url="https://jobs.example/11",
                       company="Acme Foods", title="Office Administrator", location="Mississauga, ON",
                       description="Run the office." if first else "", raw_payload={"location": {"country": "ca"}})]

    with factory() as s:
        with pytest.raises(ValueError, match="no jobs in Canada"):
            pool.add_company(s, "smartrecruiters", "nobody", fetcher=fetcher)
        company = pool.add_company(s, "smartrecruiters", "acme", fetcher=fetcher)
        assert company.canada_count == 1
        assert pool.sync_company(s, company, fetcher=fetcher)
        assert asked_known[-1] == {"11"}
        assert s.query(PoolJob).one().description == "Run the office."  # not fetched again, so kept


def test_admins_add_the_suggested_employers_in_one_go(factory, monkeypatch):
    from app.modules.jobs.pool import suggested

    monkeypatch.setattr(suggested, "SUGGESTED", (("workable", "maple", "Maple", 3), ("lever", "gone", "Gone", 1)))
    feeds = {("workable", "maple"): [_raw(source="workable", ext="m1", title="Receptionist", location="Burnaby, BC",
                                          company_name="Maple Builders")]}

    def fetcher(provider, board):
        if (provider, board) not in feeds:
            raise RuntimeError("404 Not Found")
        return feeds[(provider, board)]

    with factory() as s:
        assert [x[1] for x in pool.suggestions(s)] == ["maple", "gone"]
    assert pool.add_suggested(factory, fetcher=fetcher, pause=0) == {"added": 1, "failed": 1}
    with factory() as s:
        assert [c.label for c in s.query(PoolCompany)] == ["Maple Builders"]
        assert [x[1] for x in pool.suggestions(s)] == ["gone"]  # still offered: it couldn't be read



def test_the_job_sources_page_offers_the_suggested_employers(http, monkeypatch):
    _who["id"] = ADMIN
    started = []
    monkeypatch.setattr(pool, "start_adding_suggested", lambda factory: started.append(1) or True)

    page = http.get("/admin/sources")
    assert "Suggested Canadian employers" in page.text and "Maple Reinders" in page.text
    assert "Job boards" in page.text and "JOOBLE_API_KEY" in page.text

    done = http.post("/admin/sources/add-suggested")
    assert started == [1] and "in the background" in done.text


def test_job_seekers_only_see_current_postings(factory):
    from datetime import timedelta

    from app.modules.jobs import postings

    now = datetime.utcnow()
    feed = [
        _raw(ext="new", title="Office Administrator", company_name="Acme Inc."),
        _raw(ext="old", title="Receptionist", company_name="Acme Inc."),
        _raw(ext="twice", title="Office Administrator", company_name="Acme Inc."),  # same job, same city
        _raw(ext="ottawa", title="Office Administrator", location="Ottawa, ON", company_name="Acme Inc."),
    ]
    feed[0].date_posted = feed[2].date_posted = feed[3].date_posted = now - timedelta(days=3)
    feed[1].date_posted = now - timedelta(days=90)  # past the 60-day limit for company sites

    with factory() as s:
        company = pool.add_company(s, "greenhouse", "acme", fetcher=lambda p, b: feed)

        def found():
            return sorted((j.title, j.location) for j in pool.search_pool(s, ["administrator"], [], [], "ca").jobs)

        assert found() == [("Office Administrator", "Ottawa, ON"), ("Office Administrator", "Toronto, ON")]
        assert pool.search_pool(s, ["receptionist"], [], [], "ca").total == 0
        old_id = s.query(PoolJob).filter_by(external_id="old").one().id
        assert postings.get(s, f"p{old_id}") is None  # its page says it's no longer available

        # The feed can't be read for over a day: nothing it listed can be confirmed open.
        s.query(PoolJob).update({PoolJob.last_seen_at: now - timedelta(hours=25)})
        s.commit()
        assert found() == []
        assert pool.sync_company(s, company, fetcher=lambda p, b: feed)
        assert len(found()) == 2


def test_a_posting_abroad_is_not_canadian_because_of_the_companys_office():
    assert pool.classify(_raw(location="Portugal", offices=[{"name": "Canada", "location": None}])) == (None, False)
    assert pool.classify(_raw(location="London, UK", offices=[{"name": "Toronto"}])) == (None, False)
    assert pool.classify(_raw(location="London, Ontario")) == ("ca", False)
    assert pool.classify(_raw(location="Remote", offices=[{"name": "Canada"}])) == ("ca", True)
    # Ashby sends "location" as text, SmartRecruiters as an object.
    def raw(source, payload_location):
        return RawJob(source=source, source_url="u", company="c", title="t", description="", location="Laval, QC",
                      raw_payload={"location": payload_location})

    assert pool.classify(raw("ashby", "Laval, QC")) == ("ca", False)
    assert pool.classify(raw("smartrecruiters", {"country": "ca", "remote": True})) == ("ca", True)
