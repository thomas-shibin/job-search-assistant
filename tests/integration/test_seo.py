"""Being found in search engines: robots.txt, the sitemap, the browse pages,
canonical/noindex tags and the JobPosting data Google's job search reads."""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.database.base import Base
from app.core.database.session import get_session
from app.modules.jobs.pool.models import PoolCompany, PoolJob
from app.web import seo

pytestmark = pytest.mark.real_auth  # crawlers are signed-out visitors

JOBS = [
    ("Senior Data Analyst", "Toronto, ON", False, "full_time", 90000, 120000),
    ("Data Analyst", "Vancouver, BC", False, None, None, None),
    ("Software Engineer", "Remote - Canada", True, "full_time", None, None),
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

    import main

    monkeypatch.setenv("PUBLIC_URL", "https://hirlumo.com")
    seo._cache.clear()
    with SessionLocal() as s:
        company = PoolCompany(provider="greenhouse", board="northwind", name="Northwind", enabled=True)
        s.add(company)
        s.flush()
        for n, (title, location, remote, kind, low, high) in enumerate(JOBS):
            s.add(PoolJob(company_id=company.id, provider="greenhouse", external_id=f"j{n}", company="Northwind",
                          title=title, location=location, country="ca", remote=remote, employment_type=kind,
                          salary_min=low, salary_max=high, currency="CAD" if low else None,
                          description="About the role.\n\nYou will build <dashboards>.", url=f"https://example.com/{n}",
                          date_posted=datetime(2026, 10, 1)))
        s.commit()
        ids = [j.id for j in s.query(PoolJob).order_by(PoolJob.id)]
    main.app.dependency_overrides[get_session] = override_get_session
    yield SimpleNamespace(http=TestClient(main.app, base_url="https://hirlumo.com"), ids=ids)
    main.app.dependency_overrides.pop(get_session, None)
    seo._cache.clear()


def _ld(html: str) -> list[dict]:
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


@pytest.mark.parametrize("slug, expected", [
    ("data-analyst-jobs-in-toronto", ("data-analyst", "toronto", False)),
    ("data-analyst-jobs", ("data-analyst", None, False)),
    ("jobs-in-kitchener", (None, "kitchener", False)),
    ("remote-jobs", (None, None, True)),
    ("remote-devops-jobs", ("devops", None, True)),
    ("jobs", None), ("plumber-jobs", None), ("jobs-in-paris", None), ("remote-jobs-in-toronto", None), ("", None),
])
def test_browse_slugs(slug, expected):
    assert seo.parse_slug(slug) == expected
    if expected:
        assert seo.make_slug(*expected) == slug


def test_robots_keeps_accounts_out_and_points_at_the_sitemap(site):
    body = site.http.get("/robots.txt").text
    assert "Disallow: /admin" in body and "Disallow: /profile" in body and "Disallow: /jobs/posting/a" in body
    assert "Sitemap: https://hirlumo.com/sitemap.xml" in body


def test_sitemap_lists_public_pages_browse_pages_with_jobs_and_every_job(site):
    response = site.http.get("/sitemap.xml")
    assert response.headers["content-type"].startswith("application/xml")
    body = response.text
    for path in ["/", "/job-search", "/job-search/data-analyst-jobs-in-toronto", "/job-search/remote-jobs",
                 *(f"/jobs/posting/p{i}" for i in site.ids)]:
        assert f"<loc>https://hirlumo.com{path}</loc>" in body
    assert "/job-search/data-analyst-jobs-in-ottawa" not in body  # nothing there: not worth indexing
    assert "/admin" not in body and "/profile" not in body


def test_browse_page_lists_the_jobs_and_links_onwards(site):
    response = site.http.get("/job-search/data-analyst-jobs")
    html = response.text
    assert response.status_code == 200 and "X-Robots-Tag" not in response.headers
    assert "<h1>Data Analyst jobs in Canada</h1>" in html and "Senior Data Analyst" in html
    assert "Software Engineer" not in html.split("<h1>")[1].split("browse-pitch")[0]
    assert '<link rel="canonical" href="https://hirlumo.com/job-search/data-analyst-jobs">' in html
    assert "/job-search/data-analyst-jobs-in-toronto" in html  # the same role in other cities
    assert "2 open Data Analyst jobs in Canada at Northwind" in html  # the meta description


def test_an_empty_browse_page_is_kept_out_of_the_index(site):
    response = site.http.get("/job-search/recruiter-jobs-in-halifax")
    assert response.status_code == 200 and response.headers["X-Robots-Tag"] == "noindex"
    assert "No open roles here right now" in response.text


def test_unknown_or_uppercase_slugs_redirect(site):
    assert site.http.get("/job-search/plumber-jobs", follow_redirects=False).headers["location"] == "/job-search"
    moved = site.http.get("/job-search/Remote-Jobs", follow_redirects=False)
    assert moved.status_code == 301 and moved.headers["location"] == "/job-search/remote-jobs"


def test_browse_index_links_every_role_and_city(site):
    html = site.http.get("/job-search").text
    assert "/job-search/software-engineer-jobs" in html and "/job-search/jobs-in-winnipeg" in html
    assert "/job-search/remote-jobs" in html


def test_job_page_has_jobposting_data(site):
    html = site.http.get(f"/jobs/posting/p{site.ids[0]}").text
    job, = _ld(html)
    assert job["@type"] == "JobPosting" and job["title"] == "Senior Data Analyst"
    assert job["hiringOrganization"]["name"] == "Northwind" and job["datePosted"] == "2026-10-01"
    assert job["employmentType"] == "FULL_TIME" and job["directApply"] is False
    assert job["jobLocation"][0]["address"] == {"@type": "PostalAddress", "addressLocality": "Toronto",
                                                "addressRegion": "ON", "addressCountry": "CA"}
    assert job["baseSalary"]["value"] == {"@type": "QuantitativeValue", "unitText": "YEAR", "minValue": 90000,
                                          "maxValue": 120000}
    assert job["description"] == "<p>About the role.</p><p>You will build &lt;dashboards&gt;.</p>"
    assert "</script>" not in json.dumps(job) and f'href="https://hirlumo.com/jobs/posting/p{site.ids[0]}"' in html


def test_remote_job_is_marked_as_telecommute_in_canada(site):
    job, = _ld(site.http.get(f"/jobs/posting/p{site.ids[2]}").text)
    assert job["jobLocationType"] == "TELECOMMUTE"
    assert job["applicantLocationRequirements"] == {"@type": "Country", "name": "Canada"}


def test_landing_page_names_the_site_and_answers_questions(site, monkeypatch):
    monkeypatch.setenv("GOOGLE_SITE_VERIFICATION", "abc123")
    html = site.http.get("/").text
    kinds = {item["@type"] for item in _ld(html)}
    assert kinds == {"WebSite", "Organization", "FAQPage"}
    assert '<meta name="google-site-verification" content="abc123">' in html
    assert "Is Hirlumo free?" in html and "/job-search/remote-jobs" in html
    assert '<link rel="canonical" href="https://hirlumo.com/">' in html


@pytest.mark.parametrize("path", ["/login", "/jobs?q=data", "/companies?q=north"])
def test_private_and_search_pages_are_noindex(site, path):
    html = site.http.get(path).text
    assert '<meta name="robots" content="noindex, follow">' in html and 'rel="canonical"' not in html


def test_versioned_static_files_are_cached_for_a_year(site):
    from app.web.templating import static_url

    assert "immutable" in site.http.get(static_url("style.css")).headers["cache-control"]
    assert "cache-control" not in site.http.get("/static/style.css").headers
