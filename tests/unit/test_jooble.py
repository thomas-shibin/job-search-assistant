"""The second job board's adapter (app/integrations/job_boards/jooble.py),
against a mock transport: no network, and the key never appears in errors."""
from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.job_boards.jooble import JoobleError, JoobleSource

PAYLOAD = {"totalCount": 42, "jobs": [
    {"id": -123, "title": "<b>Administrative</b> Assistant", "company": "Maple Dental ", "location": "Toronto, ON",
     "snippet": "&nbsp;Answer phones and <b>book</b> patients...", "salary": "$22 per hour", "type": "Full-time",
     "link": "https://jooble.org/desc/-123", "updated": "2026-10-20T09:15:00.0000000"},
    {"id": 5, "title": "No link", "link": ""},
]}


def _source(handler) -> JoobleSource:
    return JoobleSource("secret-key", "https://jooble.example/api/", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_search_posts_the_keywords_and_reads_the_postings():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, url=str(request.url), body=json.loads(request.content))
        return httpx.Response(200, json=PAYLOAD)

    jobs, total = _source(handler).search("administrator", "Toronto", page=2)

    assert seen == {"method": "POST", "url": "https://jooble.example/api/secret-key",
                    "body": {"keywords": "administrator", "location": "Toronto", "page": "2"}}
    assert total == 42 and len(jobs) == 1  # a posting with no link is skipped
    job = jobs[0]
    assert (job.source, job.external_id, job.title, job.company) == ("jooble", "-123", "Administrative Assistant", "Maple Dental")
    assert job.source_url == "https://jooble.org/desc/-123"
    assert "<b>" not in job.description and "book patients" in job.description
    assert job.date_posted.year == 2026 and job.employment_type == "Full-time"


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(403, json={}),
    lambda request: httpx.Response(200, text="<html>not json</html>"),
])
def test_failures_raise_without_the_key(handler):
    with pytest.raises(JoobleError) as error:
        _source(handler).search("admin")
    assert "secret-key" not in str(error.value)
