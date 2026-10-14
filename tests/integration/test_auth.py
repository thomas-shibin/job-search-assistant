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
from app.core.tenancy import system_session
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
    for path in ("/jobs", "/resume", "/settings", "/watchlist", "/jobs/1", "/account", "/profile", "/saved",
                 "/applications", "/jobs/posting/p1"):
        response = http.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login"), path
    assert http.get("/api/jobs").status_code == 401
    assert http.get("/api/profile").status_code == 401
    assert http.get("/api/auth/me").json() == {"authenticated": False}
    for public in ("/", "/companies", "/resources", "/terms", "/privacy", "/login", "/signup"):
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
    ("short-pass", "at least 12"), ("Ann-Lee-12345!", "name or email"), ("Password-1234!", "common"),
    ("aaaaaaaaaaaaaaa", "different characters"),
])
def test_weak_passwords_are_rejected(env, password, problem):
    http, factory, _ = env
    response = _signup(http, password=password)
    assert response.status_code == 400 and problem in response.json()["error"]["fields"]["password"]
    assert _user(factory, "ann@example.com") is None


def test_long_passphrases_need_no_symbols_or_capitals(env):
    http, factory, _ = env
    assert _signup(http, password="correct horse battery staple").status_code == 200


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


def _social(monkeypatch, profile: ProviderProfile):
    import app.modules.auth.oauth as oauth

    monkeypatch.setattr(oauth.AuthSettings, "provider_enabled", lambda self, p: True)
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
