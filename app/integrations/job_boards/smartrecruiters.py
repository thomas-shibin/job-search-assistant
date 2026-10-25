"""SmartRecruiters adapter: the public, unauthenticated Posting API
(`api.smartrecruiters.com/v1/companies/<company>/postings`) that companies on
SmartRecruiters use to power their own careers pages -- same "consuming a
public API as designed" rationale as the Greenhouse, Lever and Ashby adapters.

The list gives each posting's title, place and type but no description; that
takes one more request per posting. So only one country's postings are read
(big employers list thousands worldwide), descriptions already in the job
pool aren't fetched again (`known`), and at most `max_details` new ones are
fetched per refresh -- the rest arrive on the next refresh.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.core.logging import log_event
from app.integrations.job_boards.base import RawJob
from app.shared.htmlstrip import strip_html_tags
from app.shared.retry import with_retry

logger = logging.getLogger(__name__)

SOURCE_NAME = "smartrecruiters"
_LIST_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
_PAGE_SIZE = 100
_MAX_POSTINGS = 2000
_REQUEST_TIMEOUT_SECONDS = 30.0
_RETRYABLE_ERRORS = (httpx.TransportError,)
_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SmartRecruitersSource:
    """Structurally satisfies `app.integrations.job_boards.base.JobSource`."""

    name = SOURCE_NAME

    def __init__(self, company: str, *, country: str | None = "ca", known: set[str] | frozenset[str] = frozenset(),
                 max_details: int = 150, client: httpx.Client | None = None) -> None:
        self._company = company
        self._country = country
        self._known = known
        self._max_details = max_details
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def _get(self, url: str, params: dict | None = None) -> dict:
        response = with_retry(lambda: self._client.get(url, params=params), retry_on=_RETRYABLE_ERRORS)
        response.raise_for_status()
        return response.json()

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        url = _LIST_URL.format(company=self._company)
        postings: list[dict] = []
        while len(postings) < _MAX_POSTINGS:
            params = {"limit": _PAGE_SIZE, "offset": len(postings)}
            if self._country:
                params["country"] = self._country
            data = self._get(url, params)
            page = data.get("content") or []
            postings += page
            if not page or len(postings) >= int(data.get("totalFound") or 0):
                break
        jobs = []
        fetched = 0
        for posting in postings:
            details = None
            if str(posting.get("id")) not in self._known and fetched < self._max_details:
                try:
                    details = self._get(f"{url}/{posting['id']}")
                except httpx.HTTPError as exc:
                    logger.info("SmartRecruiters %s: no details for one posting (%s)", self._company,
                                type(exc).__name__)
                fetched += 1
            jobs.append(self._to_raw_job(posting, details))
        log_event(logger, "SOURCE_FETCHED", source=SOURCE_NAME, board=self._company, count=len(jobs))
        return jobs

    def _to_raw_job(self, posting: dict, details: dict | None) -> RawJob:
        place = posting.get("location") or {}
        location = ", ".join(x for x in (place.get("city"), place.get("region")) if x) or place.get("fullLocation")
        if place.get("remote"):
            location = f"{location} (Remote)" if location else "Remote"
        sections = ((details or {}).get("jobAd") or {}).get("sections") or {}
        description = "\n\n".join(strip_html_tags((sections.get(key) or {}).get("text") or "").strip()
                                  for key in _SECTIONS if (sections.get(key) or {}).get("text"))
        url = (details or {}).get("postingUrl") or \
            f"https://jobs.smartrecruiters.com/{self._company}/{posting.get('id')}"
        return RawJob(
            external_id=str(posting.get("id")),
            source=SOURCE_NAME,
            source_url=url,
            company=(posting.get("company") or {}).get("name") or self._company,
            title=(posting.get("name") or "Unknown Title").strip(),
            location=location,
            description=description,
            date_posted=_parse_datetime(posting.get("releasedDate")),
            employment_type=(posting.get("typeOfEmployment") or {}).get("label"),
            raw_payload={"company_name": (posting.get("company") or {}).get("name"), "location": place},
        )
