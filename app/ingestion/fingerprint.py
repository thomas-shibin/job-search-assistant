"""Duplicate detection for discovered jobs (spec section 6).

Three escalating, deterministic checks -- no AI/embeddings, per the
architecture's deterministic-vs-AI table:

1. Exact `(source, external_id)` match -- the same posting re-ingested from
   the same source.
2. Exact fingerprint match (normalized company+title+location) -- the same
   role posted under a different source/external_id, e.g. seen manually and
   also via an ATS adapter later.
3. Near-duplicate description similarity among jobs at the same company --
   catches slightly reworded reposts (extra whitespace, a tweaked sentence)
   that would otherwise fingerprint as distinct if the title string differs
   slightly.

Trade-off: step 3 uses `difflib.SequenceMatcher`, which is O(n*m) per pair.
Comparing against every existing job would not scale, so it's scoped to jobs
at the same company only -- fine for a local, single-user tool where a given
company realistically has a handful of open jobs at once. A larger-scale
version of this project would want a proper similarity index (e.g. MinHash)
instead; that's deliberately not built until there's a demonstrated need.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.ingestion.normalize import NormalizedJob
from app.models.job import Job
from app.sources.base import RawJob
from app.shared.hashing import content_hash, normalize_text

# Empirically chosen: minor formatting/whitespace edits between reposts of the
# same job typically score well above 0.95; genuinely different postings at
# the same company are typically well below 0.7. Configurable only in the
# sense that it's a single named constant -- promote to config.yaml if real
# usage shows it needs tuning per-user.
NEAR_DUPLICATE_DESCRIPTION_THRESHOLD = 0.92


def compute_fingerprint(company: str, title: str, location: str | None) -> str:
    return content_hash(company, title, location or "")


def compute_description_hash(description: str) -> str:
    return content_hash(description)


def _is_near_duplicate_description(a: str, b: str) -> bool:
    ratio = SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()
    return ratio >= NEAR_DUPLICATE_DESCRIPTION_THRESHOLD


def find_duplicate(session: Session, raw: RawJob, normalized: NormalizedJob) -> Job | None:
    """Return the existing `Job` this posting duplicates, or None if it's new."""
    if raw.external_id:
        existing = (
            session.query(Job)
            .filter_by(source=raw.source, external_id=raw.external_id)
            .first()
        )
        if existing is not None:
            return existing

    fingerprint = compute_fingerprint(normalized["company"], normalized["title"], normalized["location"])

    existing = session.query(Job).filter_by(fingerprint=fingerprint).first()
    if existing is not None:
        return existing

    description_hash = compute_description_hash(normalized["description"])
    existing = session.query(Job).filter_by(description_hash=description_hash).first()
    if existing is not None:
        return existing

    same_company_jobs = session.query(Job).filter_by(company=normalized["company"]).all()
    for candidate in same_company_jobs:
        if _is_near_duplicate_description(normalized["description"], candidate.description):
            return candidate

    return None
