"""Lever job-board adapter (Milestone 12): reads the public, documented,
unauthenticated Postings API (`api.lever.co/v0/postings/<company>`) that
Lever companies use to power their own careers pages -- same "consuming a
public API as designed" rationale as the Greenhouse adapter (see
docs/ARCHITECTURE.md, "Job source risk assessment").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.logging import log_event
from app.integrations.job_boards.base import RawJob
from app.shared.htmlstrip import strip_html_tags
from app.shared.retry import with_retry

logger = logging.getLogger(__name__)

SOURCE_NAME = "lever"
_URL_TEMPLATE = "https://api.lever.co/v0/postings/{company}?mode=json"
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRYABLE_ERRORS = (httpx.TransportError,)


def _parse_epoch_millis(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


class LeverSource:
    """Structurally satisfies `app.integrations.job_boards.base.JobSource`."""

    name = SOURCE_NAME

    def __init__(self, company: str, client: httpx.Client | None = None) -> None:
        self._company = company
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        url = _URL_TEMPLATE.format(company=self._company)
        response = with_retry(lambda: self._client.get(url), retry_on=_RETRYABLE_ERRORS)
        response.raise_for_status()
        postings = response.json()

        jobs = [self._to_raw_job(posting) for posting in postings]
        log_event(logger, "SOURCE_FETCHED", source=SOURCE_NAME, company=self._company, count=len(jobs))
        return jobs

    def _to_raw_job(self, posting: dict) -> RawJob:
        categories = posting.get("categories") or {}
        description_parts = [strip_html_tags(posting.get("descriptionPlain") or posting.get("description") or "")]
        for section in posting.get("lists") or []:
            heading = section.get("text") or ""
            content = strip_html_tags(section.get("content") or "")
            description_parts.append(f"{heading}\n{content}" if heading else content)

        return RawJob(
            external_id=posting.get("id"),
            source=SOURCE_NAME,
            source_url=posting.get("hostedUrl") or "",
            company=self._company,
            title=posting.get("text") or "Unknown Title",
            location=categories.get("location"),
            description="\n\n".join(p for p in description_parts if p),
            date_posted=_parse_epoch_millis(posting.get("createdAt")),
            employment_type=categories.get("commitment"),
            raw_payload=posting,
        )
