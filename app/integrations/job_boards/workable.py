"""Workable adapter: the public, unauthenticated jobs widget API
(`apply.workable.com/api/v1/widget/accounts/<account>?details=true`) that
Workable companies use to list their jobs on their own websites -- same
"consuming a public API as designed" rationale as the Greenhouse, Lever and
Ashby adapters. One request returns every open job with its description.
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

SOURCE_NAME = "workable"
_URL_TEMPLATE = "https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRYABLE_ERRORS = (httpx.TransportError,)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class WorkableSource:
    """Structurally satisfies `app.integrations.job_boards.base.JobSource`."""

    name = SOURCE_NAME

    def __init__(self, account: str, client: httpx.Client | None = None) -> None:
        self._account = account
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        url = _URL_TEMPLATE.format(account=self._account)
        response = with_retry(lambda: self._client.get(url), retry_on=_RETRYABLE_ERRORS)
        response.raise_for_status()
        data = response.json()
        name = data.get("name") or None
        jobs = [self._to_raw_job(job, name) for job in data.get("jobs") or [] if job.get("url")]
        log_event(logger, "SOURCE_FETCHED", source=SOURCE_NAME, board=self._account, count=len(jobs))
        return jobs

    def _to_raw_job(self, job: dict, company: str | None) -> RawJob:
        location = ", ".join(x for x in (job.get("city"), job.get("state"), job.get("country")) if x) or None
        if job.get("telecommuting"):
            location = f"{location} (Remote)" if location else "Remote"
        return RawJob(
            external_id=job.get("shortcode") or job["url"],
            source=SOURCE_NAME,
            source_url=job["url"],
            company=company or self._account,
            title=(job.get("title") or "Unknown Title").strip(),
            location=location,
            description=strip_html_tags(job.get("description") or "").strip(),
            date_posted=_parse_date(job.get("published_on") or job.get("created_at")),
            employment_type=job.get("employment_type"),
            raw_payload={"company_name": company, "country": job.get("country"),
                         "locations": job.get("locations") or [], "telecommuting": bool(job.get("telecommuting"))},
        )
