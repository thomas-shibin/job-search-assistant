"""Hourly limits (new searches, resume actions) and the admin activity dashboard."""
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
from app.modules.usage.models import ActivityEvent, ActivityKind
from app.modules.usage.ai_calls import AICallLog
from app.modules.resumes.models import CandidateProfile
from app.modules.jobs.models import Job, JobStatus
from app.modules.auth.models import AuthSession, User
from app.modules.usage import quota
from app.core.tenancy import system_session, tenant_session

MEMBER, ADMIN, OTHER = 1, 2, 3
_signed_in: dict = {}


def _current(request: Request):
    """Whoever `_signed_in` names, loaded fresh (so admin changes show up)."""
    with system_session(_signed_in["factory"]) as s:
        user = s.get(User, _signed_in["id"])
        s.expunge(user)
    request.state.user = user
    return user


def _search_result(**extra):
    return {"error": None, "cards": [], "page": 1, "has_next": True, "failed": [], "queries": 1, "requests": 2,
            **extra}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        s.add(User(id=MEMBER, email="mia@example.com", first_name="Mia", last_name="Member", role="member",
                   email_verified_at=datetime(2026, 1, 1)))
        s.add(User(id=ADMIN, email="boss@example.com", first_name="Ada", last_name="Admin", role="admin",
                   email_verified_at=datetime(2026, 1, 1)))
        s.add(User(id=OTHER, email="otto@example.com", first_name="Otto", last_name="Other", role="member",
                   email_verified_at=datetime(2026, 1, 1)))
        s.commit()
    with tenant_session(factory, MEMBER) as s:
        s.add(CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True,
                               summary="Mia's very private resume summary", full_name="Mia Member"))
        s.add(Job(source="manual", source_url="https://e.com/1", company="Acme", title="Data Analyst",
                  description="Mia's private job description", status=JobStatus.DISCOVERED, fingerprint="fp",
                  description_hash="dh"))
        s.commit()

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    _signed_in.clear()
    _signed_in.update(id=MEMBER, factory=factory)
    signed_in = _signed_in

    import main
    from app.modules.auth.deps import current_user

    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = _current
    yield TestClient(main.app), factory, signed_in
    main.app.dependency_overrides.clear()


def _events(factory, user_id, kind=None):
    with system_session(factory) as s:
        q = s.query(ActivityEvent).filter(ActivityEvent.user_id == user_id)
        if kind:
            q = q.filter(ActivityEvent.kind == kind)
        return q.order_by(ActivityEvent.id).all()


def _age_events(factory, minutes: int):
    with system_session(factory) as s:
        for e in s.query(ActivityEvent):
            e.created_at = e.created_at - timedelta(minutes=minutes)
        s.commit()


@pytest.mark.real_limits
def test_one_new_search_per_hour_paging_and_repeats_free(world, monkeypatch):
    http, factory, _ = world
    import app.web.routes as ui_routes

    calls = []
    monkeypatch.setattr(ui_routes, "_search_for_page", lambda *a, **k: calls.append(k) or _search_result())

    with system_session(factory) as s:  # the strict one-search hour, whatever the platform default
        s.get(User, MEMBER).search_limit_per_hour = 1
        s.commit()
    first = http.get("/jobs?titles=Data+Analyst&locations=Toronto")
    assert first.status_code == 200 and "New searches this hour: 1 of 1" in first.text
    assert http.get("/jobs?titles=Data+Analyst&locations=Toronto&page=2").status_code == 200  # paging: free
    assert http.get("/jobs?titles=data+analyst&locations=toronto").status_code == 200  # same search again: free
    assert len(calls) == 3

    blocked = http.get("/jobs?titles=Nurse")
    assert "used your 1 new search for this hour" in blocked.text
    assert "Back to your last search" in blocked.text and "Data Analyst in Toronto" in blocked.text
    assert len(calls) == 3  # the provider was never asked

    searches = _events(factory, MEMBER, ActivityKind.SEARCH)
    assert len(searches) == 1 and searches[0].provider_calls == 2 and "Data Analyst" in searches[0].summary
    assert len(_events(factory, MEMBER, ActivityKind.SEARCH_MORE)) == 2  # the page-2 and repeat requests
    assert len(_events(factory, MEMBER, ActivityKind.LIMIT_HIT)) == 1

    _age_events(factory, 61)  # an hour later the window has moved on
    assert "used your 1 new search" not in http.get("/jobs?titles=Nurse").text
    assert len(calls) == 4


