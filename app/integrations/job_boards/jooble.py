"""Jooble job-search adapter: Jooble's partner REST API (a key from
https://jooble.org/api/about, sent in the URL path), the second job board
behind Adzuna. The Jobs page asks it when Adzuna can't answer -- keys missing,
rate or daily limit reached, or the service down -- so a member's search still
finds board postings (app/modules/jobs/service.py).

Request: POST {JOOBLE_URL}{key} with JSON {"keywords", "location", "page"}.
Response: {"totalCount", "jobs": [{"id", "title", "company", "location",
"snippet", "salary", "type", "link", "updated", "source"}]}. Like Adzuna, a
result carries a short preview, not the whole description; nothing is stored.

Checked against Jooble's published sample code, not yet against a live key:
the tests use a mock transport. Try one search after adding JOOBLE_API_KEY.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.core.exceptions import IntegrationError
from app.integrations.job_boards.base import RawJob
from app.shared.htmlstrip import strip_html_tags
from app.shared.retry import with_retry

logger = logging.getLogger(__name__)

SOURCE_NAME = "jooble"
DEFAULT_URL = "https://jooble.org/api/"
_TIMEOUT_SECONDS = 8.0  # someone is waiting on the Jobs page
_RETRYABLE_ERRORS = (httpx.TransportError,)


class JoobleError(IntegrationError, RuntimeError):
    """A failed Jooble request; the message never includes the URL (it carries the key)."""


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class JoobleSource:
    name = SOURCE_NAME

    def __init__(self, api_key: str, base_url: str = DEFAULT_URL, client: httpx.Client | None = None) -> None:
        self._url = base_url.rstrip("/") + "/" + api_key
        self._client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)

    def search(self, keywords: str, location: str | None = None, page: int = 1) -> tuple[list[RawJob], int]:
        """(one page of postings, how many there are in all)."""
        body = {"keywords": keywords or "", "page": str(max(page, 1))}
        if location:
            body["location"] = location
        try:
            response = with_retry(lambda: self._client.post(self._url, json=body), retry_on=_RETRYABLE_ERRORS,
                                  max_attempts=2, base_delay_seconds=0.5)
        except httpx.TransportError as exc:
            raise JoobleError(f"Could not reach Jooble: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise JoobleError(f"Jooble returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise JoobleError("Jooble returned something that isn't JSON") from None
        jobs = [self.to_raw_job(p) for p in data.get("jobs") or [] if p.get("link") and p.get("title")]
        return jobs, int(data.get("totalCount") or 0)

    def to_raw_job(self, posting: dict) -> RawJob:
        return RawJob(
            external_id=str(posting["id"]) if posting.get("id") is not None else None,
            source=SOURCE_NAME,
            source_url=posting["link"],
            company=(posting.get("company") or "").strip() or "Unknown Company",
            title=strip_html_tags(posting["title"]).strip(),
            location=(posting.get("location") or "").strip() or None,
            description=strip_html_tags(posting.get("snippet") or "").strip(),
            date_posted=_parse_datetime(posting.get("updated")),
            employment_type=(posting.get("type") or "").strip() or None,
            raw_payload={"salary": posting.get("salary") or None},
        )
