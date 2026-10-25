"""When two postings are the same job, and how old a posting may be before
it's left out -- the same rules for every source (the job board, the second
board, and companies' careers-site feeds in the job pool).

Same job: the same company (ignoring "Inc.", "Ltd." and the like), the same
title and the same city. Boards list one ad several times (an employer posts
it on several sites), and a company's own feed often has the board's ad too.
The copy kept is the most reliable one: the company's own feed first (full
description, and it's still listed), then the job board, then the second
board. The same title in another city is a different job and is kept.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, TypeVar

from app.integrations.job_boards.base import RawJob

# Lower is more reliable: the copy kept when sources list the same job.
SOURCE_RANK = {"jooble": 2, "adzuna": 1}  # every company feed ranks 0

_COMPANY_SUFFIX = re.compile(r"\b(inc|ltd|llc|corp|corporation|co|company|limited|technologies|canada)\b\.?",
                             re.IGNORECASE)
_PLACE_NOISE = re.compile(r"\b(remote|hybrid|on-?site|in|only|anywhere|canada|province|ca)\b", re.IGNORECASE)

T = TypeVar("T")


def normalize(value: str | None, *, company: bool = False) -> str:
    value = (value or "").lower()
    if company:
        value = _COMPANY_SUFFIX.sub(" ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def city(location: str | None) -> str:
    """'Toronto, ON' / 'Toronto, Ontario, Canada' / 'Remote - Toronto' -> 'toronto'."""
    for part in re.split(r"[,|/()\-–]+", location or ""):
        part = normalize(_PLACE_NOISE.sub(" ", part))
        if part:
            return part
    return ""


def key(company: str | None, title: str | None, location: str | None) -> tuple[str, str, str]:
    return normalize(company, company=True), normalize(title), city(location)


def job_key(job: RawJob) -> tuple[str, str, str]:
    return key(job.company, job.title, job.location)


def rank(job: RawJob) -> int:
    return SOURCE_RANK.get(job.source, 0)


def unique(jobs: Iterable[RawJob]) -> list[RawJob]:
    """One copy of each job, the most reliable source's, in the order given."""
    best: dict[tuple, RawJob] = {}
    order: list[tuple] = []
    for job in jobs:
        k = job_key(job)
        if k not in best:
            best[k] = job
            order.append(k)
        elif rank(job) < rank(best[k]):
            best[k] = job
    return [best[k] for k in order]


def unique_by(items: Iterable[T], key_of) -> list[T]:
    """The first of each key, in order (for rows already sorted best first)."""
    seen: set = set()
    out = []
    for item in items:
        k = key_of(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


def cutoff(max_age_days: int | None) -> datetime | None:
    """Naive UTC: postings dated before this are too old to show."""
    if not max_age_days:
        return None
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max_age_days)


def too_old(job: RawJob, max_age_days: int | None) -> bool:
    """Dated longer ago than `max_age_days`. An undated posting isn't judged here."""
    if not max_age_days or job.date_posted is None:
        return False
    posted = job.date_posted
    if posted.tzinfo is not None:
        posted = posted.astimezone(timezone.utc).replace(tzinfo=None)
    return posted < cutoff(max_age_days)
