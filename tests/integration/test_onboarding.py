"""The visitor-to-member journey, end to end with real sign-in: the public
landing page, searching while signed out (the search is kept through
sign-up), email codes (wrong / expired / too many attempts / resend), account
creation, the optional profile step, saved jobs, search filters, the profile
page, signing out and back in."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.auth import email as mailer
from app.database.base import Base
from app.database.session import get_session
from app.models.profile import ProfileExperience, SavedJob
from app.models.user import AuthSession, EmailCode, User
from app.services import pool
from app.sources.base import RawJob
from app.tenancy import system_session, tenant_session

pytestmark = pytest.mark.real_auth

BASE = "http://testserver"
PASSWORD = "Maple-Leaf-Careers-2026"
SEARCH = "/jobs?q=Data+Analyst&l=Toronto"
FULL = "About the role. You will turn data into decisions with SQL and Python. " * 20


def _feed(provider, board):
    jobs = [
        ("1", "Data Analyst", "Toronto, ON", {"workplaceType": "Hybrid"}),
        ("2", "Senior Data Analyst", "Remote - Canada", {"workplaceType": "Remote", "isRemote": True}),
        ("3", "Office Manager", "Vancouver, BC", {}),
    ]
    return [RawJob(external_id=ext, source="ashby", source_url=f"https://careers.example/{ext}", company="acme",
                   title=title, location=loc, description=f"{FULL} ({title})", date_posted=datetime(2026, 10, 1),
                   raw_payload={**extra, "compensation": {"scrapeableCompensationSalarySummary": "CA$90K – CA$110K"}})
            for ext, title, loc, extra in jobs]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", BASE)
    for var in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "FACEBOOK_CLIENT_ID", "FACEBOOK_CLIENT_SECRET"):
        monkeypatch.setenv(var, "")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with factory() as s:
        pool.add_company(s, "ashby", "acme", fetcher=_feed)
    import app.ui.public_routes as public_routes

    public_routes._cache.clear()

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    outbox: list = []
    monkeypatch.setattr(mailer, "outbox", outbox)
    import main

    main.app.dependency_overrides[get_session] = override_get_session
    yield TestClient(main.app, base_url=BASE), factory, outbox
    main.app.dependency_overrides.clear()


def _code(outbox) -> str:
    return re.search(r"\b(\d{6})\b", outbox[-1].get_content()).group(1)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def _register(http, outbox, email="mia@example.com", next_path=SEARCH):
    http.post("/api/auth/register/start", json={"email": email, "next": next_path})
    http.post("/api/auth/verify-email", json={"email": email, "code": _code(outbox), "next": next_path})
    return http.post("/api/auth/register/complete", json={
        "first_name": "Mia", "last_name": "Chen", "password": PASSWORD, "confirm_password": PASSWORD,
        "next": next_path})


# ---- signed out ----------------------------------------------------------------------

def test_landing_page_is_public_and_shows_real_jobs(env):
    http, _, _ = env
    page = http.get("/")
    assert page.status_code == 200
    text = page.text
    assert "Find Your Next Career Opportunity" in text
    assert 'data-auth-search' in text and 'name="q"' in text and 'name="l"' in text
    assert "Popular job categories" in text and "How it works" in text
    assert "Senior Data Analyst" in text and "Acme" in text  # recently posted, from the real pool
    assert 'id="auth-dialog"' in text  # the sign-in dialog is there for signed-out visitors
    assert "Continue with Google" not in text  # not configured
    assert "I'm hiring" not in text and "For Employers" not in text  # employer accounts aren't open yet
    assert "guest" not in text.lower()


def test_searching_while_signed_out_asks_to_sign_in_and_keeps_the_search(env):
    http, _, _ = env
    response = http.get(SEARCH, follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers["location"])
    assert target.path == "/login" and parse_qs(target.query)["next"] == [SEARCH]
    login = http.get(response.headers["location"])
    assert f'data-next="{SEARCH.replace("&", "&amp;")}"' in login.text


def test_saving_needs_an_account(env):
    http, _, _ = env
    assert http.post("/saved", data={"ref": "p1"}, follow_redirects=False).headers["location"].startswith("/login")
    assert http.post("/api/jobs/p1/save").status_code == 401
    assert http.get("/api/jobs/search?q=Data").status_code == 401


# ---- sign-up by email code -------------------------------------------------------------

def test_new_user_signs_up_with_an_emailed_code(env):
    http, factory, outbox = env
    start = http.post("/api/auth/register/start", json={"email": "Mia@Example.com ", "next": SEARCH})
    assert start.status_code == 200
    assert start.json() == {"step": "verify", "email": "mia@example.com", "expires_in": 60, "resend_in": 30}
    code = _code(outbox)
    assert outbox[-1]["Subject"] == "Verify your email address" and "expires in 60 seconds" in outbox[-1].get_content()
    assert code not in start.text
    with system_session(factory) as s:
        row = s.query(EmailCode).one()
        assert row.purpose == "register" and code not in row.code_hash and len(row.code_hash) == 64

    wrong = http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": _wrong(code)})
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "invalid"
    assert wrong.json()["error"]["attempts_left"] == 4

    details = http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": code, "next": SEARCH})
    assert details.json()["step"] == "details" and "jsa_reg" in details.headers["set-cookie"]
    assert "httponly" in details.headers["set-cookie"].lower()
    assert http.get("/api/auth/me").json() == {"authenticated": False}  # not signed in until the details

    mismatch = http.post("/api/auth/register/complete", json={
        "first_name": "Mia", "last_name": "Chen", "password": PASSWORD, "confirm_password": PASSWORD + "x"})
    assert mismatch.status_code == 400 and "confirm_password" in mismatch.json()["error"]["fields"]

    created = http.post("/api/auth/register/complete", json={
        "first_name": "Mia", "last_name": "Chen", "password": PASSWORD, "confirm_password": PASSWORD, "next": SEARCH})
    assert created.status_code == 200 and created.json()["step"] == "created"
    redirect = created.json()["redirect"]
    assert redirect.startswith("/welcome") and parse_qs(urlparse(redirect).query)["next"] == [SEARCH]
    assert outbox[-1]["Subject"] == "Welcome to Job Assistant"

    with system_session(factory) as s:
        user = s.query(User).one()
        assert (user.first_name, user.last_name, user.account_type) == ("Mia", "Chen", "job_seeker")
        assert user.email_verified_at is not None and user.password_hash.startswith("$argon2id$")
    me = http.get("/api/auth/me").json()
    assert me["authenticated"] and me["profile_completion"] < 60

    # The ticket was single use: the same browser can't create a second account with it.
    again = http.post("/api/auth/register/complete", json={
        "first_name": "Eve", "last_name": "X", "password": PASSWORD, "confirm_password": PASSWORD})
    assert again.status_code == 400 and again.json()["error"]["code"] == "registration_expired"


def test_expired_and_overused_codes_are_refused(env):
    http, factory, outbox = env
    http.post("/api/auth/register/start", json={"email": "mia@example.com"})
    code = _code(outbox)
    with system_session(factory) as s:
        s.query(EmailCode).update({EmailCode.expires_at: datetime.utcnow() - timedelta(seconds=1)})
        s.commit()
    expired = http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": code})
    assert expired.status_code == 400 and expired.json()["error"]["code"] == "expired"

    with system_session(factory) as s:  # a fresh code, then five wrong guesses
        s.query(EmailCode).update({EmailCode.created_at: datetime.utcnow() - timedelta(minutes=5)})
        s.commit()
    http.post("/api/auth/resend-otp", json={"email": "mia@example.com"})
    code = _code(outbox)
    for _ in range(4):
        assert http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": _wrong(code)}
                         ).json()["error"]["code"] == "invalid"
    fifth = http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": _wrong(code)})
    assert fifth.status_code == 429 and fifth.json()["error"]["code"] == "too_many_attempts"
    right = http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": code})
    assert right.json()["error"]["code"] == "too_many_attempts"  # that code is done for


def test_resend_waits_and_retires_the_old_code(env):
    http, factory, outbox = env
    http.post("/api/auth/register/start", json={"email": "mia@example.com"})
    old = _code(outbox)
    too_soon = http.post("/api/auth/resend-otp", json={"email": "mia@example.com"})
    assert too_soon.status_code == 429 and too_soon.json()["error"]["code"] == "cooldown"
    assert 0 < too_soon.json()["error"]["retry_after"] <= 30
    with system_session(factory) as s:
        s.query(EmailCode).update({EmailCode.created_at: datetime.utcnow() - timedelta(seconds=31)})
        s.commit()
    assert http.post("/api/auth/resend-otp", json={"email": "mia@example.com"}).status_code == 200
    new = _code(outbox)
    if new != old:
        assert http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": old}
                         ).json()["error"]["code"] == "invalid"
    assert http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": new}).json()["step"] == "details"


def test_invalid_email_and_delivery_failure(env, monkeypatch):
    http, factory, _ = env
    bad = http.post("/api/auth/register/start", json={"email": "not-an-email"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_email"

    def fail(*a, **k):
        raise mailer.EmailDeliveryError("smtp down")
    monkeypatch.setattr(mailer, "send_code_email", fail)
    down = http.post("/api/auth/register/start", json={"email": "mia@example.com"})
    assert down.status_code == 503 and down.json()["error"]["code"] == "email_failed"
    with system_session(factory) as s:
        assert s.query(EmailCode).count() == 0  # withdrawn, so a retry isn't held up by the cooldown


def test_existing_address_gets_a_sign_in_code_and_the_same_answer(env):
    http, factory, outbox = env
    _register(http, outbox)
    http.post("/api/auth/logout")
    with system_session(factory) as s:
        s.query(EmailCode).delete()
        s.commit()

    start = http.post("/api/auth/register/start", json={"email": "mia@example.com", "next": SEARCH})
    assert start.json() == {"step": "verify", "email": "mia@example.com", "expires_in": 60, "resend_in": 30}
    assert outbox[-1]["Subject"] == "Your Job Assistant sign-in code"
    signed_in = http.post("/api/auth/verify-email", json={"email": "mia@example.com", "code": _code(outbox),
                                                          "next": SEARCH})
    assert signed_in.json()["step"] == "done"  # straight in -- no second account
    with system_session(factory) as s:
        assert s.query(User).count() == 1
    assert http.get("/api/auth/me").json()["authenticated"]


def test_too_many_code_requests_from_one_network(env):
    http, factory, _ = env
    from app.auth.api import code_limiter

    code_limiter.limit, original = 3, code_limiter.limit
    try:
        statuses = [http.post("/api/auth/register/start", json={"email": f"u{i}@example.com"}).status_code
                    for i in range(4)]
    finally:
        code_limiter.limit = original
    assert statuses == [200, 200, 200, 429]


# ---- after sign-up: the profile step, searching, saving ------------------------------------

def test_profile_step_is_optional_and_asked_once(env):
    http, factory, outbox = env
    redirect = _register(http, outbox).json()["redirect"]
    page = http.get(redirect)
    assert "Let's build your professional profile" in page.text and "Continue Job Search" in page.text
    skip = http.post("/welcome", data={"choice": "search", "next": SEARCH}, follow_redirects=False)
    assert skip.headers["location"] == SEARCH  # back to the original search

    http.post("/api/auth/logout")
    again = http.post("/api/auth/login", json={"email": "mia@example.com", "password": PASSWORD, "next": SEARCH})
    assert again.json() == {"step": "done", "redirect": SEARCH}  # not asked again


def test_original_search_runs_after_sign_in_with_filters_saves_and_details(env):
    http, factory, outbox = env
    _register(http, outbox)
    page = http.get(SEARCH)
    assert page.status_code == 200
    assert 'name="q" value="Data Analyst"' in page.text and 'name="l" value="Toronto"' in page.text
    assert "Data Analyst" in page.text and "Office Manager" not in page.text
    assert "$90K – $110K a year" in page.text and "Company career site" in page.text

    everywhere = http.get("/jobs?q=Data+Analyst")
    assert "Senior Data Analyst" in everywhere.text
    remote = http.get("/jobs?q=Data+Analyst&workplace=remote")
    assert "Senior Data Analyst" in remote.text and 'data-ref="p1"' not in remote.text
    assert 'value="remote" checked' in remote.text  # filters live in the URL
    none = http.get("/jobs?q=Data+Analyst&type=internship")
    assert "No jobs found. Try adjusting your search filters." in none.text

    with system_session(factory) as s:
        from app.models.pool import PoolJob

        senior = s.query(PoolJob).filter(PoolJob.title == "Senior Data Analyst").one().id
    detail = http.get(f"/jobs?q=Data+Analyst&view=p{senior}")
    assert 'class="result-card selected" data-ref="p%d"' % senior in detail.text
    assert http.get(f"/jobs/posting/p{senior}").status_code == 200

    saved = http.post("/saved", data={"ref": f"p{senior}"}, headers={"HX-Request": "true"})
    assert "Saved" in saved.text and "/remove" in saved.text
    listing = http.get("/saved")
    assert "Senior Data Analyst" in listing.text
    assert "Senior Data Analyst" not in http.get("/applications").text  # only saved, not worked on yet
    assert http.get("/api/saved-jobs").json()[0]["title"] == "Senior Data Analyst"

    # The JSON API too.
    results = http.get("/api/jobs/search?q=Data+Analyst&workplace=remote").json()["results"]
    assert [r["title"] for r in results] == ["Senior Data Analyst"] and results[0]["saved"]
    assert http.delete(f"/api/jobs/p{senior}/save").json() == {"saved": False}
    assert http.post(f"/api/jobs/p{senior}/save").status_code == 201
    assert http.get(f"/api/jobs/p{senior}").json()["description"].startswith("About the role.")


def test_profile_completed_later(env):
    http, factory, outbox = env
    _register(http, outbox)
    start = http.get("/api/profile").json()["completion_percentage"]

    http.post("/profile/personal", data={"first_name": "Mia", "last_name": "Chen", "phone": "604 555 1234",
                                         "city": "Toronto", "province": "ON", "country": "Canada"})
    http.post("/profile/professional", data={"headline": "Analyst who ships", "current_title": "Data Analyst",
                                             "years_experience": "4", "skills": "SQL, Python, Tableau, SQL"})
    http.post("/profile/preferences", data={"preferred_titles": "Data Analyst", "preferred_locations": "Toronto",
                                            "employment_types": ["full_time", "bogus"], "workplaces": ["hybrid"]})
    bad = http.post("/profile/experience", data={"job_title": "Analyst", "company": ""}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    http.post("/profile/experience", data={"job_title": "Data Analyst", "company": "Shopify", "start_date": "2022-03",
                                           "is_current": "1", "description": "Dashboards"})
    http.post("/profile/education", data={"institution": "University of Toronto", "qualification": "BSc",
                                          "start_date": "2016-09", "end_date": "2020-05"})

    profile = http.get("/api/profile").json()
    assert profile["phone"] == "+16045551234" and profile["skills"] == ["SQL", "Python", "Tableau"]
    assert profile["employment_types"] == ["full_time"] and profile["workplaces"] == ["hybrid"]
    assert profile["experience"][0]["company"] == "Shopify" and profile["experience"][0]["is_current"]
    assert profile["completion_percentage"] == 90 > start  # everything but a resume
    assert profile["suggestions"] == ["Upload your resume"]
    page = http.get("/profile")
    assert "Your profile is <strong>90%</strong> complete." in page.text

    updated = http.put("/api/profile", json={"headline": "Senior-bound analyst"}).json()
    assert updated["headline"] == "Senior-bound analyst" and updated["current_title"] == "Data Analyst"
    assert http.put("/api/profile", json={"phone": "12"}).status_code == 400
    assert http.post("/api/profile/experience", json={"job_title": "Intern", "company": "Acme",
                                                      "start_date": "2021-05", "end_date": "2020-01"}).status_code == 400

    # Nobody else sees it.
    with system_session(factory) as s:
        s.add(User(id=99, email="other@example.com", first_name="O", last_name="T", role="member",
                   email_verified_at=datetime(2026, 1, 1)))
        s.commit()
    with tenant_session(factory, 99) as s:
        assert s.query(ProfileExperience).count() == 0 and s.query(SavedJob).count() == 0


def test_sign_out_and_back_in_and_sessions_expire(env):
    http, factory, outbox = env
    _register(http, outbox)
    http.post("/logout")
    assert http.get("/jobs", follow_redirects=False).status_code == 303
    wrong = http.post("/api/auth/login", json={"email": "mia@example.com", "password": "Not-The-Password-1"})
    assert wrong.status_code == 401
    ok = http.post("/api/auth/login", json={"email": "mia@example.com", "password": PASSWORD, "next": "/saved"})
    assert ok.status_code == 200 and "jsa_session" in ok.headers["set-cookie"]
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert http.get("/saved").status_code == 200

    with system_session(factory) as s:
        s.query(AuthSession).update({AuthSession.expires_at: datetime.utcnow() - timedelta(minutes=1)})
        s.commit()
    assert http.get("/api/auth/me").json() == {"authenticated": False}
    assert http.get("/saved", follow_redirects=False).headers["location"].startswith("/login")


def test_signed_in_header_and_landing(env):
    http, _, outbox = env
    _register(http, outbox)
    assert http.get("/", follow_redirects=False).headers["location"] == "/jobs"
    page = http.get("/companies")
    for item in ("My Profile", "Saved Jobs", "Applications", "Settings", "Log Out"):
        assert item in page.text
    assert 'id="auth-dialog"' not in page.text
