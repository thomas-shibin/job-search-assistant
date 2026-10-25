"""The help knowledge base and assistant: every article is well formed and
links only to real pages, typical questions get the right answer (and
off-topic ones don't get a made-up one), and the Help centre, the chat
endpoint and the admin list of questions work."""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.core.config.models import AppSetting
from app.core.config.schema import PlatformConfig
from app.core.database.base import Base
from app.core.database.session import get_session
from app.core.tenancy import system_session
from app.modules.auth.models import User
from app.modules.help import knowledge, nlp, routes as help_routes
from app.modules.help.models import HelpQuestion

MEMBER, ADMIN = 1, 2
_signed_in: dict = {}


def _articles():
    return knowledge.articles(knowledge.values(PlatformConfig()))


# ---- the articles ------------------------------------------------------------------

def test_every_article_is_complete_and_names_no_outside_service():
    articles = _articles()
    assert len(articles) >= 14
    for article in articles:
        assert article.title and article.summary, article.slug
        assert [s for s in article.sections if s.heading], article.slug
        page = article.html
        assert "{{" not in page and "?:" not in page, article.slug
        text = (article.markdown + " ".join(" ".join(s.asked) for s in article.sections)).lower()
        for vendor in ("adzuna", "jooble", "openai", "chatgpt", "gpt-"):
            assert vendor not in text, (article.slug, vendor)


def test_every_link_in_the_help_goes_to_a_real_page():
    import json
    from pathlib import Path

    snapshot = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "routes_snapshot.json").read_text())
    templates = [r.split(" ", 1)[1] for r in snapshot if r.startswith("GET ")]
    patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", t) + "$") for t in templates]
    articles = {a.slug: a for a in _articles()}
    for article in articles.values():
        for target in re.findall(r"\]\((/[^)\s]*)\)", article.markdown):
            path, _, anchor = target.partition("#")
            assert any(p.match(path) for p in patterns), (article.slug, target)
            if path.startswith("/help/"):
                linked = articles.get(path.removeprefix("/help/"))
                assert linked is not None, (article.slug, target)
                assert not anchor or anchor in {s.anchor for s in linked.sections}, (article.slug, target)


def test_the_limits_quoted_are_the_live_settings():
    article = knowledge.find(knowledge.values(PlatformConfig(free_searches_total=7, free_ai_actions_total=3)),
                             "plans-and-limits")
    assert "7 job-board searches in total" in article.html and "3 resume actions in total" in article.html


def test_markdown_is_escaped_and_only_safe_links_are_made():
    out = knowledge.render("Hi <script>alert(1)</script> **bold** [ok](/help) [bad](javascript:alert(1))")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert '<a href="/help">ok</a>' in out and "javascript:" not in out and "<strong>bold</strong>" in out


# ---- understanding questions -------------------------------------------------------

@pytest.mark.parametrize("question, slug, heading", [
    ("how do i change my cv for a job", "tailored-resumes", "tailor my resume"),
    ("I found a job on indeed", "tailored-resumes", "Quick Tailor"),
    ("why only company jobs", "troubleshooting", "company careers sites only"),
    ("can I get more searches", "plans-and-limits", "Can I get more"),
    ("how many resume actions do i have", "plans-and-limits", "how much I have left"),
    ("what is a resume action", "plans-and-limits", "resume action"),
    ("my code never came", "troubleshooting", "code"),
    ("delete my account", "account-and-privacy", "delete my account"),
    ("how to connect telegram", "job-alerts", "Telegram"),
    ("set up email notifications for data analyst jobs in vancouver", "job-alerts", "set up a job alert"),
    ("resme chek for canada", "canadian-resume-check", "check my resume for Canada"),
    ("forgot password", "getting-started", "forgot my password"),
    ("upload pdf or word", "your-resume", "Word or PDF"),
    ("undo my changes", "your-resume", "undo"),
    ("what does must fix mean", "canadian-resume-check", "must fix"),
    ("how to mark applied", "tracking-applications", "applied"),
    ("jobs within 25km", "searching-for-jobs", "near a city"),
    ("minimum salary filter", "searching-for-jobs", "filter"),
    ("how do I get a better match score", "match-analysis", "improve my match"),
    ("can I use the app without an account", "getting-started", "need an account"),
    ("can you write my cover letter", "getting-started", "cover letters"),
    ("is there an app for iphone", "getting-started", "mobile app"),
    ("how long until you reply", "feedback-and-contact", "contact"),
    ("talk to a human", "feedback-and-contact", "contact"),
    ("dark mode", "account-and-privacy", "dark mode"),
    ("how does it work", "getting-started", "What can I do"),
])
def test_questions_get_the_right_answer(question, slug, heading):
    answer = nlp.reply(_articles(), question)
    assert answer.kind == "answer", question
    assert answer.source.article.slug == slug, (question, answer.source.article.slug, answer.source.heading)
    assert heading.lower() in answer.source.heading.lower(), (question, answer.source.heading)


@pytest.mark.parametrize("question", ["pizza recipe", "what's the weather in toronto", "salary negotiation tips",
                                      "tell me a joke about cats and dogs", "asdf qwer"])
def test_questions_the_help_doesnt_cover_are_not_guessed(question):
    answer = nlp.reply(_articles(), question)
    assert answer.kind == "fallback" and "/contact" in answer.text and answer.source is None


