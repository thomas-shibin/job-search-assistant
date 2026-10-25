"""Smart search end to end (app/modules/jobs/interpret.py and the /jobs route): a
typed search is read into titles, places and filters, the analysis provider
is asked only when the rules can't read it, and searches nobody could read
don't use up the hourly allowance. The job board and the provider are faked.
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
from app.integrations.ai.base import SearchInterpretation
from app.integrations.ai.usage import AIUsage
from app.integrations.job_boards.base import RawJob
from app.modules.access.models import FeatureFlag
from app.modules.jobs.catalog.models import UserSearchTerm
from app.modules.jobs.catalog.store import seed_catalog
from app.modules.jobs.search import clear_cache
from app.modules.usage.ai_calls import AICallLog
from app.modules.usage.models import ActivityEvent, ActivityKind

ASKED: list[str | None] = []
FOUND: dict[str, list[RawJob]] = {}


class _FakeBoard:
    down = False

    def __init__(self, app_id, app_key, country, **_):
        pass

    def search_page(self, *, what, where, page, per_page=50, area=(), what_or=None, filters=None):
        ASKED.append(what)
        if _FakeBoard.down:
            raise RuntimeError("HTTP 503")
        return FOUND.get(what or "", []) if page == 1 else []


class _FakeProvider:
    def __init__(self, answer: SearchInterpretation | None = None, error: Exception | None = None):
        self.answer, self.error, self.calls = answer, error, []
        self.last_usage = AIUsage(tokens_in=600, tokens_out=80, estimated_cost=0.0002)

    def interpret_search(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.answer


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
    import app.modules.jobs.interpret as interpret
    import app.modules.jobs.service as jobs_service
    import main

    config = AppConfig(candidate=CandidateConfig(name="Jordan Casey", base_resume="x.docx"),
                       search=GlobalSearchConfig(titles=["Backend Engineer"]), ai=AIConfig(provider="openai"))
    monkeypatch.setattr(current_config, "load_app_config", lambda session, path: config)
    monkeypatch.setattr(jobs_service, "AdzunaSettings", lambda: SimpleNamespace(app_id="id", app_key="key"))
    monkeypatch.setattr(jobs_service, "AdzunaSource", _FakeBoard)
    provider = _FakeProvider()
    monkeypatch.setattr(interpret, "build_ai_provider", lambda config, settings: provider)
    ASKED.clear()
    FOUND.clear()
    _FakeBoard.down = False
    clear_cache()
    with SessionLocal() as s:
        seed_catalog(s)
    main.app.dependency_overrides[get_session] = override_get_session
    yield SimpleNamespace(http=TestClient(main.app), db=SessionLocal, provider=provider, config=config)
    main.app.dependency_overrides.pop(get_session, None)


def _redirect(response) -> dict[str, list[str]]:
    assert response.status_code == 303, response.text[:300]
    url = urlsplit(response.headers["location"])
    assert url.path == "/jobs"
    return parse_qs(url.query, keep_blank_values=True)


def _counted_searches(site) -> int:
    with site.db() as s:
        return s.query(ActivityEvent).filter(ActivityEvent.kind == ActivityKind.SEARCH).count()


def test_a_sentence_is_read_into_the_search_and_its_filters(site):
    response = site.http.get("/jobs", params={"q": "remote recruiter jobs over 80k in Toronto posted this week"},
                             follow_redirects=False)

    params = _redirect(response)
    assert params["q"] == ["recruiter"]
    assert params["l"] == ["Toronto, ON"]
    assert params["workplace"] == ["remote"]
    assert params["salary"] == ["80000"]
    assert params["days"] == ["7"]
    assert params["exact"] == ["1"]
    assert params["typed"] == ["remote recruiter jobs over 80k in Toronto posted this week"]
    assert ASKED == []  # nothing searched until the browser asks for the results
    assert site.provider.calls == []  # the rules read it: no paid call

    page = site.http.get(response.headers["location"])
    assert page.status_code == 200
    assert ASKED and all(what == "recruiter" for what in ASKED)
    assert "Showing results for &ldquo;remote recruiter jobs over 80k in Toronto posted this week&rdquo;" in page.text
    assert 'class="search-chip search-chip-title"' in page.text and "$80K+" in page.text
    assert "Search for the exact words instead" in page.text


def test_plain_titles_search_straight_away(site):
    response = site.http.get("/jobs", params={"q": "Data Analyst"}, follow_redirects=False)

    assert response.status_code == 200
    assert ASKED and ASKED[0] == "Data Analyst"
    assert "Showing results for" not in response.text
    # Results links never read the search again.
    assert "exact=1" in response.text


def test_spelling_is_corrected_and_shown(site):
    params = _redirect(site.http.get("/jobs", params={"q": "sofware engineer"}, follow_redirects=False))

    assert params["q"] == ["software engineer"]
    assert params["fixed"] == ["sofware>software"]
    page = site.http.get("/jobs", params={k: v for k, v in params.items()})
    assert "<s>sofware</s> &rarr; <strong>software</strong>" in page.text


def test_exact_words_and_the_feature_switch_skip_reading(site):
    assert site.http.get("/jobs", params={"q": "remote recruiter", "exact": 1}, follow_redirects=False).status_code == 200
    assert ASKED[-1] == "remote recruiter"

    with site.db() as s:
        flag = s.query(FeatureFlag).filter_by(feature_key="smart_search").one_or_none()
        if flag is None:
            flag = FeatureFlag(feature_key="smart_search", name="Smart search", description="")
            s.add(flag)
        flag.enabled = False
        s.commit()
    assert site.http.get("/jobs", params={"q": "remote recruiter in Toronto"}, follow_redirects=False).status_code == 200
    assert ASKED[-1] == "remote recruiter in Toronto"


def test_the_provider_reads_what_the_rules_cannot(site):
    site.provider.answer = SearchInterpretation(titles=["Bookkeeper", "Accounts Payable Clerk"],
                                                locations=["Toronto"], workplace="hybrid")

    params = _redirect(site.http.get("/jobs", params={"q": "someone to do spreadsheets and invoices"},
                                     follow_redirects=False))

    assert params["q"] == ["Bookkeeper, Accounts Payable Clerk"]
    assert params["l"] == ["Toronto, ON"]  # the provider's place, matched to the catalog's
    assert params["workplace"] == ["hybrid"]
    with site.db() as s:
        logged = s.query(AICallLog).filter(AICallLog.operation == "search_interpret").all()
    assert len(logged) == 1 and logged[0].tokens_in == 600

    # The same words again: answered from memory, no second call.
    _redirect(site.http.get("/jobs", params={"q": "someone to do spreadsheets and invoices"}, follow_redirects=False))
    assert len(site.provider.calls) == 1


def test_not_a_job_search_runs_nothing_and_costs_no_search(site):
    site.provider.answer = SearchInterpretation(is_job_search=False)

    page = site.http.get("/jobs", params={"q": "asdkjh qwe"})

    assert page.status_code == 200
    assert "We couldn&#39;t tell what job you&#39;re looking for" in page.text
    assert ASKED == []
    assert _counted_searches(site) == 0


def test_without_the_provider_unknown_words_are_searched_but_free_if_nothing_is_found(site):
    site.config.ai.search_assist = False

    page = site.http.get("/jobs", params={"q": "asdkjh qwe"}, follow_redirects=False)

    assert page.status_code == 200
    assert ASKED[0] == "asdkjh qwe"  # searched as typed, as before smart search
    assert site.provider.calls == []
    assert _counted_searches(site) == 0  # found nothing: given back
    with site.db() as s:
        assert s.query(UserSearchTerm).count() == 0  # and not kept as a suggestion


def test_unknown_words_that_find_jobs_count_as_usual(site):
    site.config.ai.search_assist = False
    FOUND["sommelier"] = [RawJob(external_id="1", source="adzuna", source_url="https://example.com/1",
                                 company="Wine Bar", title="Sommelier", location="Toronto, ON", description="Wine.")]

    site.http.get("/jobs", params={"q": "sommelier"})

    assert _counted_searches(site) == 1
    with site.db() as s:
        assert [t.label for t in s.query(UserSearchTerm)] == ["sommelier"]


def test_a_failing_provider_falls_back_to_the_rules(site):
    site.provider.error = RuntimeError("timeout")

    page = site.http.get("/jobs", params={"q": "spreadsheets"}, follow_redirects=False)

    assert page.status_code == 200
    assert ASKED[0] == "spreadsheets"
    with site.db() as s:
        assert s.query(AICallLog).count() == 0


def test_provider_calls_per_member_are_capped(site, monkeypatch):
    import app.modules.jobs.interpret as interpret

    monkeypatch.setattr(interpret, "AI_READS_PER_HOUR", 1)
    site.provider.answer = SearchInterpretation(titles=["Bookkeeper"])

    site.http.get("/jobs", params={"q": "spreadsheets"}, follow_redirects=False)
    site.http.get("/jobs", params={"q": "invoices"}, follow_redirects=False)

    assert site.provider.calls == ["spreadsheets"]


def _board_job(n: int, title: str, description: str = "") -> RawJob:
    return RawJob(external_id=str(n), source="adzuna", source_url=f"https://example.com/{n}", company=f"Co {n}",
                  title=title, location="Toronto, ON", description=description)


def test_only_jobs_whose_title_matches_are_shown(site):
    FOUND["hr generalist"] = [
        _board_job(1, "HR Generalist"),
        _board_job(2, "Plumber", "Questions? Contact our HR generalist."),  # the board matches descriptions
        _board_job(3, "Site Supervisor", "Our HR generalist will call you."),
    ]

    page = site.http.get("/jobs", params={"q": "hr generalist", "exact": 1})

    assert "HR Generalist" in page.text
    assert "Plumber" not in page.text and "Site Supervisor" not in page.text
    assert "2 less relevant hidden" in page.text


def test_paging_stops_once_the_board_runs_out_of_relevant_jobs(site):
    from app.modules.jobs.service import CARDS_PER_PAGE

    FOUND["hr generalist"] = [_board_job(n, "Plumber", "Ask our HR generalist.") for n in range(CARDS_PER_PAGE)]
    page = site.http.get("/jobs", params={"q": "hr generalist", "exact": 1})
    assert "No jobs match \u201chr generalist\u201d closely" in page.text
    assert "Next" not in page.text

    clear_cache()
    FOUND["hr generalist"][0] = _board_job(0, "HR Generalist")
    page = site.http.get("/jobs", params={"q": "hr generalist", "exact": 1})
    assert "Next" in page.text  # this page still had a match: there may be more


@pytest.mark.real_limits
def test_when_the_job_board_is_down_the_page_says_so_and_the_search_is_free(site):
    _FakeBoard.down = True

    page = site.http.get("/jobs", params={"q": "Human resource", "l": "Vancouver, BC", "exact": 1})

    assert "The job board isn't responding right now, so we couldn't search it" in page.text
    assert "this search wasn't counted" in page.text
    assert "No jobs found" not in page.text and "No jobs match" not in page.text
    assert _counted_searches(site) == 0


class _FakeSecondBoard:
    asked: list[tuple[str, str | None]] = []

    def __init__(self, api_key, base_url):
        pass

    def search(self, keywords, location=None, page=1):
        _FakeSecondBoard.asked.append((keywords, location))
        job = RawJob(source="jooble", external_id="j1", source_url="https://example.org/j1", company="Maple Dental",
                     title="Human Resources Coordinator", location="Vancouver, BC", description="A short preview.")
        return [job], 1


def test_when_the_job_board_is_down_the_second_board_answers_and_stands_in_for_a_while(site, monkeypatch):
    from app.modules.jobs import service as jobs_service

    monkeypatch.setattr(jobs_service, "JoobleSettings", lambda: SimpleNamespace(api_key="key", url="https://x/"))
    monkeypatch.setattr(jobs_service, "JoobleSource", _FakeSecondBoard)
    _FakeSecondBoard.asked = []
    _FakeBoard.down = True

    page = site.http.get("/jobs", params={"q": "Human resource", "l": "Vancouver, BC", "exact": 1})

    assert "Human Resources Coordinator" in page.text and "Maple Dental" in page.text
    assert "isn't responding" not in page.text
    assert _FakeSecondBoard.asked == [("Human resource", "Vancouver, BC")]
    asked_first = len(ASKED)
    site.http.get("/jobs", params={"q": "Recruiter", "l": "Vancouver, BC", "exact": 1})
    assert len(ASKED) == asked_first  # the first board is skipped while it's paused
    assert len(_FakeSecondBoard.asked) == 2


def test_members_searches_stop_asking_the_board_when_todays_share_is_used(site, monkeypatch):
    from app.modules.jobs import service as jobs_service

    FOUND["Recruiter"] = [RawJob(source="adzuna", external_id="a1", source_url="https://example.org/a1",
                                 company="Acme", title="Recruiter", location="Vancouver, BC", description="x")]
    site.http.get("/jobs", params={"q": "Recruiter", "l": "Vancouver, BC", "exact": 1})
    assert ASKED == ["Recruiter"]
    with site.db() as s:  # members' searches have made 100 requests today: their share of the default 250
        s.query(ActivityEvent).update({ActivityEvent.provider_calls: 100})
        s.commit()
    jobs_service.reset_board_pause()  # count again now

    monkeypatch.setattr(jobs_service, "JoobleSettings", lambda: SimpleNamespace(api_key="key", url="https://x/"))
    monkeypatch.setattr(jobs_service, "JoobleSource", _FakeSecondBoard)
    _FakeSecondBoard.asked = []
    page = site.http.get("/jobs", params={"q": "Human resource", "l": "Vancouver, BC", "exact": 1})

    assert ASKED == ["Recruiter"]  # the first board wasn't asked
    assert "Human Resources Coordinator" in page.text  # the second board answered
    assert jobs_service.board_budget(site.db()) == (100, 100)
