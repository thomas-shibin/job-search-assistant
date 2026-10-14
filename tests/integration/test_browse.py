"""End-to-end test of the landing page's job search: prefilled from the global
`search` settings and run on landing across every title x location, shown as
cards, never saved to the DB until the user analyzes one (`/browse/analyze`,
AI fit via the mock provider here) or tracks it (`/browse/track`).
Adzuna itself is faked -- no real HTTP call is made.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.config.schema import AIConfig, AppConfig, CandidateConfig, GlobalSearchConfig, MatchingConfig
from app.core.database.base import Base
from app.core.database.session import get_session
from app.services.search import clear_cache
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


QUERIES: list[tuple] = []
DETAILS: list[dict] = []

_TRACK_PAYLOAD = {
    "external_id": "111",
    "source_url": "https://www.adzuna.ca/land/ad/111",
    "company": "Acme Corp",
    "title": "Senior Backend Engineer",
    "location": "Victoria, BC",
    "description": "Java, Spring Boot, 5+ years.",
}


class _FakeAdzunaSource:
    def __init__(self, app_id, app_key, country):
        self.app_id = app_id
        self.app_key = app_key
        self._country = country

    def search_page(self, *, what, where, page, per_page=50, area=(), what_or=None):
        QUERIES.append((what, where, self._country))
        DETAILS.append({"what": what, "where": where, "area": tuple(area), "what_or": what_or})
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
        search=GlobalSearchConfig(titles=["Backend Engineer", "Java Developer"], locations=["Victoria, BC"]),
        matching=MatchingConfig(minimum_score=999),  # deliberately unreachable
        ai=AIConfig(provider="mock"),
    )
    QUERIES.clear()
    DETAILS.clear()
    clear_cache()
    dummy_config_path = tmp_path / "config.yaml"
    dummy_config_path.write_text("placeholder -- load_app_config is mocked below", encoding="utf-8")
    monkeypatch.setattr(ui_routes, "DEFAULT_CONFIG_PATH", dummy_config_path)
    monkeypatch.setattr(ui_routes, "load_app_config", lambda session, path: config)
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


def test_landing_page_prefills_saved_search_and_runs_it(client):
    test_client, SessionLocal = client

    response = test_client.get("/jobs")

    assert response.status_code == 200
    # Prefilled in the search boxes -- "Victoria, BC" stays whole.
    assert 'name="q" value="Backend Engineer, Java Developer"' in response.text
    assert 'name="l" value="Victoria, BC"' in response.text
    assert sorted(QUERIES) == [("Backend Engineer", "Victoria, BC", "ca"), ("Java Developer", "Victoria, BC", "ca")]
    assert "Senior Backend Engineer" in response.text  # one card -- the same posting from both queries
    assert response.text.count('class="result-card') == 1
    assert "Analyze match" in response.text and "Apply" in response.text

    from app.models.job import Job

    session = SessionLocal()
    assert session.query(Job).count() == 0  # nothing saved just from searching
    session.close()


def test_blank_search_boxes_search_all_jobs(client):
    test_client, _ = client

    test_client.get("/jobs", params={"titles": "", "locations": "", "country": "gb"})

    assert QUERIES == [(None, None, "gb")]


def test_comma_separated_locations_keep_region_codes(client):
    test_client, _ = client

    test_client.get("/jobs", params={"titles": "Backend", "locations": "Victoria, BC, Vancouver, BC"})

    assert sorted(q[1] for q in QUERIES) == ["Vancouver, BC", "Victoria, BC"]


def test_picker_values_arrive_one_per_field(client):
    test_client, _ = client

    test_client.get("/jobs", params=[("titles", ""), ("titles", "Backend"), ("locations", ""),
                                 ("locations", "Victoria, BC"), ("locations", "Toronto, Ontario")])

    assert sorted(q[1] for q in QUERIES) == ["Toronto, Ontario", "Victoria, BC"]


def test_keywords_narrow_every_query_without_extra_requests(client):
    test_client, _ = client

    test_client.get("/jobs", params=[("titles", "Backend"), ("locations", "Victoria, BC"),
                                 ("keywords", "Kafka"), ("keywords", "Spring Boot")])

    assert len(DETAILS) == 1
    assert DETAILS[0]["what_or"] == "Kafka Spring Boot"


def test_catalog_locations_are_searched_by_exact_path(client):
    test_client, SessionLocal = client
    from app.catalog.store import Term, upsert_terms
    from app.models.catalog import TermKind, TermSource

    with SessionLocal() as session:
        upsert_terms(session, TermKind.LOCATION, [
            Term("Victoria, BC", geo_path=["Canada", "British Columbia", "Capital", "Victoria"]),
        ], TermSource.SCAN)

    test_client.get("/jobs", params=[("titles", "Backend"), ("locations", "victoria bc"), ("locations", "Remote")])

    by_where = {d["what"]: d for d in DETAILS}
    assert by_where["Backend"]["area"] == ("Canada", "British Columbia", "Capital", "Victoria")
    assert by_where["Backend"]["where"] is None
    assert by_where["Backend remote"]["area"] == () and by_where["Backend remote"]["where"] is None


def test_searched_custom_terms_join_the_users_own_catalog(client):
    test_client, SessionLocal = client
    from app.models.catalog import SearchTerm, UserSearchTerm

    test_client.get("/jobs", params=[("titles", "Rust Wizard"), ("locations", "Tofino, BC")])

    with SessionLocal() as session:
        own = {(t.kind, t.label, t.user_id) for t in session.query(UserSearchTerm)}
        shared = {t.label for t in session.query(SearchTerm)}
    assert ("title", "Rust Wizard", 1) in own
    assert ("location", "Tofino, BC", 1) in own
    assert "Rust Wizard" not in shared  # never offered to anyone else
    assert [t["label"] for t in test_client.get("/api/terms/title").json()] == ["Rust Wizard"]


def test_dashboard_search_with_no_results_shows_empty_state_and_no_next(client):
    test_client, _ = client

    response = test_client.get("/jobs", params={"titles": "Backend Engineer", "page": 2})

    assert response.status_code == 200
    assert "No jobs found. Try adjusting your search filters." in response.text
    assert "Next" not in response.text
    assert "Previous" in response.text  # page 2 -> previous still offered


def test_save_as_default_updates_global_search(client, monkeypatch):
    import app.ui.routes as ui_routes

    saved = {}
    monkeypatch.setattr(ui_routes, "save_section", lambda session, key, value: saved.update({key: value}))
    test_client, _ = client

    test_client.post("/search/defaults", data={"titles": "Go Developer", "locations": "Toronto, ON, Remote", "country": "ca"})

    assert saved["search"]["titles"] == ["Go Developer"]
    assert saved["search"]["locations"] == ["Toronto, ON", "Remote"]

    test_client.post("/search/defaults", data={
        "titles": ["", "Go Developer", "Rust Developer"], "locations": ["", "Victoria, BC"], "keywords": ["", "Kafka"],
        "country": "ca",
    })
    assert saved["search"]["titles"] == ["Go Developer", "Rust Developer"]
    assert saved["search"]["locations"] == ["Victoria, BC"]
    assert saved["search"]["keywords"] == ["Kafka"]


def test_analyze_card_saves_job_and_shows_ai_fit(client):
    test_client, SessionLocal = client

    response = test_client.post("/browse/analyze", data=_TRACK_PAYLOAD, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("#fit")
    detail = test_client.get(response.headers["location"])
    assert "Match analysis" in detail.text
    assert "Re-run analysis" in detail.text

    from app.models.matching import JobFitAssessment

    session = SessionLocal()
    assessment = session.query(JobFitAssessment).one()
    assert assessment.provider == "mock"
    assert assessment.summary.startswith("Mock assessment") and 0 <= assessment.fit_score <= 100
    session.close()

    # The card for an analyzed posting now links to its analysis instead.
    landing = test_client.get("/jobs")
    assert "view analysis" in landing.text


def test_analyze_card_twice_reuses_the_cached_assessment(client):
    test_client, SessionLocal = client

    test_client.post("/browse/analyze", data=_TRACK_PAYLOAD)
    test_client.post("/browse/analyze", data=_TRACK_PAYLOAD)

    from app.models.matching import JobFitAssessment

    session = SessionLocal()
    assert session.query(JobFitAssessment).count() == 1
    session.close()


def test_analyze_card_without_resume_explains_what_to_do(client):
    test_client, SessionLocal = client
    from app.models.candidate import CandidateProfile

    session = SessionLocal()
    session.query(CandidateProfile).update({"is_active": False})
    session.commit()
    session.close()

    response = test_client.post("/browse/analyze", data=_TRACK_PAYLOAD)

    assert "No resume yet" in response.text
    assert "Upload your resume first" in response.text


def test_dashboard_search_missing_credentials_shows_neutral_message(client, monkeypatch):
    import app.ui.routes as ui_routes

    monkeypatch.setattr(ui_routes, "AdzunaSettings", lambda: SimpleNamespace(app_id=None, app_key=None))
    test_client, _ = client

    response = test_client.get("/jobs", params={"titles": ""})

    assert response.status_code == 200
    assert "Job search is temporarily unavailable" in response.text
    assert "ADZUNA" not in response.text.upper()  # users never see the provider's name


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


def test_browse_track_without_resume_shows_why_job_was_not_analyzed(client):
    """Tracking before any resume is imported used to leave the job silently
    stuck at DISCOVERED -- the detail page must say why and offer a way out.
    """
    test_client, SessionLocal = client
    from app.models.candidate import CandidateProfile

    session = SessionLocal()
    session.query(CandidateProfile).update({"is_active": False})
    session.commit()
    session.close()

    response = test_client.post("/browse/track", data=_TRACK_PAYLOAD)

    assert response.status_code == 200
    assert "No resume yet" in response.text
    assert "Analyze &amp; tailor" in response.text


def test_browse_track_below_minimum_score_explains_no_resume(client):
    test_client, _ = client

    response = test_client.post("/browse/track", data=_TRACK_PAYLOAD)

    assert response.status_code == 200
    assert "below your minimum score" in response.text


def test_analyze_action_imports_uploaded_resume_then_analyzes(client, tmp_path, monkeypatch):
    from pathlib import Path

    import app.ui.routes as ui_routes
    from app.models.candidate import CandidateProfile
    from app.models.job import Job

    monkeypatch.setattr(ui_routes, "UPLOAD_DIR", tmp_path / "uploads")
    test_client, SessionLocal = client
    session = SessionLocal()
    session.query(CandidateProfile).update({"is_active": False})
    session.commit()
    session.close()
    test_client.post("/browse/track", data=_TRACK_PAYLOAD)
    session = SessionLocal()
    job_id = session.query(Job.id).scalar()
    session.close()

    sample = Path(__file__).resolve().parents[1] / "fixtures" / "sample_resume.docx"
    with open(sample, "rb") as f:
        response = test_client.post(f"/jobs/{job_id}/analyze", files={"resume": ("resume.docx", f)})

    assert response.status_code == 200
    assert "No resume yet" not in response.text
    assert "below your minimum score" in response.text  # it got as far as scoring
    session = SessionLocal()
    assert session.query(CandidateProfile).filter_by(is_active=True).count() == 1
    session.close()


def test_analyze_action_rejects_unsupported_resume_type(client):
    test_client, SessionLocal = client
    test_client.post("/browse/track", data=_TRACK_PAYLOAD)
    from app.models.job import Job

    session = SessionLocal()
    job_id = session.query(Job.id).scalar()
    session.close()

    response = test_client.post(f"/jobs/{job_id}/analyze", files={"resume": ("resume.txt", b"hello")})

    assert response.status_code == 200
    assert "Resume must be a .docx or .pdf file." in response.text


def test_retailor_rewrites_resume_without_changing_status(client, monkeypatch, tmp_path):
    import app.ui.routes as ui_routes
    from app.models.job import Job, JobStatus

    test_client, SessionLocal = client
    created = test_client.post("/browse/analyze", data=_TRACK_PAYLOAD, follow_redirects=False)
    job_id = int(created.headers["location"].split("/jobs/")[1].split("#")[0])
    with SessionLocal() as session:
        session.get(Job, job_id).status = JobStatus.APPLICATION_OPENED
        session.commit()

    calls = []

    def fake_tailor(session, job, config, ai_provider=None, *, update_status=True):
        calls.append(update_status)
        path = tmp_path / "tailored_resume.docx"
        path.write_bytes(b"docx")
        return SimpleNamespace(docx_path=path, tailored_resume=SimpleNamespace(pdf_path=None))

    monkeypatch.setattr(ui_routes, "tailor_resume", fake_tailor)
    monkeypatch.setattr(ui_routes, "convert_to_pdf", lambda path: path.with_suffix(".pdf"))

    response = test_client.post(f"/jobs/{job_id}/retailor", follow_redirects=False)

    assert response.status_code == 303 and "re-tailored" in response.headers["location"]
    assert calls == [False]
    with SessionLocal() as session:
        assert session.get(Job, job_id).status == JobStatus.APPLICATION_OPENED


def test_pasting_the_full_description_replaces_the_preview_and_reruns_analysis(client):
    from app.models.job import Job, JobRequirements, JobStatus
    from app.models.matching import JobFitAssessment

    test_client, SessionLocal = client
    created = test_client.post("/browse/analyze", data=_TRACK_PAYLOAD, follow_redirects=False)
    job_id = int(created.headers["location"].split("/jobs/")[1].split("#")[0])
    with SessionLocal() as session:
        session.get(Job, job_id).status = JobStatus.APPLICATION_OPENED
        session.add(JobRequirements(job_id=job_id, required_skills=["Snippet"]))
        session.commit()
        assessments_before = session.query(JobFitAssessment).filter_by(job_id=job_id).count()

    full = "Senior Backend Engineer at Acme.\\n" + "Build Java and Kafka services on GCP. " * 20
    response = test_client.post(f"/jobs/{job_id}/description", data={"description": full}, follow_redirects=False)

    assert response.status_code == 303 and "Full+description+saved" in response.headers["location"]
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job.description.startswith("Senior Backend Engineer at Acme.")
        assert job.status == JobStatus.APPLICATION_OPENED  # re-scoring never moves the status
        assert session.query(JobRequirements).filter_by(job_id=job_id).count() == 0  # snippet-derived data dropped
        assert session.query(JobFitAssessment).filter_by(job_id=job_id).count() == assessments_before + 1


def test_preview_descriptions_are_flagged_and_too_short_pastes_rejected(client):
    from app.models.job import Job

    test_client, SessionLocal = client
    payload = {**_TRACK_PAYLOAD, "description": "x" * 500}
    created = test_client.post("/browse/track", data=payload, follow_redirects=False)
    job_id = int(created.headers["location"].split("/jobs/")[1].split("#")[0].split("?")[0])

    page = test_client.get(f"/jobs/{job_id}").text
    assert "Preview only" in page and "Paste the full description" in page

    response = test_client.post(f"/jobs/{job_id}/description", data={"description": "too short"})
    assert "too short to be a job description" in response.text
    with SessionLocal() as session:
        assert session.get(Job, job_id).description == "x" * 500


def test_below_minimum_job_explains_why_and_can_be_tailored_anyway(client, monkeypatch, tmp_path):
    import app.ui.routes as ui_routes
    from app.models.job import Job, JobStatus

    test_client, SessionLocal = client
    test_client.post("/browse/track", data=_TRACK_PAYLOAD)
    with SessionLocal() as session:
        job_id = session.query(Job.id).scalar()

    page = test_client.get(f"/jobs/{job_id}").text
    assert "below your minimum score of" in page and "Tailor anyway" in page
    assert 'name="resume"' not in page.split('id="description"')[0].split("Tailored resume")[-1]  # no upload box

    calls = []

    def fake_tailor(session, job, config, ai_provider=None, *, update_status=True):
        calls.append(update_status)
        path = tmp_path / "tailored_resume.docx"
        path.write_bytes(b"docx")
        return SimpleNamespace(docx_path=path, tailored_resume=SimpleNamespace(pdf_path=None))

    monkeypatch.setattr(ui_routes, "tailor_resume", fake_tailor)
    monkeypatch.setattr(ui_routes, "convert_to_pdf", lambda path: path.with_suffix(".pdf"))

    response = test_client.post(f"/jobs/{job_id}/analyze", data={"force": "true"}, follow_redirects=False)

    assert response.status_code == 303
    assert calls == [False]  # tailored, without pretending the job qualified
    with SessionLocal() as session:
        assert session.get(Job, job_id).status == JobStatus.ANALYZED