def test_greetings_broad_words_and_follow_ups():
    articles = _articles()
    assert nlp.reply(articles, "hello").kind == "greeting"
    assert nlp.reply(articles, "thanks!").kind == "thanks"
    broad = nlp.reply(articles, "resume")
    assert broad.kind == "clarify" and len(broad.suggestions) >= 3
    follow = nlp.reply(articles, "pause it", context="job-alerts")
    assert follow.source.article.slug == "job-alerts"


def test_words_are_read_the_same_way_in_questions_and_articles():
    assert nlp.terms("CVs") == nlp.terms("resumes") == nlp.terms("Résumé")
    assert nlp.terms("notifications") == nlp.terms("alerts")
    assert nlp.terms("saved") == nlp.terms("saving") == nlp.terms("save")


# ---- pages and the chat endpoint ---------------------------------------------------

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
def site(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        s.add(User(id=MEMBER, email="mia@example.com", first_name="Mia", last_name="Member", role="member",
                   email_verified_at=datetime(2026, 1, 1)))
        s.add(User(id=ADMIN, email="boss@example.com", first_name="Ada", last_name="Admin", role="admin",
                   email_verified_at=datetime(2026, 1, 1)))
        s.add(AppSetting(key="platform", value={"free_searches_total": 12}))
        s.commit()

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    _signed_in.clear()
    _signed_in.update(id=None, factory=factory)
    help_routes.help_limiter.reset()

    import main
    from app.modules.auth.deps import current_user

    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = _current
    yield TestClient(main.app), factory
    main.app.dependency_overrides.clear()


def test_the_help_centre_lists_topics_and_searches(site):
    http, factory = site
    page = http.get("/help")
    assert page.status_code == 200 and "How can we help?" in page.text and "/help/plans-and-limits" in page.text
    found = http.get("/help", params={"q": "tailor my resume"}).text
    assert "result" in found and "/help/tailored-resumes#how-do-i-tailor-my-resume-for-a-job" in found
    with system_session(factory) as s:
        logged = s.query(HelpQuestion).one()
        assert (logged.question, logged.channel, logged.answered) == ("tailor my resume", "search", True)


def test_an_article_page_shows_live_numbers_and_its_questions(site):
    http, _ = site
    page = http.get("/help/plans-and-limits").text
    assert "12 job-board searches in total" in page  # from the platform settings
    assert 'id="what-is-a-resume-action"' in page and "On this page" in page
    assert http.get("/help/no-such-article").status_code == 404
    _signed_in["id"] = MEMBER
    assert "Log Out" in http.get("/help/plans-and-limits").text  # the page knows who's signed in


def test_the_chat_answers_and_logs_without_who_asked(site):
    http, factory = site
    _signed_in["id"] = MEMBER
    answer = http.post("/help/ask", data={"question": "how do I pause an alert", "page": "/watchlist"}).json()
    assert answer["kind"] == "answer" and answer["source"]["slug"] == "job-alerts"
    assert answer["source"]["url"].startswith("/help/job-alerts#")
    missing = http.post("/help/ask", data={"question": "pizza recipe"}).json()
    assert missing["kind"] == "fallback" and missing["suggestions"]
    assert http.post("/help/ask", data={"question": "hi"}).json()["kind"] == "greeting"
    with system_session(factory) as s:
        rows = s.query(HelpQuestion).order_by(HelpQuestion.id).all()
        assert [(r.answered, r.signed_in, r.page) for r in rows] == [(True, True, "/watchlist"), (False, True, None)]
    assert not hasattr(HelpQuestion, "user_id")


def test_the_chat_suggests_questions_for_the_page_and_is_rate_limited(site):
    http, _ = site
    offered = http.get("/help/ask/suggestions", params={"page": "/plans"}).json()["suggestions"]
    assert offered and all(q.endswith("?") for q in offered) and "What does the free plan include?" in offered
    help_routes.help_limiter.limit = 2
    try:
        for _ in range(2):
            assert http.post("/help/ask", data={"question": "alerts"}).status_code == 200
        assert http.post("/help/ask", data={"question": "alerts"}).status_code == 429
    finally:
        help_routes.help_limiter.limit = 60


def test_the_assistant_is_on_every_page_for_everyone(site):
    http, _ = site
    for signed_in in (None, MEMBER):
        _signed_in["id"] = signed_in
        page = http.get("/plans").text
        assert 'id="help-chat"' in page and "data-help-toggle" in page and 'href="/help"' in page


def test_admins_see_what_people_ask(site):
    http, _ = site
    http.post("/help/ask", data={"question": "how do I tailor my resume"})
    http.post("/help/ask", data={"question": "pizza recipe"})
    _signed_in["id"] = MEMBER
    assert http.get("/admin/feedback?show=help").status_code == 404
    _signed_in["id"] = ADMIN
    page = http.get("/admin/feedback?show=help").text
    assert "pizza recipe" in page and "how do I tailor my resume" in page and "No answer" in page
    only = http.get("/admin/feedback?show=help&unanswered=1").text
    assert "pizza recipe" in only and "how do I tailor my resume" not in only


def test_the_sitemap_lists_the_help(site):
    http, _ = site
    sitemap = http.get("/sitemap.xml").text
    assert "/help</loc>" in sitemap and "/help/canadian-resume-check</loc>" in sitemap
