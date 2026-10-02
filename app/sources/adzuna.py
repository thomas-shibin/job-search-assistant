"""Adzuna job-search adapter: reads the public, documented Adzuna Job Search
API (`api.adzuna.com`) -- a legitimate job-aggregator API (key-authenticated,
signed up for at developer.adzuna.com, not scraped), covering postings from
many employers/boards per query rather than one ATS at a time like the
Greenhouse/Lever adapters (see docs/ARCHITECTURE.md, "Job source risk
assessment").
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.sources.base import RawJob
from app.utils.htmlstrip import strip_html_tags
from app.utils.logging import log_event
from app.utils.retry import with_retry

logger = logging.getLogger(__name__)

SOURCE_NAME = "adzuna"
_URL_TEMPLATE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_REQUEST_TIMEOUT_SECONDS = 30.0
_RESULTS_PER_PAGE = 50
_RETRYABLE_ERRORS = (httpx.TransportError,)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class AdzunaSource:
    """Structurally satisfies `app.sources.base.JobSource`. Unlike Greenhouse/
    Lever (one fixed board per instance), Adzuna is query-based -- it runs one
    search per `search_profile.titles` entry (falling back to a single
    unfiltered query if none are set) and de-duplicates postings that surface
    under more than one title query.
    """

    name = SOURCE_NAME

    def __init__(self, app_id: str, app_key: str, country: str, client: httpx.Client | None = None) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._country = country
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        titles = list(getattr(search_profile, "titles", None) or [])
        queries: list[str | None] = titles or [None]
        where = next(iter(getattr(search_profile, "locations", None) or []), None)

        seen_ids: set[str] = set()
        jobs: list[RawJob] = []
        for what in queries:
            for posting in self._search(what, where, page=1):
                raw = self.to_raw_job(posting)
                if raw.external_id is not None:
                    if raw.external_id in seen_ids:
                        continue
                    seen_ids.add(raw.external_id)
                jobs.append(raw)

        log_event(logger, "SOURCE_FETCHED", source=SOURCE_NAME, country=self._country, count=len(jobs))
        return jobs

    def search_page(self, *, what: str | None, where: str | None, page: int = 1) -> list[RawJob]:
        """One specific page of results for an interactive/browse UI, as opposed
        to `discover_jobs`'s "everything on page 1 across every configured
        title" used by the automated scheduler. Returns an empty list once
        `page` runs past the last page of matching results -- that's the
        caller's "no more results" signal, not an error.
        """
        postings = self._search(what, where, page=page)
        jobs = [self.to_raw_job(posting) for posting in postings]
        log_event(
            logger, "SOURCE_FETCHED", source=SOURCE_NAME, country=self._country, page=page, count=len(jobs)
        )
        return jobs

    def _search(self, what: str | None, where: str | None, page: int) -> list[dict]:
        url = _URL_TEMPLATE.format(country=self._country, page=page)
        params: dict[str, str | int] = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "results_per_page": _RESULTS_PER_PAGE,
            "content-type": "application/json",
        }
        if what:
            params["what"] = what
        if where:
            params["where"] = where

        response = with_retry(lambda: self._client.get(url, params=params), retry_on=_RETRYABLE_ERRORS)
        response.raise_for_status()
        return response.json().get("results", [])

    def to_raw_job(self, posting: dict) -> RawJob:
        company = (posting.get("company") or {}).get("display_name") or "Unknown Company"
        location = (posting.get("location") or {}).get("display_name")
        return RawJob(
            external_id=str(posting.get("id")) if posting.get("id") is not None else None,
            source=SOURCE_NAME,
            source_url=posting.get("redirect_url") or "",
            company=company,
            title=posting.get("title") or "Unknown Title",
            location=location,
            description=strip_html_tags(posting.get("description") or ""),
            date_posted=_parse_datetime(posting.get("created")),
            raw_payload=posting,
        )