@pytest.mark.real_limits
def test_a_failed_search_is_given_back(world, monkeypatch):
    http, factory, _ = world
    import app.web.routes as ui_routes

    with system_session(factory) as s:
        s.get(User, MEMBER).search_limit_per_hour = 1
        s.commit()

    monkeypatch.setattr(ui_routes, "_search_for_page",
                        lambda *a, **k: _search_result(error="Job search is temporarily unavailable.", requests=0))
    http.get("/jobs?titles=Welder")
    assert _events(factory, MEMBER, ActivityKind.SEARCH) == []
    monkeypatch.setattr(ui_routes, "_search_for_page", lambda *a, **k: _search_result())
    assert "used your 1 new search" not in http.get("/jobs?titles=Plumber").text


@pytest.mark.real_limits
def test_paging_stops_at_the_platform_page_cap(world, monkeypatch):
    http, _, _ = world
    import app.web.routes as ui_routes

    monkeypatch.setattr(ui_routes, "_search_for_page", lambda *a, **k: _search_result())
    assert "Results stop at page 10" in http.get("/jobs?titles=Cook&page=11").text


@pytest.mark.real_limits
def test_admins_are_unlimited_and_overrides_apply(world, monkeypatch):
    http, factory, signed_in = world
    import app.web.routes as ui_routes

    monkeypatch.setattr(ui_routes, "_search_for_page", lambda *a, **k: _search_result())
    signed_in["id"] = ADMIN
    for title in ("A", "B", "C"):
        text = http.get(f"/jobs?titles={title}").text
        assert "used your" not in text and "New searches this hour" not in text
    assert _events(factory, ADMIN, ActivityKind.LIMIT_HIT) == []

    with system_session(factory) as s:
        s.get(User, OTHER).search_limit_per_hour = 3
        s.commit()
    signed_in["id"] = OTHER
    for title in ("A", "B", "C"):
        assert "used your" not in http.get(f"/jobs?titles={title}").text
    assert "used your 3 new searches" in http.get("/jobs?titles=D").text


@pytest.mark.real_limits
def test_resume_actions_limited_to_five_per_hour(world):
    http, factory, _ = world
    with tenant_session(factory, MEMBER) as s:
        job_id = s.query(Job).one().id
        for _ in range(5):
            s.add(ActivityEvent(kind=ActivityKind.AI_ANALYSIS, ai_calls=1))
        s.commit()

    for path in (f"/jobs/{job_id}/assess", f"/jobs/{job_id}/retailor"):
        response = http.post(path, follow_redirects=False)
        assert response.status_code == 303 and "resume+actions" in response.headers["location"], path
    response = http.post("/quick", data={"description": "Some job"})
    assert response.status_code == 429 and "used your 5 resume actions" in response.text
    assert len(_events(factory, MEMBER, ActivityKind.LIMIT_HIT)) == 2  # assess + retailor (quick checks first)
    assert "Resume actions left this hour: 0 of 5" in http.get(f"/jobs/{job_id}").text


