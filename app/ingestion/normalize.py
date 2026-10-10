"""Normalize a source-native `RawJob` into the fields the `Job` ORM model
expects. Deterministic only -- no AI calls (per the architecture's
deterministic-vs-AI table, normalization must be cheap and run on every
discovered job before any paid analysis happens).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TypedDict

from app.sources.base import RawJob

_WHITESPACE_RE = re.compile(r"\s+")
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_ONSITE_RE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|\bin-person\b", re.IGNORECASE)

# How much of the description to scan for remote/hybrid/onsite keywords. Job
# postings often only mention this near the top; scanning the full text risks
# false positives from unrelated later mentions (e.g. "occasional remote work
# for the on-site team").
_REMOTE_TYPE_SCAN_CHARS = 1000


class NormalizedJob(TypedDict):
    source: str
    external_id: str | None
    source_url: str
    company: str
    title: str
    location: str | None
    description: str
    date_posted: datetime | None
    remote_type: str | None
    salary_min: int | None
    salary_max: int | None
    currency: str | None
    employment_type: str | None


def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def infer_remote_type(location: str | None, description: str) -> str | None:
    """Best-effort remote/hybrid/onsite classification from free text.

    Order matters: "hybrid" is checked first since it's unambiguous when
    present, even if "remote" also appears in the text (e.g. "hybrid, with
    some remote flexibility"). Returns None rather than guessing when no
    keyword is found -- an absent signal should stay absent, not default to
    a specific value.
    """
    text = f"{location or ''} {description[:_REMOTE_TYPE_SCAN_CHARS]}"
    if _HYBRID_RE.search(text):
        return "hybrid"
    if _REMOTE_RE.search(text):
        return "remote"
    if _ONSITE_RE.search(text):
        return "onsite"
    return None


def normalize_raw_job(raw: RawJob) -> NormalizedJob:
    company = _clean(raw.company) or "Unknown Company"
    title = _clean(raw.title) or "Unknown Title"
    location = _clean(raw.location) if raw.location else None
    description = raw.description.strip()

    return NormalizedJob(
        source=raw.source,
        external_id=raw.external_id,
        source_url=raw.source_url,
        company=company,
        title=title,
        location=location or None,
        description=description,
        date_posted=raw.date_posted,
        remote_type=infer_remote_type(location, description),
        salary_min=raw.salary_min,
        salary_max=raw.salary_max,
        currency=raw.currency,
        employment_type=raw.employment_type,
    )
