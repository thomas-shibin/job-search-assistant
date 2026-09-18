from __future__ import annotations

from app.ingestion.normalize import infer_remote_type, normalize_raw_job
from app.sources.base import RawJob


def _raw_job(**overrides) -> RawJob:
    defaults = dict(
        external_id=None,
        source="manual",
        source_url="https://example.com/job/1",
        company="  Acme Corp  ",
        title="  Senior   Backend Engineer ",
        location="  Vancouver, BC ",
        description="A great job.",
        date_posted=None,
        raw_payload={},
    )
    defaults.update(overrides)
    return RawJob(**defaults)


def test_infer_remote_type_remote():
    assert infer_remote_type("Vancouver, BC (Remote)", "Work from anywhere in Canada.") == "remote"


def test_infer_remote_type_hybrid():
    assert infer_remote_type("Toronto, ON", "This is a hybrid role, 3 days on-site per week.") == "hybrid"


def test_infer_remote_type_hybrid_takes_priority_over_remote_mention():
    assert infer_remote_type(None, "Hybrid role with occasional remote flexibility.") == "hybrid"


def test_infer_remote_type_onsite():
    assert infer_remote_type("Victoria, BC", "This is an on-site position at our downtown office.") == "onsite"


def test_infer_remote_type_unclear_returns_none():
    assert infer_remote_type("Victoria, BC", "We are looking for a great engineer.") is None


def test_normalize_raw_job_cleans_whitespace():
    normalized = normalize_raw_job(_raw_job())
    assert normalized["company"] == "Acme Corp"
    assert normalized["title"] == "Senior Backend Engineer"
    assert normalized["location"] == "Vancouver, BC"


def test_normalize_raw_job_missing_location_is_none():
    normalized = normalize_raw_job(_raw_job(location=None))
    assert normalized["location"] is None


def test_normalize_raw_job_blank_company_title_fall_back():
    normalized = normalize_raw_job(_raw_job(company="   ", title="   "))
    assert normalized["company"] == "Unknown Company"
    assert normalized["title"] == "Unknown Title"