def test_an_action_counts_only_when_it_made_a_paid_call(world):
    _, factory, _ = world
    with system_session(factory) as s:
        member = s.get(User, MEMBER)
        s.expunge(member)
    with tenant_session(factory, MEMBER) as s:
        with quota.ai_action(s, member, ActivityKind.AI_ANALYSIS, "Cached"):
            s.add(AICallLog(provider="p", operation="fit", cache_hit=True))
            s.commit()
        with quota.ai_action(s, member, ActivityKind.AI_TAILOR, "Paid"):
            s.add(AICallLog(provider="p", operation="tailor", cache_hit=False))
            s.add(AICallLog(provider="p", operation="tailor", cache_hit=False))
            s.commit()
    events = _events(factory, MEMBER)
    assert [(e.kind, e.summary, e.ai_calls) for e in events] == [(ActivityKind.AI_TAILOR, "Paid", 2)]


def test_dashboard_is_admin_only(world):
    http, _, signed_in = world
    for path in ("/admin/activity", f"/admin/users/{MEMBER}"):
        assert http.get(path).status_code == 404
    assert http.post(f"/admin/users/{OTHER}/limits", data={"search_limit": "50"}).status_code == 404
    assert http.post(f"/admin/users/{OTHER}/suspend").status_code == 404


def test_dashboard_shows_activity_but_never_resume_content(world, monkeypatch):
    http, factory, signed_in = world
    import app.web.routes as ui_routes

    monkeypatch.setattr(ui_routes, "_search_for_page", lambda *a, **k: _search_result())
    http.get("/jobs?titles=Data+Analyst&locations=Toronto")
    with tenant_session(factory, MEMBER) as s:
        s.add(AICallLog(provider="p", operation="assess_fit", cache_hit=False, estimated_cost=0.0123))
        s.add(ActivityEvent(kind=ActivityKind.AI_ANALYSIS, summary="Data Analyst at Acme", ai_calls=1))
        s.commit()

    signed_in["id"] = ADMIN
    for days in (7, 30, 90):
        page = http.get(f"/admin/activity?days={days}")
        assert page.status_code == 200
    page = http.get("/admin/activity?sort=ai_cost&q=mia")
    assert "Mia Member" in page.text and "Otto" not in page.text
    assert "<svg" in page.text and "Activity per day" in page.text

    detail = http.get(f"/admin/users/{MEMBER}")
    assert detail.status_code == 200
    assert "Data Analyst in Toronto" in detail.text and "Data Analyst at Acme" in detail.text
    assert "$0.0123" in detail.text and "Assess fit" in detail.text
    assert "private resume summary" not in detail.text and "private job description" not in detail.text
    assert "openai" not in detail.text.lower()


def test_admin_sets_limits_and_suspends(world):
    http, factory, signed_in = world
    signed_in["id"] = ADMIN
    with system_session(factory) as s:
        s.add(AuthSession(user_id=OTHER, token_hash="t" * 64, expires_at=datetime(2099, 1, 1)))
        s.commit()

    http.post(f"/admin/users/{OTHER}/limits", data={"search_limit": "10", "ai_limit": ""})
    bad = http.post(f"/admin/users/{OTHER}/limits", data={"search_limit": "-4", "ai_limit": ""})
    assert "whole numbers" in bad.text
    http.post(f"/admin/users/{OTHER}/suspend")
    refused = http.post(f"/admin/users/{ADMIN}/suspend")
    assert "your own roles or status" in refused.text

    with system_session(factory) as s:
        other = s.get(User, OTHER)
        assert other.search_limit_per_hour == 10 and other.ai_limit_per_hour is None
        assert other.is_active is False
        assert s.query(AuthSession).filter(AuthSession.user_id == OTHER).count() == 0
        assert s.get(User, ADMIN).is_active is True

    http.post(f"/admin/users/{OTHER}/reactivate")
    with system_session(factory) as s:
        assert s.get(User, OTHER).is_active is True


def test_members_never_see_each_others_activity(world):
    _, factory, _ = world
    with tenant_session(factory, OTHER) as s:
        s.add(ActivityEvent(kind=ActivityKind.SEARCH, summary="Otto's search"))
        s.commit()
    with tenant_session(factory, MEMBER) as s:
        assert s.query(ActivityEvent).filter(ActivityEvent.summary == "Otto's search").all() == []
