"""Tests for the Greenhouse/Lever/Adzuna adapters (Milestone 12). Every
request goes through an `httpx.MockTransport` -- no real network call is
made, so these stay fast/deterministic/offline like the rest of the suite.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.job_boards.adzuna import AdzunaSource
from app.integrations.job_boards.greenhouse import GreenhouseSource
from app.integrations.job_boards.lever import LeverSource


def _client_with_response(payload) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_with_status(status_code: int) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 12345,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "company_name": "Acme Corp",
            "location": {"name": "Remote - Canada"},
            "content": "<p>We need <b>Java</b> experience.</p><ul><li>5+ years</li></ul>",
            "updated_at": "2024-01-15T10:00:00Z",
        }
    ]
}


def test_greenhouse_source_parses_postings_into_raw_jobs():
    source = GreenhouseSource(board_token="acme", client=_client_with_response(GREENHOUSE_PAYLOAD))

    jobs = source.discover_jobs(search_profile=None)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "greenhouse"
    assert job.external_id == "12345"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme Corp"
    assert job.location == "Remote - Canada"
    assert "Java" in job.description
    assert "<p>" not in job.description
    assert job.date_posted is not None


def test_greenhouse_source_raises_on_http_error():
    source = GreenhouseSource(board_token="acme", client=_client_with_status(404))
    with pytest.raises(httpx.HTTPStatusError):
        source.discover_jobs(search_profile=None)


LEVER_PAYLOAD = [
    {
        "id": "abc-123",
        "text": "Staff Backend Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "categories": {"location": "Victoria, BC"},
        "descriptionPlain": "Build our platform.",
        "lists": [{"text": "Requirements", "content": "<ul><li>Java</li><li>AWS</li></ul>"}],
        "createdAt": 1700000000000,
    }
]


def test_lever_source_parses_postings_into_raw_jobs():
    source = LeverSource(company="acme", client=_client_with_response(LEVER_PAYLOAD))

    jobs = source.discover_jobs(search_profile=None)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "lever"
    assert job.external_id == "abc-123"
    assert job.title == "Staff Backend Engineer"
    assert job.company == "acme"
    assert job.location == "Victoria, BC"
    assert "Build our platform." in job.description
    assert "Java" in job.description
    assert job.date_posted is not None


ADZUNA_POSTING = {
    "id": 987654,
    "title": "Senior Backend Engineer",
    "company": {"display_name": "Acme Corp"},
    "location": {"display_name": "Victoria, BC"},
    "redirect_url": "https://www.adzuna.ca/land/ad/987654",
    "description": "We need <b>Java</b> experience.",
    "created": "2024-02-01T09:00:00Z",
}


def _adzuna_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_adzuna_source_parses_postings_into_raw_jobs():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [ADZUNA_POSTING]})

    search_profile = SimpleNamespace(titles=["Senior Backend Engineer"], locations=["Victoria, BC"])
    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(handler))

    jobs = source.discover_jobs(search_profile)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "adzuna"
    assert job.external_id == "987654"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme Corp"
    assert job.location == "Victoria, BC"
    assert "Java" in job.description
    assert "<b>" not in job.description
    assert job.date_posted is not None


def test_adzuna_source_sends_credentials_and_query_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": []})

    search_profile = SimpleNamespace(titles=["Backend Engineer"], locations=["Toronto, ON"])
    source = AdzunaSource(app_id="my-id", app_key="my-key", country="ca", client=_adzuna_client(handler))

    source.discover_jobs(search_profile)

    assert len(captured) == 1
    params = captured[0].url.params
    assert params["app_id"] == "my-id"
    assert params["app_key"] == "my-key"
    assert params["what"] == "Backend Engineer"
    assert params["where"] == "Toronto, ON"
    assert "/v1/api/jobs/ca/search/1" in str(captured[0].url)


def test_adzuna_source_queries_once_per_title_and_dedupes_by_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [ADZUNA_POSTING]})

    search_profile = SimpleNamespace(titles=["Backend Engineer", "Senior Backend Engineer"], locations=[])
    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(handler))

    jobs = source.discover_jobs(search_profile)

    # Same posting id surfaces under both title queries -- only counted once.
    assert len(jobs) == 1


def test_adzuna_source_falls_back_to_unfiltered_query_when_no_titles():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": []})

    search_profile = SimpleNamespace(titles=[], locations=[])
    source = AdzunaSource(app_id="id", app_key="key", country="us", client=_adzuna_client(handler))

    source.discover_jobs(search_profile)

    assert len(captured) == 1
    assert "what" not in captured[0].url.params
    assert "where" not in captured[0].url.params


def test_adzuna_source_search_page_requests_the_given_page_and_returns_raw_jobs():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"results": [ADZUNA_POSTING]})

    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(handler))

    jobs = source.search_page(what="Backend Engineer", where="Toronto, ON", page=3)

    assert len(jobs) == 1
    assert jobs[0].external_id == "987654"
    assert "/v1/api/jobs/ca/search/3" in str(captured[0].url)


def test_adzuna_source_search_page_returns_empty_list_past_last_page():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(handler))

    jobs = source.search_page(what="Backend Engineer", where=None, page=99)

    assert jobs == []


def test_adzuna_source_raises_on_http_error_without_leaking_the_key():
    from app.integrations.job_boards.adzuna import AdzunaError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    search_profile = SimpleNamespace(titles=["Backend Engineer"], locations=[])
    source = AdzunaSource(app_id="id", app_key="SECRET-KEY", country="ca", client=_adzuna_client(handler))

    with pytest.raises(AdzunaError) as exc_info:
        source.discover_jobs(search_profile)
    assert "404" in str(exc_info.value)
    assert "SECRET-KEY" not in str(exc_info.value)


def test_adzuna_source_retries_when_rate_limited(monkeypatch):
    import app.integrations.job_boards.adzuna as adzuna_module
    from app.shared.retry import with_retry

    monkeypatch.setattr(adzuna_module, "with_retry", lambda fn, **kw: with_retry(fn, **kw, sleep=lambda s: None))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429) if len(calls) < 3 else httpx.Response(200, json={"results": []})

    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(handler))

    assert source.search_page(what="Java", where=None) == []
    assert len(calls) == 3


def test_adzuna_source_retries_when_briefly_unavailable_then_says_so(monkeypatch):
    import app.integrations.job_boards.adzuna as adzuna_module
    from app.integrations.job_boards.adzuna import AdzunaError
    from app.shared.retry import with_retry

    monkeypatch.setattr(adzuna_module, "with_retry", lambda fn, **kw: with_retry(fn, **kw, sleep=lambda s: None))
    calls = []

    def recovers(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 2 else httpx.Response(200, json={"results": []})

    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(recovers))
    assert source.search_page(what="Human resource", where=None) == []
    assert len(calls) == 2

    down = AdzunaSource(app_id="id", app_key="SECRET-KEY", country="ca",
                        client=_adzuna_client(lambda request: httpx.Response(503)))
    with pytest.raises(AdzunaError) as exc_info:
        down.search_page(what="Human resource", where=None)
    assert "temporarily unavailable (HTTP 503)" in str(exc_info.value)
    assert "SECRET-KEY" not in str(exc_info.value)


def test_adzuna_keeps_salary_and_job_type():
    from app.integrations.job_boards.adzuna import AdzunaSource

    raw = AdzunaSource(app_id="i", app_key="k", country="ca").to_raw_job({
        "id": 1, "title": "Java Dev", "company": {"display_name": "Acme"}, "location": {"display_name": "Toronto"},
        "description": "d", "redirect_url": "https://x", "salary_min": 95000.0, "salary_max": 120000.4,
        "contract_time": "full_time", "contract_type": "permanent",
    })
    assert (raw.salary_min, raw.salary_max, raw.currency, raw.employment_type) == (95000, 120000, "CAD", "full_time permanent")


def test_adzuna_gives_up_quickly_when_someone_is_waiting(monkeypatch):
    import app.integrations.job_boards.adzuna as adzuna_module
    from app.integrations.job_boards.adzuna import AdzunaError
    from app.shared.retry import with_retry

    slept: list[float] = []
    monkeypatch.setattr(adzuna_module, "with_retry", lambda fn, **kw: with_retry(fn, **kw, sleep=slept.append))
    calls = []

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    source = AdzunaSource(app_id="id", app_key="key", country="ca", client=_adzuna_client(down), interactive=True)
    with pytest.raises(AdzunaError):
        source.search_page(what="Java", where=None)
    assert len(calls) == 2 and slept == [1.0]  # one quick retry, not 2 + 4 + 8 seconds


def test_page_search_stops_waiting_at_the_deadline():
    import threading
    import time

    from app.integrations.job_boards.base import RawJob
    from app.modules.jobs.search import search_jobs

    release = threading.Event()

    class _Board:
        def search_page(self, *, what, **_):
            if what == "Slow":
                release.wait(5)
                return []
            return [RawJob(external_id="1", source="adzuna", source_url="https://x", company="Acme", title="Fast",
                           location=None, description="")]

    started = time.monotonic()
    found = search_jobs(_Board(), ["Fast", "Slow"], [], deadline_seconds=0.3)
    release.set()
    assert time.monotonic() - started < 2
    assert [job.title for job in found.jobs] == ["Fast"]
    assert found.failed_queries == ["Slow"]


def test_smartrecruiters_reads_one_countrys_postings_and_only_new_descriptions():
    from app.integrations.job_boards.smartrecruiters import SmartRecruitersSource

    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path + ("?" + request.url.query.decode() if request.url.query else ""))
        if request.url.path.endswith("/postings"):
            return httpx.Response(200, json={"totalFound": 2, "content": [
                {"id": "11", "name": "Office Administrator ", "company": {"name": "Acme Foods"},
                 "releasedDate": "2026-10-20T10:00:00.000Z", "typeOfEmployment": {"label": "Full-time"},
                 "location": {"city": "Mississauga", "region": "ON", "country": "ca", "remote": False}},
                {"id": "12", "name": "Driver", "company": {"name": "Acme Foods"},
                 "location": {"city": "Laval", "region": "QC", "country": "ca", "remote": True}},
            ]})
        return httpx.Response(200, json={"postingUrl": "https://jobs.smartrecruiters.com/Acme/11-office-administrator",
                                         "jobAd": {"sections": {"jobDescription": {"text": "<p>Run the <b>office</b>.</p>"},
                                                                "qualifications": {"text": "<ul><li>2 years</li></ul>"}}}})

    source = SmartRecruitersSource("Acme", known={"12"}, client=httpx.Client(transport=httpx.MockTransport(handler)))
    jobs = source.discover_jobs(None)

    assert "country=ca" in asked[0]
    assert asked[1:] == ["/v1/companies/Acme/postings/11"]  # 12's description is already known
    first, second = jobs
    assert (first.title, first.company, first.location, first.employment_type) == (
        "Office Administrator", "Acme Foods", "Mississauga, ON", "Full-time")
    assert first.description == "Run the office.\n\n2 years"
    assert first.source_url == "https://jobs.smartrecruiters.com/Acme/11-office-administrator"
    assert second.description == "" and second.location == "Laval, QC (Remote)"
    assert second.source_url == "https://jobs.smartrecruiters.com/Acme/12"


def test_workable_reads_every_job_with_its_description():
    from app.integrations.job_boards.workable import WorkableSource

    payload = {"name": "Maple Builders", "jobs": [
        {"title": "Administrative Assistant / Receptionist", "shortcode": "958B", "employment_type": "Full-time",
         "telecommuting": False, "url": "https://apply.workable.com/j/958B", "published_on": "2026-07-14",
         "country": "Canada", "city": "Burnaby", "state": "British Columbia",
         "locations": [{"country": "Canada", "countryCode": "CA", "city": "Burnaby", "region": "British Columbia"}],
         "description": "<p>Greet visitors &amp; book meetings.</p>"},
    ]}
    jobs = WorkableSource("maple-builders", client=_client_with_response(payload)).discover_jobs(None)

    job = jobs[0]
    assert (job.source, job.external_id, job.company) == ("workable", "958B", "Maple Builders")
    assert job.location == "Burnaby, British Columbia, Canada"
    assert job.description == "Greet visitors & book meetings."
    assert job.date_posted.isoformat().startswith("2026-07-14")


def test_greenhouse_dates_a_job_by_when_it_was_first_published_not_its_last_edit():
    raw = GreenhouseSource("acme")._to_raw_job({"id": 1, "title": "T", "absolute_url": "u",
                                               "first_published": "2026-08-18T06:19:46-04:00",
                                               "updated_at": "2026-10-21T14:53:01-04:00"})
    assert raw.date_posted.date().isoformat() == "2026-08-18"
