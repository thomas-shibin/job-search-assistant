"""Tests for the Greenhouse/Lever adapters (Milestone 12). Every request goes
through an `httpx.MockTransport` -- no real network call is made, so these
stay fast/deterministic/offline like the rest of the suite.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.sources.greenhouse import GreenhouseSource
from app.sources.lever import LeverSource


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
