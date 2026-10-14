"""Hard filters applied to a `Job` before any (paid, deterministic-or-not)
scoring happens (spec section 10). Purely deterministic and config-driven --
no hardcoded locations/keywords/statuses, per the project's core requirement.

Rejections are explainable: every filter that fails contributes a
human-readable reason to `FilterResult.reasons` rather than failing silently.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config.schema import MatchingConfig, SearchProfile
from app.core.logging import log_event
from app.modules.jobs.models import Job, JobStatus

logger = logging.getLogger(__name__)

# Statuses that mean a job has already been through a human decision or a
# terminal outcome -- re-running filters/scoring on these would be wasted
# work (and, once AI extraction exists, wasted API cost).
_ALREADY_PROCESSED_STATUSES = frozenset(
    {JobStatus.APPLIED, JobStatus.REJECTED, JobStatus.SKIPPED, JobStatus.ARCHIVED}
)


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def _normalize_location_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def location_matches(job_location: str | None, remote_type: str | None, configured_locations: list[str]) -> bool:
    """Loose substring matching in both directions, since job postings and
    config entries phrase locations inconsistently (e.g. "Vancouver, BC" vs
    "Vancouver, British Columbia" vs just "Vancouver"). A `configured_locations`
    entry containing "remote" matches any job whose inferred remote_type is
    "remote", regardless of the literal location string.
    """
    job_loc_norm = _normalize_location_token(job_location or "")

    for configured in configured_locations:
        configured_norm = _normalize_location_token(configured)
        if "remote" in configured_norm and remote_type == "remote":
            return True
        if not job_loc_norm or not configured_norm:
            continue
        if job_loc_norm in configured_norm or configured_norm in job_loc_norm:
            return True
        # Token-level overlap catches e.g. "Vancouver, BC" vs "Vancouver, British
        # Columbia" where neither is a substring of the other.
        job_tokens = set(job_loc_norm.split())
        configured_tokens = set(configured_norm.split())
        if job_tokens & configured_tokens:
            return True
    return False


def check_hard_filters(
    job: Job,
    matching_config: MatchingConfig,
    search_profile: SearchProfile | None,
) -> FilterResult:
    """Run all hard filters against `job`. `search_profile` is the
    `SearchProfile` the job was discovered under (via `job.search_profile_name`
    resolved by the caller); it is `None` for manually imported jobs that
    aren't tied to a search profile, in which case the location filter is
    skipped entirely rather than treated as a failure -- there is nothing to
    check it against.
    """
    reasons: list[str] = []

    if job.status in _ALREADY_PROCESSED_STATUSES:
        reasons.append(f"Job status '{job.status.value}' has already been processed; skipping re-analysis")

    if search_profile is not None and search_profile.locations:
        if not location_matches(job.location, job.remote_type, search_profile.locations):
            reasons.append(
                f"Location '{job.location or 'unspecified'}' does not match any of "
                f"search profile '{search_profile.name}''s configured locations"
            )

    haystack = f"{job.title}\n{job.description}".lower()
    for keyword in matching_config.negative_keywords:
        if keyword.lower() in haystack:
            reasons.append(f"Negative keyword matched: '{keyword}'")

    result = FilterResult(passed=not reasons, reasons=reasons)
    if not result.passed:
        log_event(logger, "JOB_FILTERED", job_id=job.id, reasons=result.reasons)
    return result
