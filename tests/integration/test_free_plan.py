"""The free plan: a lifetime allowance of resume actions per person, shared by
accounts found to be the same person, kept across account deletion; the
duplicate-accounts admin page; and the public plans page."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.database.base import Base
from app.core.database.session import get_session
from app.core.tenancy import system_session, tenant_session
from app.modules.auth.models import User
from app.modules.jobs.models import Job, JobStatus
from app.modules.resumes import uploads
from app.modules.usage import free_plan, plans, quota
from app.modules.usage.free_plan_models import AccountLink, FreeAccount, PersonMark
from app.modules.usage.models import ActivityEvent, ActivityKind

pytestmark = pytest.mark.real_limits  # the free plan's limit is what these test

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MEMBER, ADMIN, OTHER, THIRD = 1, 2, 3, 4
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
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        for uid, email, first, last, role in (
                (MEMBER, "mia.member@gmail.com", "Mia", "Member", "member"),
                (ADMIN, "boss@example.com", "Ada", "Admin", "admin"),
                (OTHER, "otto@example.com", "Otto", "Other", "member"),
                (THIRD, "someone@example.com", "Member", "Mia", "member")):
            s.add(User(id=uid, email=email, first_name=first, last_name=last, role=role,
                       email_verified_at=datetime(2026, 1, 1)))
        s.commit()

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    _signed_in.clear()
    _signed_in.update(id=MEMBER, factory=factory)

    import main
    from app.modules.auth.deps import current_user

    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = _current
    yield TestClient(main.app), factory, _signed_in
    main.app.dependency_overrides.clear()


def _user(factory, uid):
    with system_session(factory) as s:
        user = s.get(User, uid)
        s.expunge(user)
    return user


def _upload(factory, uid, path: Path):
    with tenant_session(factory, uid) as s:
        result, error = uploads.import_resume_file(s, path.name, path.read_bytes())
        assert error is None, error
        return result


def _standing(factory, uid):
    with tenant_session(factory, uid) as s:
        return free_plan.standing(s, _user(factory, uid), 10)


def _spend(factory, uid, times=1):
    with tenant_session(factory, uid) as s:
        for _ in range(times):
            free_plan.spend(s, _user(factory, uid))


# ---- marks ------------------------------------------------------------------------

def test_emails_phones_and_names_are_compared_the_way_people_vary_them():
    assert free_plan.norm_email("Mia.Member+jobs@GMAIL.com") == free_plan.norm_email("miamember@googlemail.com")
    assert free_plan.norm_email("mia+x@example.com") == "mia@example.com"
    assert free_plan.norm_email("m.ia@example.com") != free_plan.norm_email("mia@example.com")  # dots only at Gmail
    assert free_plan.norm_phone("+1 (604) 555-1234") == free_plan.norm_phone("604.555.1234") == "6045551234"
    assert free_plan.norm_phone("555-1234") is None
    assert free_plan.norm_name("Mia Member") == free_plan.norm_name("MEMBER, Mía")
    assert free_plan.norm_name("Mia") is None  # one word matches far too many people
    marks = free_plan.resume_marks("Mia Member\nmia.member+cv@gmail.com | (604) 555-1234\nExperience")
    assert {k for k, _ in marks} == {"email", "phone", "resume"}


def test_nothing_readable_is_stored(world):
    _, factory, _ = world
    _upload(factory, MEMBER, FIXTURES / "sample_resume.docx")
    with system_session(factory) as s:
        digests = [m.digest for m in s.query(PersonMark)]
    assert digests and all(len(d) == 64 and "@" not in d for d in digests)
    assert not any("member" in d or "6045551234" in d for d in digests)


# ---- one person, several accounts ---------------------------------------------------

def test_the_same_resume_on_two_accounts_shares_one_allowance(world):
    _, factory, _ = world
    _upload(factory, MEMBER, FIXTURES / "sample_resume.docx")
    _spend(factory, MEMBER, 7)
    _upload(factory, OTHER, FIXTURES / "sample_resume.docx")
    _spend(factory, OTHER, 2)

    for uid in (MEMBER, OTHER):
        standing = _standing(factory, uid)
        assert (standing.used, standing.accounts, standing.remaining) == (9, 2, 1)
    with system_session(factory) as s:
        link = s.query(AccountLink).one()
        assert link.status == free_plan.SHARED and "resume" in link.reasons


def test_a_name_alone_only_flags_the_pair_for_an_admin(world):
    _, factory, _ = world
    _spend(factory, MEMBER, 4)
    _spend(factory, THIRD, 1)  # "Member Mia": the same words as Mia Member
    with system_session(factory) as s:
        link = s.query(AccountLink).one()
        assert link.status == free_plan.REVIEW and link.reasons == ["name"]
    assert _standing(factory, THIRD).used == 1  # not shared until an admin says so


def test_admin_confirms_or_dismisses_a_link(world):
    http, factory, signed_in = world
    _spend(factory, MEMBER, 4)
    _spend(factory, THIRD, 1)
    with system_session(factory) as s:
        link_id = s.query(AccountLink).one().id

    assert http.get("/admin/duplicates").status_code == 404  # members can't
    assert http.post(f"/admin/duplicates/{link_id}", data={"decision": "same"}).status_code == 404

    signed_in["id"] = ADMIN
    page = http.get("/admin/duplicates")
    assert page.status_code == 200 and "Mia Member" in page.text and "same name" in page.text
    http.post(f"/admin/duplicates/{link_id}", data={"decision": "same"})
    assert _standing(factory, THIRD).used == 5
    assert "Resume actions" in http.get(f"/admin/users/{THIRD}").text

    http.post(f"/admin/duplicates/{link_id}", data={"decision": "different"})
    assert _standing(factory, THIRD).used == 1
    with system_session(factory) as s:
        assert s.get(AccountLink, link_id).reviewed_by == ADMIN
    assert "Nothing here" in http.get("/admin/duplicates").text  # nothing left to review


def test_a_dismissed_pair_stays_apart_when_more_matches_turn_up(world):
    _, factory, _ = world
    _spend(factory, MEMBER, 3)
    _spend(factory, THIRD, 1)
    with system_session(factory) as s:
        free_plan.review(s, s.query(AccountLink).one().id, same_person=False, admin=s.get(User, ADMIN))
        s.commit()
    _upload(factory, MEMBER, FIXTURES / "sample_resume.docx")
    _upload(factory, THIRD, FIXTURES / "sample_resume.docx")
    with system_session(factory) as s:
        link = s.query(AccountLink).one()
        assert link.status == free_plan.DISMISSED and "resume" in link.reasons
    assert _standing(factory, THIRD).used == 1


def test_deleting_the_account_and_signing_up_again_doesnt_start_over(world, monkeypatch):
    _, factory, _ = world
    from app.modules.auth import service as auth

    monkeypatch.setattr(auth, "delete_user_files", lambda user_id: None)
    _upload(factory, OTHER, FIXTURES / "sample_resume.pdf")
    _spend(factory, OTHER, 10)
    auth.delete_account(factory, OTHER)
    with system_session(factory) as s:
        retired = s.query(FreeAccount).one()
        assert retired.user_id is None and retired.deleted_at is not None and retired.ai_used == 10

    _upload(factory, MEMBER, FIXTURES / "sample_resume.pdf")  # a new account, the same resume
    standing = _standing(factory, MEMBER)
    assert standing.used == 10 and standing.exhausted


# ---- the limit itself ----------------------------------------------------------------

def test_resume_actions_stop_when_the_free_plan_is_used_up(world):
    http, factory, _ = world
    with tenant_session(factory, MEMBER) as s:
        # An account from before the free plan: its activity log counts toward it.
        assert s.query(FreeAccount).count() == 0
        for _ in range(10):
            s.add(ActivityEvent(kind=ActivityKind.AI_ANALYSIS, ai_calls=1, created_at=datetime(2026, 1, 1)))
        s.add(Job(source="manual", source_url="https://e.com/1", company="Acme", title="Analyst",
                  description="d", status=JobStatus.DISCOVERED, fingerprint="fp", description_hash="dh"))
        s.commit()
        job_id = s.query(Job).one().id
    _upload(factory, MEMBER, FIXTURES / "sample_resume.docx")

    response = http.post("/quick", data={"description": "Some job"})
    assert response.status_code == 429 and "included in the free plan" in response.text
    assert "Job search, saved jobs, tracking and the resume check stay free" in response.text
    detail = http.get(f"/jobs/{job_id}").text
    assert "Free plan</a>: 0 of 10 left" in detail
    response = http.post(f"/jobs/{job_id}/assess", follow_redirects=False)
    assert response.status_code == 303 and "free+plan" in response.headers["location"]


def test_admin_can_give_one_user_more(world):
    http, factory, signed_in = world
    _spend(factory, MEMBER, 10)
    signed_in["id"] = ADMIN
    http.post(f"/admin/users/{MEMBER}/limits", data={"search_limit": "", "ai_limit": "", "ai_total_limit": "25"})
    assert _user(factory, MEMBER).ai_total_limit == 25
    with tenant_session(factory, MEMBER) as s:
        quota.check_ai(s, _user(factory, MEMBER))  # no longer refused


def test_a_counted_action_spends_one(world):
    _, factory, _ = world
    from app.modules.usage.ai_calls import AICallLog

    member = _user(factory, MEMBER)
    with tenant_session(factory, MEMBER) as s:
        with quota.ai_action(s, member, ActivityKind.AI_TAILOR, "Paid"):
            s.add(AICallLog(provider="p", operation="tailor", cache_hit=False))
            s.commit()
        with quota.ai_action(s, member, ActivityKind.AI_ANALYSIS, "Cached"):
            s.add(AICallLog(provider="p", operation="fit", cache_hit=True))
            s.commit()
    assert _standing(factory, MEMBER).used == 1


# ---- the plans page ------------------------------------------------------------------

def test_plans_page_for_visitors_says_beta_and_lists_the_limits(world):
    http, _, signed_in = world
    signed_in["id"] = None
    page = http.get("/plans")
    assert page.status_code == 200
    text = page.text
    assert "Hirlumo is in beta" in text and "$0" in text and "10 in total" in text
    assert "Create a free account" in text and "Your plan" not in text
    assert "data-plans-next" not in text  # one plan: no arrows


def test_plans_page_shows_a_member_their_usage(world):
    http, factory, _ = world
    _spend(factory, MEMBER, 3)
    text = http.get("/plans").text
    assert "Your plan" in text and "3 of 10 used" in text and "7 left" in text


def test_a_second_plan_brings_the_arrows(world, monkeypatch):
    http, _, _ = world
    base = plans.all_plans

    def two(platform):
        free = base(platform)[0]
        return [free, plans.Plan(key="plus", name="Plus", price="$9", period="a month", summary="More.")]

    monkeypatch.setattr(plans, "all_plans", two)
    text = http.get("/plans").text
    assert "data-plans-prev" in text and "data-plans-next" in text and text.count("data-plans-dot=") == 2
    assert 'aria-label="Plus plan, 2 of 2"' in text


def test_the_key_is_made_once_and_only_its_owner_can_read_it(tmp_path):
    path = tmp_path / "free_plan.key"
    first = free_plan._load_key(path)
    assert path.stat().st_mode & 0o777 == 0o600 and len(first) == 32
    assert free_plan._load_key(path) == first


# ---- job-board searches ------------------------------------------------------------------

def _fake_search(monkeypatch, calls, requests=2):
    import app.modules.jobs.service as jobs_service

    def fake(*a, **k):
        calls.append(k)
        return {"error": None, "cards": [], "page": k.get("page", 1), "has_next": True, "failed": [],
                "queries": 1, "requests": requests if k.get("job_board", True) else 0}

    monkeypatch.setattr(jobs_service, "search_for_page", fake)


def test_ten_job_board_searches_then_company_sites_only(world, monkeypatch):
    http, factory, _ = world
    calls = []
    _fake_search(monkeypatch, calls)
    with system_session(factory) as s:
        s.get(User, MEMBER).search_limit_per_hour = 100  # the hour isn't what's tested here
        s.commit()
    for n in range(10):
        page = http.get(f"/jobs?titles=Role{n}")
        assert f"free plan</a>: {9 - n} of 10" in page.text
    assert all(k.get("job_board", True) for k in calls)

    page = http.get("/jobs?titles=Nurse")
    assert calls[-1]["job_board"] is False  # still searched, company careers sites only
    assert "used the 10 job-board searches included in the free plan" in page.text
    assert "hirlumo@gmail.com" in page.text and "0 of 10" in page.text
    http.get("/jobs?titles=Role9&page=2")  # more of a search already counted still reaches the boards
    assert calls[-1]["job_board"] is True
    assert _standing_of(factory, MEMBER, free_plan.SEARCH).used == 10


def test_answers_from_cache_and_failed_searches_dont_count(world, monkeypatch):
    http, factory, _ = world
    calls = []
    _fake_search(monkeypatch, calls, requests=0)
    http.get("/jobs?titles=Cached")
    assert _standing_of(factory, MEMBER, free_plan.SEARCH).used == 0


def test_job_board_searches_are_shared_by_one_persons_accounts(world, monkeypatch):
    _, factory, _ = world
    _upload(factory, MEMBER, FIXTURES / "sample_resume.docx")
    _upload(factory, OTHER, FIXTURES / "sample_resume.docx")
    with tenant_session(factory, MEMBER) as s:
        for _ in range(6):
            free_plan.spend(s, _user(factory, MEMBER), free_plan.SEARCH)
    with tenant_session(factory, OTHER) as s:
        for _ in range(4):
            free_plan.spend(s, _user(factory, OTHER), free_plan.SEARCH)
    assert _standing_of(factory, OTHER, free_plan.SEARCH).exhausted


def test_limit_messages_and_plans_page_give_the_contact_email(world):
    http, factory, signed_in = world
    _spend(factory, MEMBER, 10)
    text = http.get("/plans").text
    assert "Job-board searches" in text and "Resume actions" in text and "10 of 10 used" in text
    assert 'mailto:hirlumo@gmail.com?subject=Hirlumo%20limits' in text
    signed_in["id"] = None
    text = http.get("/plans").text
    assert "Job-board results" in text and "mailto:hirlumo@gmail.com?subject=Hirlumo%20feedback" in text
    assert "an hour" not in text.split("Job-board results")[1].split("</li>")[0]
    with tenant_session(factory, MEMBER) as s:
        with pytest.raises(quota.LimitReached, match="hirlumo@gmail.com"):
            quota.check_ai(s, _user(factory, MEMBER))


def _standing_of(factory, uid, kind):
    with tenant_session(factory, uid) as s:
        return free_plan.standing(s, _user(factory, uid), 10, kind)


class _ReadingProvider:
    last_usage = None

    def __init__(self):
        self.calls = []

    def interpret_search(self, text):
        self.calls.append(text)
        return None


def test_reading_a_search_counts_and_stops_when_the_allowance_is_used(world, monkeypatch):
    from app.core.config.schema import AIConfig
    from app.modules.jobs import interpret

    _, factory, _ = world
    provider = _ReadingProvider()
    monkeypatch.setattr(interpret, "user_config", lambda session: SimpleNamespace(ai=AIConfig(provider="openai")))
    monkeypatch.setattr(interpret, "build_ai_provider", lambda config, settings: provider)
    interpret._answers.clear()
    with tenant_session(factory, MEMBER) as s:
        interpret._ask_provider(s, _user(factory, MEMBER), "zorbly blarg in toronto")
    assert len(provider.calls) == 1 and _standing_of(factory, MEMBER, free_plan.AI).used == 1

    _spend(factory, MEMBER, 9)
    with tenant_session(factory, MEMBER) as s:
        assert interpret._ask_provider(s, _user(factory, MEMBER), "another odd search") is None
    assert len(provider.calls) == 1  # used up: the rules read it alone


class _ScanBoard:
    def __init__(self):
        self.queries = []

    def search_page(self, *, what, where, page, per_page, area=(), what_or=None, filters=None):
        self.queries.append(what)
        return []


def test_alert_scans_count_as_job_board_searches_and_make_no_paid_calls(world, monkeypatch, tmp_path):
    import app.workers.run as run_module
    from app.core.config.schema import AIConfig, AppConfig, CandidateConfig, GlobalSearchConfig

    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    _, factory, _ = world
    board = _ScanBoard()
    monkeypatch.setattr(run_module, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key"))
    monkeypatch.setattr(run_module, "AdzunaSource", lambda app_id, app_key, country: board)
    monkeypatch.setattr(run_module, "build_ai_provider", lambda *a: pytest.fail("no paid calls in a member's scan"))

    def config():
        return AppConfig(candidate=CandidateConfig(name="Mia Member", base_resume="x.docx"),
                         search=GlobalSearchConfig(titles=["Bookkeeper"], locations=["Toronto, ON"]),
                         ai=AIConfig(provider="openai", enabled=True, generate_match_explanations=True))

    with tenant_session(factory, MEMBER) as s:
        run_module._run(s, config(), desktop_notifications=False)
    assert board.queries and _standing_of(factory, MEMBER, free_plan.SEARCH).used == 1

    with tenant_session(factory, MEMBER) as s:
        for _ in range(9):
            free_plan.spend(s, _user(factory, MEMBER), free_plan.SEARCH)
    asked = len(board.queries)
    with tenant_session(factory, MEMBER) as s:
        run_module._run(s, config(), desktop_notifications=False)
    assert len(board.queries) == asked  # used up: the scan skips the job board
    assert _standing_of(factory, MEMBER, free_plan.SEARCH).used == 10
