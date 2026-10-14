"""Tests for Telegram watchlist alerts: watch matching rules, the per-run alert
cap, and failure isolation. Telegram itself is faked -- no real HTTP.
"""
from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.core.config.schema import AppConfig, CandidateConfig, Watch, WatchlistConfig
from app.models.job import Job, JobStatus
from app.integrations.telegram import TelegramClient, TelegramError, TelegramSettings
from app.notifications.watchlist import WatchlistAlerter, build_watchlist_alerter, format_alert, watch_matches


def _job(**overrides) -> Job:
    fields = dict(
        id=7, source="greenhouse", source_url="https://example.com/jobs/7", company="Shopify Inc.",
        title="Senior Backend Engineer", location="Toronto, ON", remote_type=None,
        description="We use Java, Spring Boot and C++ daily.", status=JobStatus.DISCOVERED,
        fingerprint="fp", description_hash="dh",
    )
    fields.update(overrides)
    return Job(**fields)


class _FakeClient:
    def __init__(self, fail: bool = False):
        self.messages: list[tuple[str, str]] = []
        self._fail = fail

    def send_message(self, chat_id: str, text: str) -> None:
        if self._fail:
            raise TelegramError("Telegram sendMessage failed: 403 Forbidden")
        self.messages.append((chat_id, text))


def test_company_match_is_case_insensitive_substring():
    assert watch_matches(Watch(name="w", companies=["shopify"]), _job())
    assert not watch_matches(Watch(name="w", companies=["Stripe"]), _job())


def test_fields_are_anded_and_lists_are_ored():
    watch = Watch(name="w", companies=["Stripe", "Shopify"], locations=["Vancouver"])
    assert not watch_matches(watch, _job())
    assert watch_matches(watch, _job(location="Vancouver, BC"))


def test_keywords_match_whole_words_including_symbols():
    assert watch_matches(Watch(name="w", keywords=["spring boot"]), _job())
    assert watch_matches(Watch(name="w", keywords=["C++"]), _job())
    assert not watch_matches(Watch(name="w", keywords=["Jav"]), _job())


def test_exclude_keywords_veto_a_match():
    assert not watch_matches(Watch(name="w", companies=["Shopify"], exclude_keywords=["java"]), _job())


def test_remote_location_watch_matches_remote_type():
    watch = Watch(name="w", locations=["Remote"])
    assert watch_matches(watch, _job(location="Canada", remote_type="remote"))
    assert not watch_matches(watch, _job(location="Canada", remote_type="hybrid"))


def test_watch_without_criteria_is_rejected():
    with pytest.raises(ValidationError):
        Watch(name="everything")


def test_format_alert_escapes_html_and_includes_links():
    text = format_alert(_job(title="C++ <Lead>"), ["Dream"], 88, "http://127.0.0.1:8001/")
    assert "C++ &lt;Lead&gt;" in text
    assert "88/100" in text
    assert 'href="https://example.com/jobs/7"' in text
    assert 'href="http://127.0.0.1:8001/jobs/7"' in text


def test_alerter_sends_only_matching_jobs():
    client = _FakeClient()
    alerter = WatchlistAlerter(WatchlistConfig(watches=[Watch(name="Dream", companies=["Shopify"])]), client, "42")

    alerter.consider(_job())
    alerter.consider(_job(company="Other Co"))

    assert len(client.messages) == 1
    assert client.messages[0][0] == "42"
    assert "Dream" in client.messages[0][1]


def test_alerter_caps_alerts_and_rolls_up_the_rest():
    client = _FakeClient()
    config = WatchlistConfig(max_alerts_per_run=2, watches=[Watch(name="w", companies=["Shopify"])])
    alerter = WatchlistAlerter(config, client, "42")

    for i in range(5):
        alerter.consider(_job(id=i, title=f"Role {i}"))
    alerter.flush()

    assert len(client.messages) == 3
    assert "3 more watchlist matches" in client.messages[-1][1]


def test_alerter_swallows_telegram_failures():
    alerter = WatchlistAlerter(WatchlistConfig(watches=[Watch(name="w", companies=["Shopify"])]), _FakeClient(fail=True), "42")
    alerter.consider(_job())  # must not raise


def _config(enabled: bool) -> AppConfig:
    return AppConfig(
        candidate=CandidateConfig(name="X", base_resume="x.docx"),
        watchlist=WatchlistConfig(enabled=enabled, watches=[Watch(name="w", companies=["Shopify"])]),
    )


def test_build_alerter_requires_enabled_token_and_the_users_own_chat():
    creds = TelegramSettings(TELEGRAM_BOT_TOKEN="t")
    linked = _config(True)
    linked.watchlist.telegram_chat_id = "42"
    assert build_watchlist_alerter(_config(False), creds) is None
    assert build_watchlist_alerter(linked, TelegramSettings(TELEGRAM_BOT_TOKEN=None)) is None
    assert build_watchlist_alerter(_config(True), creds) is None  # no linked chat: no shared fallback
    assert build_watchlist_alerter(linked, creds) is not None


