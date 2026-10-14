"""Tests for PDF resume parsing.

Mirrors test_resume_parser.py's expectations against the .docx fixture, but
for the .pdf fixture. PDF extraction has no bold/paragraph-style metadata, so
this is a real (if synthetic) test of the ALL-CAPS heading fallback heuristic
and the "-"-prefixed bullet heuristic used for plain-text input, not just a
copy-paste of the docx test.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database.base import Base
import app.models  # noqa: F401  register all models on Base.metadata
from app.resume.parser import import_resume, parse_resume, parse_resume_pdf

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "sample_resume.pdf"


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_parse_pdf_extracts_expected_structure():
    parsed = parse_resume_pdf(FIXTURE_PATH)

    assert parsed.summary is not None
    assert "distributed" in parsed.summary.lower()

    assert len(parsed.employments) == 3
    assert len(parsed.education) == 1
    assert len(parsed.certifications) == 2

    total_bullets = sum(len(e.bullets) for e in parsed.employments)
    assert total_bullets == 9


def test_pdf_bullets_preserved_verbatim():
    parsed = parse_resume_pdf(FIXTURE_PATH)
    first_job = parsed.employments[0]
    assert first_job.bullets == [
        "Led design and implementation of a microservices platform handling 50M+ requests/day",
        "Reduced p95 API latency by 40% through caching and query optimization",
        "Mentored a team of 5 engineers and led architecture review sessions",
    ]


def test_pdf_employment_dates_parsed_including_varied_format():
    parsed = parse_resume_pdf(FIXTURE_PATH)
    jobs_by_company = {e.company: e for e in parsed.employments}

    acme = jobs_by_company["Acme Cloud Systems"]
    assert acme.title == "Senior Software Engineer"
    assert acme.start_date == date(2021, 1, 1)
    assert acme.is_current is True

    # "(2015-2018)" with no spaces around the dash — the same tricky format
    # the docx fixture exercises.
    gamma = jobs_by_company["Gamma Software"]
    assert gamma.title == "Junior Developer"
    assert gamma.start_date == date(2015, 1, 1)
    assert gamma.end_date == date(2018, 1, 1)


def test_pdf_skills_split_correctly_across_wrapped_lines():
    # The skills section spans three plain-text lines (no reflow like a
    # docx paragraph), so this also verifies tokens aren't lost/duplicated
    # at line-wrap boundaries.
    parsed = parse_resume_pdf(FIXTURE_PATH)
    assert "Java" in parsed.skills
    assert "Kubernetes" in parsed.skills
    assert "CI/CD" in parsed.skills
    assert "Linux" in parsed.skills
    assert len(parsed.skills) == 18


def test_pdf_unrecognized_section_lands_in_raw_unparsed_not_dropped():
    parsed = parse_resume_pdf(FIXTURE_PATH)
    combined = "\n".join(parsed.raw_unparsed_sections)
    assert "VOLUNTEER" in combined
    assert "Local Coding Bootcamp" in combined


def test_pdf_header_block_is_extracted_as_name_and_contact():
    parsed = parse_resume_pdf(FIXTURE_PATH)
    assert parsed.full_name == "Jordan Casey"
    assert "jordan.casey@example.com" in parsed.contact_line
    assert "Victoria, BC" in parsed.contact_line


def test_parse_resume_dispatches_to_pdf_by_extension():
    via_dispatch = parse_resume(FIXTURE_PATH)
    direct = parse_resume_pdf(FIXTURE_PATH)
    assert via_dispatch.raw_text_for_hash == direct.raw_text_for_hash


def test_parse_resume_rejects_unsupported_extension(tmp_path):
    bogus = tmp_path / "resume.txt"
    bogus.write_text("hello")
    with pytest.raises(ValueError, match="Unsupported resume file type"):
        parse_resume(bogus)


def test_import_resume_from_pdf_creates_profile_with_expected_counts(session):
    result = import_resume(session, FIXTURE_PATH)

    assert result.created is True
    assert result.counts["employments"] == 3
    assert result.counts["bullets"] == 9
    assert result.counts["skills"] == 18
    assert result.counts["education"] == 1
    assert result.counts["certifications"] == 2
    assert result.profile.raw_unparsed_sections is not None
