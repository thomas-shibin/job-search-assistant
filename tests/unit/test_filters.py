from __future__ import annotations

from app.core.config.schema import MatchingConfig, SearchProfile
from app.modules.jobs.ingestion.filters import check_hard_filters
from app.modules.jobs.models import Job, JobStatus


def _job(**overrides) -> Job:
    defaults = dict(
        id=1,
        source="manual",
        source_url="https://example.com",
        company="Example Corp",
        title="Senior Backend Engineer",
        location="Vancouver, BC",
        remote_type=None,
        description="We need Java and Spring Boot experience.",
        status=JobStatus.DISCOVERED,
        fingerprint="fp",
        description_hash="dh",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _matching_config(**overrides) -> MatchingConfig:
    return MatchingConfig(**overrides)


def test_job_matching_configured_location_passes():
    sp = SearchProfile(name="BC Backend", address_profile="bc", locations=["Vancouver, BC"])
    result = check_hard_filters(_job(), _matching_config(), sp)
    assert result.passed is True
    assert result.reasons == []


def test_job_outside_configured_locations_fails():
    sp = SearchProfile(name="Ontario Backend", address_profile="on", locations=["Toronto, ON"])
    result = check_hard_filters(_job(location="Victoria, BC"), _matching_config(), sp)
    assert result.passed is False
    assert any("Location" in r for r in result.reasons)


def test_remote_job_matches_remote_search_entry():
    sp = SearchProfile(name="Remote", address_profile="bc", locations=["Remote Canada"])
    result = check_hard_filters(_job(location=None, remote_type="remote"), _matching_config(), sp)
    assert result.passed is True


def test_no_search_profile_skips_location_filter_gracefully():
    result = check_hard_filters(_job(location="Nowhere"), _matching_config(), None)
    assert result.passed is True


def test_negative_keyword_in_title_fails():
    config = _matching_config(negative_keywords=["Frontend Developer"])
    result = check_hard_filters(_job(title="Frontend Developer"), config, None)
    assert result.passed is False
    assert any("Negative keyword" in r for r in result.reasons)


def test_negative_keyword_in_description_fails():
    config = _matching_config(negative_keywords=["PHP Developer"])
    job = _job(description="Looking for a PHP Developer with 5 years experience.")
    result = check_hard_filters(job, config, None)
    assert result.passed is False


def test_already_applied_job_is_filtered():
    result = check_hard_filters(_job(status=JobStatus.APPLIED), _matching_config(), None)
    assert result.passed is False
    assert any("already been processed" in r for r in result.reasons)


def test_multiple_failures_all_reported():
    sp = SearchProfile(name="Ontario Backend", address_profile="on", locations=["Toronto, ON"])
    config = _matching_config(negative_keywords=["iOS Developer"])
    job = _job(location="Victoria, BC", title="iOS Developer", status=JobStatus.SKIPPED)
    result = check_hard_filters(job, config, sp)
    assert result.passed is False
    assert len(result.reasons) == 3
