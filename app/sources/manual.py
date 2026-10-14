"""Manual job import: the always-available, zero-risk discovery path.

The user pastes a job URL and/or its raw description directly (e.g. copied
from LinkedIn, a company careers page, or anywhere else). This never touches
a third-party platform programmatically, so it carries none of the ToS risk
that automated scraping/interaction would (see docs/ARCHITECTURE.md, "Job
Source Risk Assessment"). Automated `JobSource` implementations (Greenhouse,
Lever, etc.) are a later milestone and go through `discover_jobs`, not here.
"""
from __future__ import annotations

import re

from app.sources.base import RawJob
from app.shared.hashing import content_hash

SOURCE_NAME = "manual"

_FIELD_LINE_RE = {
    "title": re.compile(r"^(?:job title|title|position)\s*:\s*(.+)$", re.IGNORECASE),
    "company": re.compile(r"^(?:company|employer)\s*:\s*(.+)$", re.IGNORECASE),
    "location": re.compile(r"^(?:location)\s*:\s*(.+)$", re.IGNORECASE),
}
_TITLE_AT_COMPANY_RE = re.compile(r"^(.{3,80}?)\s+at\s+(.{2,80})$", re.IGNORECASE)


def _extract_field_from_lines(description: str, field: str) -> str | None:
    pattern = _FIELD_LINE_RE[field]
    for line in description.splitlines()[:10]:
        m = pattern.match(line.strip())
        if m:
            return m.group(1).strip()
    return None


def _guess_title_and_company(description: str) -> tuple[str | None, str | None]:
    title = _extract_field_from_lines(description, "title")
    company = _extract_field_from_lines(description, "company")
    if title and company:
        return title, company

    for line in description.splitlines()[:5]:
        line = line.strip()
        m = _TITLE_AT_COMPANY_RE.match(line)
        if m:
            return title or m.group(1).strip(), company or m.group(2).strip()

    return title, company


def import_manual_job(
    *,
    description: str,
    url: str | None = None,
    company: str | None = None,
    title: str | None = None,
    location: str | None = None,
) -> RawJob:
    """Build a `RawJob` from a manually supplied job posting.

    Company/title/location are used as given when provided. Otherwise this
    applies a few cheap, deterministic heuristics (explicit "Title:"/"Company:"
    lines, or a "<Title> at <Company>" first line). No AI is used here --
    manual import is meant to work even with no API key configured. Anything
    that can't be guessed falls back to "Unknown ..." rather than guessing
    wildly or blocking the import.
    """
    guessed_title, guessed_company = _guess_title_and_company(description)

    resolved_title = title or guessed_title or "Unknown Title"
    resolved_company = company or guessed_company or "Unknown Company"
    resolved_location = location or _extract_field_from_lines(description, "location")

    resolved_url = url or f"manual://{content_hash(description)[:16]}"

    return RawJob(
        external_id=url,
        source=SOURCE_NAME,
        source_url=resolved_url,
        company=resolved_company,
        title=resolved_title,
        location=resolved_location,
        description=description,
        date_posted=None,
        raw_payload={
            "manual_url_provided": url is not None,
            "company_provided": company is not None,
            "title_provided": title is not None,
            "location_provided": location is not None,
        },
    )
