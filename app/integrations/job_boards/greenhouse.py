"""Greenhouse job-board adapter (Milestone 12): reads the public, documented,
unauthenticated Job Board API (`boards-api.greenhouse.io`) that companies use
to power their own careers pages -- consuming a public API as designed, not
scraping a restricted platform (see docs/ARCHITECTURE.md, "Job source risk
assessment").
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any

import httpx

from app.integrations.job_boards.base import RawJob
from app.shared.htmlstrip import strip_html_tags
from app.core.logging import log_event
from app.shared.retry import with_retry

logger = logging.getLogger(__name__)

SOURCE_NAME = "greenhouse"
_URL_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRYABLE_ERRORS = (httpx.TransportError,)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GreenhouseSource:
    """Structurally satisfies `app.integrations.job_boards.base.JobSource`."""

    name = SOURCE_NAME

    def __init__(self, board_token: str, client: httpx.Client | None = None) -> None:
        self._board_token = board_token
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        url = _URL_TEMPLATE.format(board_token=self._board_token)
        response = with_retry(lambda: self._client.get(url), retry_on=_RETRYABLE_ERRORS)
        response.raise_for_status()
        payload = response.json()

        jobs = [self._to_raw_job(posting) for posting in payload.get("jobs", [])]
        log_event(logger, "SOURCE_FETCHED", source=SOURCE_NAME, board_token=self._board_token, count=len(jobs))
        return jobs

    def _to_raw_job(self, posting: dict) -> RawJob:
        location = (posting.get("location") or {}).get("name")
        return RawJob(
            external_id=str(posting.get("id")) if posting.get("id") is not None else None,
            source=SOURCE_NAME,
            source_url=posting.get("absolute_url") or "",
            company=posting.get("company_name") or self._board_token,
            title=posting.get("title") or "Unknown Title",
            location=location,
            # Greenhouse sends the HTML entity-escaped (&lt;p&gt;): unescape, then strip the tags.
            description=strip_html_tags(html.unescape(posting.get("content") or "")),
            date_posted=_parse_datetime(posting.get("updated_at")),
            raw_payload=posting,
        )
