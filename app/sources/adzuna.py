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
_API_ROOT = "https://api.adzuna.com/v1/api/jobs"
_URL_TEMPLATE = _API_ROOT + "/{country}/search/{page}"
_REQUEST_TIMEOUT_SECONDS = 30.0
_RESULTS_PER_PAGE = 50


class AdzunaError(RuntimeError):
    """A failed Adzuna request, with a message that never includes the request
    URL (it carries app_id/app_key as query parameters).
    """


class _RateLimited(Exception):
    pass


_RETRYABLE_ERRORS = (httpx.TransportError, _RateLimited)


_CURRENCY = {
    "ca": "CAD", "us": "USD", "gb": "GBP", "au": "AUD", "nz": "NZD", "in": "INR", "sg": "SGD", "za": "ZAR",
    "de": "EUR", "fr": "EUR", "it": "EUR", "nl": "EUR", "at": "EUR", "pl": "PLN", "br": "BRL", "mx": "MXN",
}


def _whole(value: Any) -> int | None:
    try:
        return int(round(float(value))) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


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

    def search_page(
        self, *, what: str | None, where: str | None, page: int = 1, per_page: int = _RESULTS_PER_PAGE,
        area: list[str] | tuple[str, ...] = (), what_or: str | None = None, filters: dict[str, str] | None = None,
    ) -> list[RawJob]:
        """One specific page of results for an interactive/browse UI, as opposed
        to `discover_jobs`'s "everything on page 1 across every configured
        title" used by the automated scheduler. Returns an empty list once
        `page` runs past the last page of matching results -- that's the
        caller's "no more results" signal, not an error.

        `area` (e.g. ["Canada", "British Columbia", "Capital", "Victoria"])
        pins the location exactly and takes precedence over free-text `where`;
        `what_or` requires at least one of its space-separated words.
        """
        extra: dict[str, str] = {f"location{i}": part for i, part in enumerate(area)}
        if what_or:
            extra["what_or"] = what_or
        extra.update(filters or {})  # e.g. salary_min, max_days_old, full_time
        postings = self._search(what, None if area else where, page=page, per_page=per_page, **extra)
        jobs = [self.to_raw_job(posting) for posting in postings]
        log_event(
            logger, "SOURCE_FETCHED", source=SOURCE_NAME, country=self._country, page=page, count=len(jobs)
        )
        return jobs

    def search_postings(self, *, page: int = 1, per_page: int = _RESULTS_PER_PAGE, **filters: str) -> list[dict]:
        """Raw postings (with Adzuna's full `location.area` path), for the
        market scan that builds the search-term catalog (app/catalog/scan.py).
        """
        return self._search(None, None, page=page, per_page=per_page, **filters)

    def geodata(self, area: list[str]) -> list[tuple[list[str], int]]:
        """The places one level below `area` in Adzuna's location tree, with job counts."""
        params = {f"location{i}": part for i, part in enumerate(area)}
        data = self._get(f"{_API_ROOT}/{self._country}/geodata", params)
        return [
            (list((entry.get("location") or {}).get("area") or []), int(entry.get("count") or 0))
            for entry in data.get("locations", [])
        ]

    def categories(self) -> list[tuple[str, str]]:
        """(tag, label) for every Adzuna job category, e.g. ("it-jobs", "IT Jobs")."""
        data = self._get(f"{_API_ROOT}/{self._country}/categories", {})
        return [(c["tag"], c["label"]) for c in data.get("results", []) if c.get("tag")]

    def _search(
        self, what: str | None, where: str | None, page: int, per_page: int = _RESULTS_PER_PAGE, **extra: str
    ) -> list[dict]:
        params: dict[str, str | int] = {"results_per_page": per_page, **extra}
        if what:
            params["what"] = what
        if where:
            params["where"] = where
        return self._get(_URL_TEMPLATE.format(country=self._country, page=page), params).get("results", [])

    def _get(self, url: str, params: dict) -> dict:
        params = {"app_id": self._app_id, "app_key": self._app_key, "content-type": "application/json", **params}

        def get() -> httpx.Response:
            response = self._client.get(url, params=params)
            if response.status_code == 429:  # per-minute rate limit: back off and retry
                raise _RateLimited()
            return response

        try:
            response = with_retry(get, retry_on=_RETRYABLE_ERRORS, max_attempts=4, base_delay_seconds=2.0)
        except _RateLimited:
            raise AdzunaError("Adzuna rate limit reached (HTTP 429) -- try again in a minute") from None
        except httpx.TransportError as exc:
            raise AdzunaError(f"Could not reach Adzuna: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise AdzunaError(f"Adzuna returned HTTP {response.status_code}")
        return response.json()

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
            salary_min=_whole(posting.get("salary_min")),
            salary_max=_whole(posting.get("salary_max")),
            currency=_CURRENCY.get(self._country) if posting.get("salary_min") or posting.get("salary_max") else None,
            employment_type=" ".join(
                kind for kind in (posting.get("contract_time"), posting.get("contract_type")) if kind
            ) or None,
            raw_payload=posting,
        )
