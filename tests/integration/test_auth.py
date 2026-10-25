"""Real sign-in, end to end (no signed-in test user): password rules, lockout,
password reset, sign-out, cross-site request blocking, Google/Facebook
sign-in and the account page. Sign-up by emailed code is covered step by
step in test_onboarding.py; here `_signup` just runs it."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database.base import Base
from app.core.database.session import get_session
from app.core.tenancy import system_session, tenant_session
from app.integrations import email as mailer
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.auth.models import AuthSession, OAuthIdentity, User
from app.modules.auth.service import ProviderProfile

pytestmark = pytest.mark.real_auth

STRONG = "Correct-Horse-9-battery"
BASE = "http://testserver"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", BASE)
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    for var in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "FACEBOOK_CLIENT_ID", "FACEBOOK_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})

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


def _link(outbox, path: str) -> str:
    body = outbox[-1].get_content()
    return re.search(rf"{re.escape(BASE)}({re.escape(path)}\?token=[\w-]+)", body).group(1)


def _code(outbox) -> str:
    return re.search(r"\b(\d{6})\b", outbox[-1].get_content()).group(1)


def _signup(http, email="ann@example.com", password=STRONG, first_name="Ann", last_name="Lee"):
    """Sign up by emailed code (signed in afterwards); returns the final response."""
    outbox = mailer.outbox
    assert http.post("/api/auth/register/start", json={"email": email}).status_code == 200
    step = http.post("/api/auth/verify-email", json={"email": email, "code": _code(outbox)})
    assert step.json()["step"] == "details", step.text
    return http.post("/api/auth/register/complete", json={
        "first_name": first_name, "last_name": last_name, "password": password, "confirm_password": password})


def _unconfirmed(factory, email="ann@example.com", password=STRONG) -> None:
    """An account from before sign-up needed a code (or a stranger's): never confirmed."""
    from app.modules.auth.passwords import hash_password

    with system_session(factory) as s:
        s.add(User(email=email, first_name="Ann", last_name="Lee", role="member", password_hash=hash_password(password)))
        s.commit()


def _user(factory, email) -> User | None:
    with system_session(factory) as s:
        return s.query(User).filter(User.email == email, User.role != "guest").one_or_none()


def test_everything_needs_sign_in(env):
    http, _, _ = env
    for path in ("/resume", "/settings", "/watchlist", "/jobs/1", "/account", "/profile", "/saved",
                 "/applications", "/jobs/posting/a1"):
        response = http.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login"), path
    assert http.get("/api/jobs").status_code == 401
    assert http.get("/api/profile").status_code == 401
    assert http.get("/api/auth/me").json() == {"authenticated": False}
    for public in ("/", "/jobs", "/companies", "/resources", "/terms", "/privacy", "/login", "/signup"):
        assert http.get(public).status_code == 200, public
    assert http.get("/health").json() == {"status": "ok"}  # liveness stays public


def test_signup_creates_a_verified_account_and_signs_in(env):
    http, factory, _ = env
    response = _signup(http)
    assert response.status_code == 200 and response.json()["step"] == "created"
    user = _user(factory, "ann@example.com")
    assert user.email_verified_at is not None and user.account_type == "job_seeker"
    assert user.password_hash.startswith("$argon2id$")
    assert http.get("/settings").status_code == 200
    me = http.get("/api/auth/me").json()
    assert me["authenticated"] and me["first_name"] == "Ann" and me["email_verified"]


@pytest.mark.parametrize("password, problem", [
    ("short-1", "at least 8"), ("Password-1234!", "common"), ("12345678", "common"), ("qwerty2024", "common"),
    ("aaaaaaaaaaaaaaa", "common"),
])
def test_weak_passwords_are_rejected(env, password, problem):
    http, factory, _ = env
    response = _signup(http, password=password)
    assert response.status_code == 400 and problem in response.json()["error"]["fields"]["password"]
    assert _user(factory, "ann@example.com") is None


def test_long_passphrases_need_no_symbols_or_capitals(env):
    http, factory, _ = env
    assert _signup(http, password="correct horse battery staple").status_code == 200


@pytest.mark.parametrize("password", ["maple123", "ann-lee-2026", "sunny day"])
def test_simple_passwords_of_8_or_more_characters_are_fine(env, password):
    http, factory, _ = env
    assert _signup(http, password=password).status_code == 200


def test_wrong_password_and_lockout(env):
    http, factory, outbox = env
    _signup(http)
    http.post("/logout")
    for _ in range(5):
        response = http.post("/api/auth/login", json={"email": "ann@example.com", "password": "Wrong-Pass-123!"})
        assert response.status_code == 401 and "Incorrect email or password" in response.json()["error"]["message"]
    response = http.post("/api/auth/login", json={"email": "ann@example.com", "password": STRONG})
    assert "Too many failed attempts" in response.json()["error"]["message"]  # even the right password, for now
    unknown = http.post("/api/auth/login", json={"email": "nobody@example.com", "password": STRONG})
    assert "Incorrect email or password" in unknown.json()["error"]["message"]  # unknown accounts look the same


def test_password_reset_signs_out_everywhere(env):
    http, factory, outbox = env
    _signup(http)

    response = http.post("/api/auth/forgot-password", json={"email": "ann@example.com"})
    assert response.json() == {"step": "reset_sent"}
    link = _link(outbox, "/reset-password")
    token = parse_qs(urlparse(link).query)["token"][0]
    assert http.post("/api/auth/reset-password", json={"token": token, "password": "weak",
                                                       "confirm_password": "weak"}).status_code == 400
    assert http.post("/api/auth/reset-password", json={"token": token, "password": "New-Strong-Pass-42",
                                                       "confirm_password": "New-Strong-Pass-42"}).status_code == 200
    with system_session(factory) as s:
        assert s.query(AuthSession).count() == 0  # every browser signed out
    assert http.get("/settings", follow_redirects=False).status_code == 303
    assert http.post("/api/auth/login", json={"email": "ann@example.com", "password": "New-Strong-Pass-42"}
                     ).status_code == 200
    # Single use.
    again = http.post("/api/auth/reset-password", json={"token": token, "password": "Other-Strong-Pass-1",
                                                        "confirm_password": "Other-Strong-Pass-1"})
    assert again.status_code == 400
    # Unknown address: same answer, no email.
    sent = len(outbox)
    assert http.post("/api/auth/forgot-password", json={"email": "nobody@example.com"}).json() == {"step": "reset_sent"}
    assert len(outbox) == sent
    # The page-based form (no JavaScript) still works too.
    assert "an account for that address" in http.post("/forgot-password", data={"email": "ann@example.com"}).text


def test_guest_access_is_gone(env):
    http, _, _ = env
    assert http.post("/guest", data={"first_name": "Gus", "last_name": "Tee", "email": "gus@example.com"}
                     ).status_code in (404, 405)
    assert "guest" not in http.get("/login").text.lower()


def test_sign_out_ends_the_session(env):
    http, _, _ = env
    _signup(http)
    assert http.get("/settings").status_code == 200
    http.post("/logout")
    assert http.get("/settings", follow_redirects=False).status_code == 303
    # The API sign-out works the same way.
    http.post("/api/auth/login", json={"email": "ann@example.com", "password": STRONG})
    assert http.post("/api/auth/logout").json() == {"step": "signed_out"}
    assert http.get("/api/auth/me").json() == {"authenticated": False}


def test_cross_site_posts_are_blocked(env):
    http, _, _ = env
    response = http.post("/login", data={"email": "a@b.c", "password": "x"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert http.get("/login").headers["x-frame-options"] == "DENY"


@pytest.mark.rbac_strict
def test_admin_emails_become_admins(env):
    """ADMIN_EMAILS is the trusted bootstrap: that account becomes a Super Admin."""
    http, factory, outbox = env
    _signup(http, email="boss@example.com")
    with factory() as s:
        boss = s.query(User).filter(User.email == "boss@example.com").one()
        assert set(boss.roles) == {"SUPER_ADMIN", "JOB_SEEKER"} and boss.is_admin
    assert http.get("/admin/health").status_code == 200


def _social(monkeypatch, profile: ProviderProfile, *, any_address: bool = True):
    """Fake providers. `any_address`: skip the https-and-a-domain rule, since the
    test client's address is http://testserver."""
    import app.modules.auth.oauth as oauth

    monkeypatch.setattr(oauth.AuthSettings, "provider_enabled", lambda self, p: True)
    if any_address:
        monkeypatch.setattr(oauth.AuthSettings, "social_redirect_allowed", property(lambda self: True))
    monkeypatch.setattr(oauth, "_google_profile", lambda http, code, verifier: profile)
    monkeypatch.setattr(oauth, "_facebook_profile", lambda http, code: profile)


def _social_login(http, provider="google", tamper=False):
    start = http.get(f"/auth/{provider}/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    if tamper:
        http.cookies.clear()
    return http.get(f"/auth/{provider}/callback", params={"code": "abc", "state": state}, follow_redirects=False)


def test_google_sign_in_creates_a_verified_account(env, monkeypatch):
    http, factory, _ = env
    _social(monkeypatch, ProviderProfile("google", "g-1", "gina@example.com", True, "Gina", "Gee"))
    response = _social_login(http)
    assert response.status_code == 303 and response.headers["location"].startswith("/welcome")
    user = _user(factory, "gina@example.com")
    assert user.email_verified and user.password_hash is None
    assert http.get("/settings").status_code == 200
    # Second time: same account.
    http.post("/logout")
    _social_login(http)
    with system_session(factory) as s:
        assert s.query(User).count() == 1 and s.query(OAuthIdentity).count() == 1


def test_social_callback_from_another_browser_is_rejected(env, monkeypatch):
    http, factory, _ = env
    _social(monkeypatch, ProviderProfile("google", "g-1", "gina@example.com", True, "Gina", "Gee"))
    response = _social_login(http, tamper=True)
    assert "/login?error=" in response.headers["location"]
    assert _user(factory, "gina@example.com") is None


def test_social_sign_in_starts_on_the_public_address(env, monkeypatch):
    """Opened as localhost while PUBLIC_URL says 127.0.0.1 (or the reverse): the
    state cookie must be set where the provider will send the browser back."""
    http, factory, _ = env
    _social(monkeypatch, ProviderProfile("google", "g-1", "gina@example.com", True, "Gina", "Gee"))
    other = http.get("http://localhost/auth/google/start?next=%2Fjobs", follow_redirects=False)
    assert other.status_code == 303 and other.headers["location"] == f"{BASE}/auth/google/start?next=%2Fjobs"
    assert "jsa_oauth_state" not in other.headers.get("set-cookie", "")
    assert _social_login(http).headers["location"].startswith("/welcome")


def test_social_sign_in_isnt_offered_when_public_url_is_this_computer(env, monkeypatch):
    """PUBLIC_URL left at 127.0.0.1 while people reach the site by its public
    address: Google would send them back to their own device, so it's hidden and
    the start link explains instead of bouncing to 127.0.0.1."""
    http, _, _ = env
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:8001")
    _social(monkeypatch, ProviderProfile("google", "g-1", "gina@example.com", True, "Gina", "Gee"), any_address=False)

    public = "http://75.0.0.10:8001"
    assert "/auth/google/start" not in http.get(f"{public}/login").text
    start = http.get(f"{public}/auth/google/start?next=%2Fjobs", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"].startswith("/login?error=")
    assert "127.0.0.1" not in start.headers["location"]

    # On the computer itself it still works (localhost goes to 127.0.0.1 to start).
    assert "/auth/google/start" in http.get("http://127.0.0.1:8001/login").text
    local = http.get("http://localhost:8001/auth/google/start", follow_redirects=False)
    assert local.headers["location"] == "http://127.0.0.1:8001/auth/google/start"


@pytest.mark.parametrize("public_url, offered", [
    ("http://75.0.0.10:8001", False),  # Google refuses IP addresses
    ("https://75.0.0.10", False),
    ("http://jobs.example.com", False),  # and plain http on a domain
    ("https://jobs.example.com", True),
])
def test_social_sign_in_needs_https_on_a_domain(env, monkeypatch, public_url, offered):
    http, _, _ = env
    monkeypatch.setenv("PUBLIC_URL", public_url)
    _social(monkeypatch, ProviderProfile("google", "g-1", "gina@example.com", True, "Gina", "Gee"), any_address=False)
    assert ("/auth/google/start" in http.get(f"{public_url}/login").text) is offered


@pytest.mark.parametrize("public_url, offered", [("https://hirlumo.com", True), ("http://127.0.0.1:8001", False)])
def test_sign_up_pop_up_on_every_page_offers_google(env, monkeypatch, public_url, offered):
    """Sign Up, Save and the guest banner open a pop-up, not /signup: it offers
    Google too, under the same rule as the sign-in pages."""
    http, _, _ = env
    monkeypatch.setenv("PUBLIC_URL", public_url)
    _social(monkeypatch, ProviderProfile("google", "g-1", "gina@example.com", True, "Gina", "Gee"), any_address=False)
    for page in ("/", "/jobs", "/companies"):
        assert ("/auth/google/start" in http.get(f"https://hirlumo.com{page}").text) is offered, page


@pytest.mark.parametrize("public_url", ["https://hirlumo.com", "https://www.hirlumo.com"])
def test_www_and_bare_domain_both_lead_to_the_public_address(env, monkeypatch, public_url):
    """Both addresses work; everyone lands on PUBLIC_URL's, where sign-in lives."""
    http, _, _ = env
    monkeypatch.setenv("PUBLIC_URL", public_url)
    other = "https://hirlumo.com" if "www." in public_url else "https://www.hirlumo.com"
    page = http.get(f"{other}/jobs?q=nurse", follow_redirects=False)
    assert page.status_code == 301 and page.headers["location"] == f"{public_url}/jobs?q=nurse"
    form = http.post(f"{other}/login", data={}, follow_redirects=False)
    assert form.status_code == 308 and form.headers["location"] == f"{public_url}/login"
    assert http.get(f"{public_url}/", follow_redirects=False).status_code == 200
    assert http.get(f"{other}/health").status_code == 200  # Docker's healthcheck isn't redirected
    assert http.get("https://evil.example/", follow_redirects=False).status_code == 200  # only www <-> bare


def test_reset_link_uses_public_url_not_the_request_address(env, monkeypatch):
    """A forged Host header must not put someone else's site into the email."""
    http, factory, outbox = env
    _signup(http)
    http.post("/logout")
    monkeypatch.setenv("PUBLIC_URL", "http://75.0.0.10:8001")
    http.post("http://evil.example/forgot-password", data={"email": "ann@example.com"})
    body = str(outbox[-1])
    assert "http://75.0.0.10:8001/reset-password?token=" in body and "evil.example" not in body


def test_unverified_provider_email_cant_join_an_existing_account(env, monkeypatch):
    http, factory, outbox = env
    _signup(http)
    http.post("/logout")
    _social(monkeypatch, ProviderProfile("facebook", "f-9", "ann@example.com", False, "Ann", "Lee"))
    response = _social_login(http, provider="facebook")
    assert "/login?error=" in response.headers["location"]
    with system_session(factory) as s:
        assert s.query(OAuthIdentity).count() == 0


def test_verified_social_email_takes_over_an_unconfirmed_signup(env, monkeypatch):
    """Someone signed up with Ann's address but never confirmed it: when Ann signs
    in with Google, that stranger's password must not keep working."""
    http, factory, _ = env
    _unconfirmed(factory)
    _social(monkeypatch, ProviderProfile("google", "g-ann", "ann@example.com", True, "Ann", "Lee"))
    _social_login(http)
    user = _user(factory, "ann@example.com")
    assert user.email_verified and user.password_hash is None


def test_profile_updates_and_account_deletion(env):
    http, factory, outbox = env
    _signup(http)

    assert http.post("/account/profile", data={"first_name": "Annie", "last_name": "Lee", "phone": "12"}
                     ).status_code == 400
    http.post("/account/profile", data={"first_name": "Annie", "last_name": "Lee", "phone": "+44 20 7946 0958"})
    assert _user(factory, "ann@example.com").phone == "+442079460958"

    assert "incorrect" in http.post("/account/password", data={
        "current_password": "nope", "new_password": "Brand-New-Pass-77", "confirm_password": "Brand-New-Pass-77"}).text

    http.post("/account/email", data={"email": "ann.new@example.com"})
    assert _user(factory, "ann@example.com") is not None  # unchanged until confirmed
    http.get(_link(outbox, "/verify-email"))
    assert _user(factory, "ann.new@example.com") is not None

    assert http.post("/account/delete", data={"confirm_email": "wrong@example.com"}).status_code == 400
    http.post("/account/delete", data={"confirm_email": "ann.new@example.com"})
    assert _user(factory, "ann.new@example.com") is None
    assert http.get("/settings", follow_redirects=False).status_code == 303


# ---- LinkedIn ----------------------------------------------------------------------------

def _linkedin_userinfo(monkeypatch, userinfo: dict, *, token_status: int = 200):
    """Real oauth._linkedin_profile against a fake LinkedIn (token + userinfo endpoints)."""
    import httpx

    import app.modules.auth.oauth as oauth

    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "li-id")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "li-secret")
    monkeypatch.setattr(oauth.AuthSettings, "social_redirect_allowed", property(lambda self: True))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v2/accessToken":
            seen["token_form"] = dict(httpx.QueryParams(request.content.decode()))
            return httpx.Response(token_status, json={"access_token": "li-token", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer li-token"
        return httpx.Response(200, json=userinfo)

    real_complete = oauth.complete
    monkeypatch.setattr(oauth, "complete", lambda *a, **k: real_complete(
        *a, **{**k, "client": httpx.Client(transport=httpx.MockTransport(handler))}))
    return seen


def test_linkedin_sign_up_fills_the_country_and_suggests_importing(env, monkeypatch):
    http, factory, _ = env
    seen = _linkedin_userinfo(monkeypatch, {
        "sub": "li-123", "given_name": "Maya", "family_name": "Okafor", "email": "maya@example.com",
        "email_verified": True, "locale": {"country": "CA", "language": "en"}, "picture": "https://x/y.jpg"})
    start = http.get("/auth/linkedin/start", follow_redirects=False)
    assert start.headers["location"].startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert parse_qs(urlparse(start.headers["location"]).query)["scope"] == ["openid profile email"]
    response = _social_login(http, provider="linkedin")
    assert response.status_code == 303 and response.headers["location"].startswith("/welcome")
    assert seen["token_form"]["redirect_uri"] == f"{BASE}/auth/linkedin/callback"
    user = _user(factory, "maya@example.com")
    assert (user.first_name, user.last_name, user.email_verified) == ("Maya", "Okafor", True)
    assert "Signed up with LinkedIn?" in http.get(response.headers["location"]).text
    assert 'value="Canada"' in http.get("/profile").text
    assert "Signed in with: LinkedIn" in http.get("/account").text


@pytest.mark.parametrize("userinfo, error", [
    ({"sub": "li-1", "name": "Maya Okafor", "email": "maya@example.com"}, "didn't share a verified email"),
    ({"sub": "li-1", "name": "Maya Okafor", "email": "maya@example.com", "email_verified": False},
     "didn't share a verified email"),
    ({"sub": "li-1", "name": "Maya Okafor"}, "didn't share a verified email"),
])
def test_linkedin_without_a_verified_email_is_refused(env, monkeypatch, userinfo, error):
    http, factory, _ = env
    _linkedin_userinfo(monkeypatch, userinfo)
    location = _social_login(http, provider="linkedin").headers["location"]
    assert location.startswith("/login?error=") and "LinkedIn" in parse_qs(urlparse(location).query)["error"][0]
    assert _user(factory, "maya@example.com") is None


def test_linkedin_name_only_and_string_locale(env, monkeypatch):
    http, factory, _ = env
    _linkedin_userinfo(monkeypatch, {"sub": "li-9", "name": "Maya van der Berg", "email": "maya@example.com",
                                     "email_verified": True, "locale": "fr_CA"})
    _social_login(http, provider="linkedin")
    user = _user(factory, "maya@example.com")
    assert (user.first_name, user.last_name) == ("Maya", "van der Berg")


def test_linkedin_joins_an_existing_account_and_keeps_its_country(env, monkeypatch):
    http, factory, _ = env
    _signup(http, email="maya@example.com")
    http.post("/profile/personal", data={"first_name": "Ann", "last_name": "Lee", "country": "India", "city": "Pune"})
    http.post("/logout")
    _linkedin_userinfo(monkeypatch, {"sub": "li-5", "given_name": "M", "family_name": "O",
                                     "email": "Maya@Example.com", "email_verified": True, "locale": {"country": "CA"}})
    assert _social_login(http, provider="linkedin").status_code == 303
    with system_session(factory) as s:
        assert s.query(User).count() == 1 and s.query(OAuthIdentity).one().provider == "linkedin"
    assert 'value="India"' in http.get("/profile").text  # never overwritten


def test_linkedin_errors_are_friendly(env, monkeypatch):
    http, _, _ = env
    _linkedin_userinfo(monkeypatch, {}, token_status=400)  # e.g. the code was already used (back button)
    location = _social_login(http, provider="linkedin").headers["location"]
    assert "Couldn't sign you in with LinkedIn right now" in parse_qs(urlparse(location).query)["error"][0]
    # Cancelled on LinkedIn's page: it returns an error and no code.
    start = http.get("/auth/linkedin/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    cancelled = http.get("/auth/linkedin/callback", params={"error": "user_cancelled_login", "state": state},
                         follow_redirects=False)
    assert "cancelled" in parse_qs(urlparse(cancelled.headers["location"]).query)["error"][0]
    # Not configured: no button and the start link explains.
    monkeypatch.delenv("LINKEDIN_CLIENT_ID")
    assert "/auth/linkedin/start" not in http.get("/login").text
    assert "/login?error=" in http.get("/auth/linkedin/start", follow_redirects=False).headers["location"]


def test_every_configured_provider_has_a_button_in_the_pop_up_and_pages(env, monkeypatch):
    http, _, _ = env
    monkeypatch.setenv("PUBLIC_URL", "https://hirlumo.com")
    for name in ("GOOGLE", "FACEBOOK", "LINKEDIN"):
        monkeypatch.setenv(f"{name}_CLIENT_ID", "id")
        monkeypatch.setenv(f"{name}_CLIENT_SECRET", "secret")
    for page in ("/", "/login", "/signup"):
        html = http.get(f"https://hirlumo.com{page}").text
        assert all(f'data-social="{p}"' in html for p in ("google", "linkedin", "facebook")), page
        assert html.index("Continue with Google") < html.index("Continue with LinkedIn") < html.index(
            "Continue with Facebook")
    monkeypatch.setenv("FACEBOOK_CLIENT_SECRET", "")
    html = http.get("https://hirlumo.com/").text
    assert 'data-social="facebook"' not in html and 'data-social="linkedin"' in html


def test_importing_linkedin_into_the_profile(env):
    from pathlib import Path

    from app.modules.profiles.models import JobSeekerProfile, ProfileEducation, ProfileExperience

    http, factory, _ = env
    _signup(http)
    # Something the member typed themselves: kept, and not duplicated by the import.
    http.post("/profile/professional", data={"headline": "My own headline", "skills": "Excel, SQL"})
    http.post("/profile/experience", data={"job_title": "Junior analyst", "company": "maple grocers",
                                           "start_date": "2017-06"})
    pdf = (Path(__file__).resolve().parents[1] / "fixtures" / "linkedin_profile_sample.pdf").read_bytes()
    response = http.post("/profile/import-linkedin", files=[("files", ("Profile.pdf", pdf, "application/pdf"))],
                         follow_redirects=False)
    assert response.status_code == 303
    message = parse_qs(urlparse(response.headers["location"]).query)["msg"][0]
    assert message.startswith("Imported from LinkedIn: 2 jobs; 2 schools; 2 skills;")
    user = _user(factory, "ann@example.com")
    with tenant_session(factory, user.id) as s:
        profile = s.query(JobSeekerProfile).one()
        assert profile.headline == "My own headline"
        assert profile.skills == ["Excel", "SQL", "Power BI", "Stakeholder Management"]
        assert (profile.current_title, profile.city, profile.province, profile.country) == (
            "Senior Data Analyst", "Vancouver", "British Columbia", "Canada")
        assert profile.years_experience >= 9
        assert s.query(ProfileExperience).count() == 3 and s.query(ProfileEducation).count() == 2
    # The same file again changes nothing.
    again = http.post("/profile/import-linkedin", files=[("files", ("Profile.pdf", pdf, "application/pdf"))],
                      follow_redirects=False)
    assert "already has everything" in parse_qs(urlparse(again.headers["location"]).query)["msg"][0]
    with tenant_session(factory, user.id) as s:
        assert s.query(ProfileExperience).count() == 3


def test_linkedin_import_errors_and_access(env):
    http, _, _ = env
    assert http.post("/profile/import-linkedin", files=[("files", ("x.pdf", b"%PDF", "application/pdf"))],
                     follow_redirects=False).headers["location"].startswith("/login")
    _signup(http)
    bad = http.post("/profile/import-linkedin", files=[("files", ("cat.jpg", b"\xff\xd8\xff", "image/jpeg"))],
                    follow_redirects=False)
    assert "Upload the PDF of your LinkedIn profile" in parse_qs(urlparse(bad.headers["location"]).query)["error"][0]
    none = http.post("/profile/import-linkedin", follow_redirects=False)
    assert "Choose your LinkedIn" in parse_qs(urlparse(none.headers["location"]).query)["error"][0]