def test_telegram_client_raises_on_api_error_without_leaking_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    client = TelegramClient("SECRET-TOKEN", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TelegramError) as exc_info:
        client.send_message("42", "hi")
    assert "SECRET-TOKEN" not in str(exc_info.value)


def test_telegram_recent_chats_dedupes_by_chat_id():
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "username": "me"}}},
        {"update_id": 2, "message": {"chat": {"id": 42, "type": "private", "username": "me"}}},
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "result": updates}))
    client = TelegramClient("t", client=httpx.Client(transport=transport))
    assert [chat["id"] for chat in client.recent_chats()] == [42]


# ---- Lean watches + advanced filters ----------------------------------------

from datetime import datetime, timedelta, timezone

from app.core.config.schema import Watch as _Watch
from app.models.job import Job as _Job
from app.notifications.watchlist import watch_matches as _matches


def _lean_job(**overrides):
    fields = dict(title="Senior Java Developer", company="Acme", location="Victoria, Capital",
                  description="Java and Kafka services.", source_url="https://x", remote_type=None,
                  salary_min=None, salary_max=None, employment_type=None, date_posted=None)
    fields.update(overrides)
    return _Job(**fields)


def test_locations_match_by_city_or_province_like_the_job_search():
    watch = _Watch(name="BC", locations=["Victoria, BC", "Ontario"])
    assert _matches(watch, _lean_job(location="Victoria, Capital"))                         # Adzuna's spelling
    assert _matches(watch, _lean_job(location="Mississauga, Peel region"), job_province="ON")
    assert _matches(watch, _lean_job(location="Toronto, Ontario"))
    assert not _matches(watch, _lean_job(location="Vancouver, Greater Vancouver"), job_province="BC")
    assert _matches(_Watch(name="r", locations=["Remote"]), _lean_job(location="Canada", remote_type="remote"))


def test_minimum_score_gates_alerts():
    watch = _Watch(name="good fits", keywords=["Java"], min_score=80)
    assert _matches(watch, _lean_job(), score=85)
    assert not _matches(watch, _lean_job(), score=79)
    assert not _matches(watch, _lean_job(), score=None)  # can't tell -> no alert


def test_salary_job_type_age_and_company_filters():
    watch = _Watch(name="w", keywords=["Java"], min_salary=120000, max_salary=200000, job_types=["contract"],
                   posted_within_days=7, exclude_companies=["Staffing Co"])
    now = datetime.now(timezone.utc)
    assert _matches(watch, _lean_job(salary_min=130000, salary_max=150000, employment_type="full_time contract",
                                date_posted=now - timedelta(days=2)))
    assert _matches(watch, _lean_job())                                               # unknown salary/type/date pass
    assert not _matches(watch, _lean_job(salary_max=100000))                          # too low
    assert not _matches(watch, _lean_job(salary_min=250000))                          # too high
    assert not _matches(watch, _lean_job(employment_type="full_time permanent"))      # wrong type
    assert not _matches(watch, _lean_job(date_posted=now - timedelta(days=10)))       # too old
    assert not _matches(watch, _lean_job(company="Staffing Co Inc."))                 # excluded company


def test_watch_validation():
    import pytest

    with pytest.raises(ValueError):
        _Watch(name="empty")
    with pytest.raises(ValueError):
        _Watch(name="x", keywords=["Java"], min_salary=200, max_salary=100)
    with pytest.raises(ValueError):
        _Watch(name="x", keywords=["Java"], job_types=["freelance"])
    assert _Watch(name="just places", locations=["Vancouver, BC"]).search  # lean: locations alone is enough


def test_watches_become_scheduled_searches_with_adzuna_filters():
    from types import SimpleNamespace

    from app.services.search import scan_specs, watch_filters

    watch = _Watch(name="BC contracts", locations=["Victoria, BC", "Vancouver, BC"], keywords=["Java"],
                   min_salary=100000, job_types=["contract"], posted_within_days=3, exclude_keywords=["PHP"])
    assert watch_filters(watch) == {"salary_min": "100000", "max_days_old": "3", "what_exclude": "PHP", "contract": "1"}
    assert "full_time" not in watch_filters(_Watch(name="x", keywords=["a"], job_types=["full_time", "part_time"]))

    config = SimpleNamespace(
        search=SimpleNamespace(scan_in_background=True, titles=["Java Developer"], locations=["Victoria, BC"], keywords=[]),
        watchlist=SimpleNamespace(watches=[
            watch,
            _Watch(name="companies only", companies=["Shopify"]),                   # nothing to search by
            _Watch(name="off", locations=["Toronto, ON"], search=False),
        ]),
    )
    specs = scan_specs(config)
    assert [(s.title, s.location, s.label) for s in specs] == [
        ("Java Developer", "Victoria, BC", "job search"),
        (None, "Victoria, BC", "watch: BC contracts"),
        (None, "Vancouver, BC", "watch: BC contracts"),
    ]
    assert dict(specs[1].filters)["contract"] == "1" and specs[1].keywords == ("Java",)
