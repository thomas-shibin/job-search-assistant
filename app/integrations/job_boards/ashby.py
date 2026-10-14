"""Ashby job-board adapter: reads the public, unauthenticated Job Postings API
(`api.ashbyhq.com/posting-api/job-board/<board>`) that Ashby companies use to
power their own careers pages -- same "consuming a public API as designed"
rationale as the Greenhouse and Lever adapters."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.integrations.job_boards.base import RawJob
from app.shared.htmlstrip import strip_html_tags
from app.core.logging import log_event
from app.shared.retry import with_retry

logger = logging.getLogger(__name__)

SOURCE_NAME = "ashby"
_URL_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRYABLE_ERRORS = (httpx.TransportError,)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AshbySource:
    """Structurally satisfies `app.integrations.job_boards.base.JobSource`."""

    name = SOURCE_NAME

    def __init__(self, board: str, client: httpx.Client | None = None) -> None:
        self._board = board
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        url = _URL_TEMPLATE.format(board=self._board)
        response = with_retry(lambda: self._client.get(url), retry_on=_RETRYABLE_ERRORS)
        response.raise_for_status()
        postings = [p for p in response.json().get("jobs", []) if p.get("isListed", True)]
        jobs = [self._to_raw_job(posting) for posting in postings]
        log_event(logger, "SOURCE_FETCHED", source=SOURCE_NAME, board=self._board, count=len(jobs))
        return jobs

    def _to_raw_job(self, posting: dict) -> RawJob:
        description = posting.get("descriptionPlain") or strip_html_tags(posting.get("descriptionHtml") or "")
        pay = (posting.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
        if pay:
            description = f"{description}\n\nCompensation: {pay}"
        return RawJob(
            external_id=posting.get("id"),
            source=SOURCE_NAME,
            source_url=posting.get("jobUrl") or "",
            company=self._board,
            title=posting.get("title") or "Unknown Title",
            location=posting.get("location"),
            description=description.strip(),
            date_posted=_parse_datetime(posting.get("publishedAt")),
            employment_type=posting.get("employmentType"),
            raw_payload=posting,
        )
