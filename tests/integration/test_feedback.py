"""Members' feedback: the review card (asked once, "later" and "never"), the
Contact us page (kept and emailed with the member as Reply-To), and the admin
page that lists both."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.database.base import Base
from app.core.database.session import get_session
from app.core.tenancy import system_session
from app.integrations import email
from app.modules.auth.models import User
from app.modules.feedback import service
from app.modules.feedback.models import ContactMessage, Review

MEMBER, ADMIN, OTHER = 1, 2, 3
JSON = {"Accept": "application/json"}
_signed_in: dict = {}


def _current(request: Request):
    if _signed_in.get("id") is None:
        request.state.user = None
        return None
    with system_session(_signed_in["factory"]) as s:
        user = s.get(User, _signed_in["id"])
        s.expunge(user)
    request.state.user = user
    return user


@pytest.fixture()
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        for uid, mail, first, last, role in ((MEMBER, "mia@example.com", "Mia", "Member", "member"),
                                             (ADMIN, "boss@example.com", "Ada", "Admin", "admin"),
                                             (OTHER, "otto@example.com", "Otto", "Other", "member")):
            s.add(User(id=uid, email=mail, first_name=first, last_name=last, role=role,
                       email_verified_at=datetime(2026, 1, 1)))
        s.commit()

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    outbox: list = []
    monkeypatch.setattr(email, "outbox", outbox)
    service.contact_limiter.reset()
    _signed_in.clear()
    _signed_in.update(id=MEMBER, factory=factory)

    import main
    from app.modules.auth.deps import current_user

    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = _current
    yield TestClient(main.app), factory, outbox
    main.app.dependency_overrides.clear()


def _user(factory, uid) -> User:
    with system_session(factory) as s:
        user = s.get(User, uid)
        s.expunge(user)
        return user


def _auto(page: str) -> str | None:
    if 'id="review-card"' not in page:
        return None
    return page.split('id="review-card"')[1].split('data-auto="')[1][:1]


# ---- the review card ----------------------------------------------------------------

def test_a_new_member_is_asked_and_a_review_is_stored(world):
    http, factory, _ = world
    page = http.get("/plans").text
    assert _auto(page) == "1" and "Are you enjoying the jobs you find here?" in page
    assert 'data-minutes="5"' in page

    answer = http.post("/feedback", headers=JSON, data={
        "jobs_rating": "5", "enough_jobs_rating": "3", "resume_rating": "9", "features_rating": "",
        "comment": "  More remote jobs please ", "page": "/jobs"})
    assert answer.json() == {"ok": True}
    with system_session(factory) as s:
        review = s.query(Review).one()
        assert (review.user_id, review.jobs_rating, review.enough_jobs_rating) == (MEMBER, 5, 3)
        assert review.resume_rating is None and review.features_rating is None  # out of range / skipped
        assert review.comment == "More remote jobs please" and review.page == "/jobs"
    assert _user(factory, MEMBER).review_prompt == service.SENT
    assert _auto(http.get("/plans").text) == "0"  # never asked again; still in the menu
    assert "data-review-open" in http.get("/plans").text


def test_an_empty_review_is_refused(world):
    http, factory, _ = world
    answer = http.post("/feedback", headers=JSON, data={"comment": "   "})
    assert answer.status_code == 400 and "stars" in answer.json()["error"]
    with system_session(factory) as s:
        assert s.query(Review).count() == 0


def test_later_asks_again_after_a_week_and_never_stops_it(world):
    http, factory, _ = world
    assert http.post("/feedback/later", headers=JSON, data={}).json() == {"ok": True}
    user = _user(factory, MEMBER)
    assert user.review_prompt == service.LATER and _auto(http.get("/plans").text) == "0"
    assert service.review_due(user, now=datetime.utcnow() + timedelta(days=service.LATER_DAYS, minutes=1))

    http.post("/feedback/later", headers=JSON, data={"never": "1"})
    user = _user(factory, MEMBER)
    assert user.review_prompt == service.NEVER
    assert not service.review_due(user, now=datetime.utcnow() + timedelta(days=365))


def test_the_card_is_not_for_visitors_or_admin_pages(world):
    http, _, _ = world
    _signed_in["id"] = ADMIN
    assert _auto(http.get("/admin/feedback").text) == "0"
    assert _auto(http.get("/plans").text) == "1"
    _signed_in["id"] = None
    assert _auto(http.get("/plans").text) is None
    assert http.post("/feedback", data={"jobs_rating": "4"}, follow_redirects=False).status_code in (303, 401, 403)


# ---- Contact us ---------------------------------------------------------------------

def test_a_member_sends_a_message_from_their_account(world):
    http, factory, outbox = world
    page = http.get("/contact").text
    assert "Sending as" in page and "Mia Member" in page and "mia@example.com" in page

    sent = http.post("/contact", data={"topic": "limits", "title": "More searches",
                                       "message": "I'm applying to 30 jobs a week, could I get more?"},
                     follow_redirects=False)
    assert sent.status_code == 303 and sent.headers["location"] == "/contact?sent=1"
    assert "Message sent." in http.get("/contact?sent=1").text
    with system_session(factory) as s:
        saved = s.query(ContactMessage).one()
        assert (saved.user_id, saved.first_name, saved.last_name, saved.email) == (MEMBER, "Mia", "Member",
                                                                                  "mia@example.com")
        assert saved.topic == "limits" and saved.emailed and saved.handled_at is None
    mail = outbox[0]
    assert mail["To"] == "hirlumo@gmail.com" and mail["Reply-To"] == "Mia Member <mia@example.com>"
    assert "More searches" in mail["Subject"] and "30 jobs a week" in mail.get_content()
    assert "Higher limits" in mail.get_content()


def test_contact_messages_need_a_title_and_a_message_and_are_rate_limited(world):
    http, factory, outbox = world
    bad = http.post("/contact", data={"topic": "x", "title": " ", "message": "Long enough message"})
    assert bad.status_code == 400 and "Add a title" in bad.text
    short = http.post("/contact", data={"topic": "idea", "title": "Hi", "message": "short"})
    assert short.status_code == 400 and "a little more" in short.text and 'value="Hi"' in short.text
    for _ in range(5):
        http.post("/contact", data={"topic": "idea", "title": "Idea", "message": "A longer idea message"})
    limited = http.post("/contact", data={"topic": "idea", "title": "Idea", "message": "A longer idea message"})
    assert limited.status_code == 400 and "several messages" in limited.text
    assert len(outbox) == 5
    with system_session(factory) as s:
        assert s.query(ContactMessage).count() == 5


def test_visitors_are_asked_to_sign_in_or_email(world):
    http, _, _ = world
    _signed_in["id"] = None
    page = http.get("/contact").text
    assert "/login?next=/contact" in page and "mailto:hirlumo@gmail.com" in page and "Sending as" not in page
    assert "/contact" in http.get("/plans").text  # in the footer


# ---- admin -----------------------------------------------------------------------------

def test_admins_see_reviews_and_answer_messages(world):
    http, factory, _ = world
    http.post("/feedback", headers=JSON, data={"jobs_rating": "4", "comment": "Nice"})
    http.post("/contact", data={"topic": "problem", "title": "Upload fails", "message": "My PDF won't upload."})
    assert http.get("/admin/feedback").status_code == 404  # members can't

    _signed_in["id"] = ADMIN
    page = http.get("/admin/feedback").text
    assert "Nice" in page and "Mia Member" in page and "4.0" in page
    page = http.get("/admin/feedback?show=open").text
    assert "Upload fails" in page and "My PDF won" in page and "mailto:mia@example.com" in page
    with system_session(factory) as s:
        message_id = s.query(ContactMessage.id).scalar()
    http.post(f"/admin/feedback/messages/{message_id}", data={"handled": "1"})
    assert "Upload fails" not in http.get("/admin/feedback?show=open").text
    assert "Upload fails" in http.get("/admin/feedback?show=handled").text


def test_deleting_the_account_deletes_its_feedback(world):
    http, factory, _ = world
    from app.modules.auth.service import delete_user_data

    http.post("/feedback", headers=JSON, data={"jobs_rating": "2"})
    http.post("/contact", data={"topic": "other", "title": "Bye", "message": "Deleting my account now."})
    with system_session(factory) as s:
        delete_user_data(s, MEMBER)
        s.commit()
        assert s.query(Review).count() == 0 and s.query(ContactMessage).count() == 0
