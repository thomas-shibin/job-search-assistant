from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database.base import Base
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.resumes.parser import import_resume, parse_resume_docx
from tests.fixtures.build_sample_resume import build as build_sample_resume

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "sample_resume.docx"


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_parse_resume_extracts_expected_structure():
    parsed = parse_resume_docx(FIXTURE_PATH)

    assert parsed.summary is not None
    assert "distributed systems" in parsed.summary.lower()

    assert len(parsed.employments) == 3
    assert len(parsed.education) == 1
    assert len(parsed.certifications) == 2

    total_bullets = sum(len(e.bullets) for e in parsed.employments)
    assert total_bullets == 9


def test_bullets_preserved_verbatim():
    parsed = parse_resume_docx(FIXTURE_PATH)
    first_job = parsed.employments[0]
    assert first_job.bullets == [
        "Led design and implementation of a microservices platform handling 50M+ requests/day",
        "Reduced p95 API latency by 40% through caching and query optimization",
        "Mentored a team of 5 engineers and led architecture review sessions",
    ]


def test_employment_dates_parsed_including_varied_format():
    parsed = parse_resume_docx(FIXTURE_PATH)
    jobs_by_company = {e.company: e for e in parsed.employments}

    acme = jobs_by_company["Acme Cloud Systems"]
    assert acme.title == "Senior Software Engineer"
    assert acme.start_date == date(2021, 1, 1)
    assert acme.is_current is True
    assert acme.end_date is None

    beta = jobs_by_company["Beta Systems Inc."]
    assert beta.title == "Software Engineer"
    assert beta.start_date == date(2018, 6, 1)
    assert beta.end_date == date(2020, 12, 1)
    assert beta.is_current is False

    # Gamma uses a deliberately different date format: "(2015-2018)" with an em-dash separator.
    gamma = jobs_by_company["Gamma Software"]
    assert gamma.title == "Junior Developer"
    assert gamma.start_date == date(2015, 1, 1)
    assert gamma.end_date == date(2018, 1, 1)
    assert gamma.is_current is False


def test_skills_split_correctly():
    parsed = parse_resume_docx(FIXTURE_PATH)
    assert "Java" in parsed.skills
    assert "Spring Boot" in parsed.skills
    assert "Kubernetes" in parsed.skills
    assert "CI/CD" in parsed.skills
    assert len(parsed.skills) == 18


def test_unrecognized_section_lands_in_raw_unparsed_not_dropped():
    parsed = parse_resume_docx(FIXTURE_PATH)
    combined = "\n".join(parsed.raw_unparsed_sections)
    assert "Volunteer Experience" in combined
    assert "Local Coding Bootcamp" in combined


def test_header_block_is_extracted_as_name_and_contact_not_left_unparsed():
    """The pre-heading contact block used to just get dumped into
    raw_unparsed_sections; it's now structured (see docx.py's header
    rendering), and specifically no longer duplicated into raw_unparsed_sections.
    """
    parsed = parse_resume_docx(FIXTURE_PATH)
    assert parsed.full_name == "Jordan Casey"
    assert "jordan.casey@example.com" in parsed.contact_line
    assert "(555) 123-4567" in parsed.contact_line
    assert "Victoria, BC" in parsed.contact_line

    combined = "\n".join(parsed.raw_unparsed_sections)
    assert "jordan.casey@example.com" not in combined


def test_content_hash_stable_across_identical_reparse(tmp_path):
    parsed_a = parse_resume_docx(FIXTURE_PATH)
    parsed_b = parse_resume_docx(FIXTURE_PATH)
    assert parsed_a.raw_text_for_hash == parsed_b.raw_text_for_hash


def test_import_resume_creates_profile_with_expected_counts(session):
    result = import_resume(session, FIXTURE_PATH)

    assert result.created is True
    assert result.profile.version == 1
    assert result.profile.is_active is True
    assert result.counts["employments"] == 3
    assert result.counts["bullets"] == 9
    assert result.counts["skills"] == 18
    assert result.counts["education"] == 1
    assert result.counts["certifications"] == 2
    assert result.profile.raw_unparsed_sections is not None


def test_reimport_unchanged_content_is_noop(session):
    first = import_resume(session, FIXTURE_PATH)
    second = import_resume(session, FIXTURE_PATH)

    assert first.created is True
    assert second.created is False
    assert second.profile.id == first.profile.id
    assert second.profile.version == 1


def test_reimport_changed_content_creates_new_version(session, tmp_path):
    import_resume(session, FIXTURE_PATH)

    modified_path = tmp_path / "modified_resume.docx"
    build_sample_resume(modified_path)
    from docx import Document

    doc = Document(str(modified_path))
    doc.paragraphs[0].text = "Jordan Casey Jr."
    # python-docx doesn't support direct paragraph.text assignment on multi-run
    # paragraphs reliably, so rebuild via runs instead for a guaranteed content change.
    for run in list(doc.paragraphs[0].runs):
        run.text = ""
    if doc.paragraphs[0].runs:
        doc.paragraphs[0].runs[0].text = "Jordan Casey Jr."
    else:
        doc.paragraphs[0].add_run("Jordan Casey Jr.")
    doc.save(str(modified_path))

    result = import_resume(session, modified_path)

    assert result.created is True
    assert result.profile.version == 2
    assert result.profile.is_active is True


def test_reimport_force_creates_new_version_even_if_unchanged(session):
    first = import_resume(session, FIXTURE_PATH)
    second = import_resume(session, FIXTURE_PATH, force=True)

    assert second.created is True
    assert second.profile.version == 2
    assert second.profile.id != first.profile.id


def test_docx_skills_laid_out_in_a_table_are_parsed(tmp_path):
    from docx import Document

    from app.modules.resumes.parser import parse_resume

    doc = Document()
    doc.add_paragraph("Jordan Casey")
    doc.add_heading("Skills", level=1)
    table = doc.add_table(rows=1, cols=2)
    label = table.cell(0, 0).paragraphs[0].add_run("Backend")
    label.bold = True  # a bold cell label must not be mistaken for a section heading
    table.cell(0, 0).add_paragraph("Java")
    table.cell(0, 1).paragraphs[0].add_run("Kafka | Docker")
    doc.add_heading("Experience", level=1)
    doc.add_paragraph("Senior Engineer, 01/2020 - Present")
    path = tmp_path / "table_resume.docx"
    doc.save(path)

    parsed = parse_resume(path)

    assert {"Java", "Kafka", "Docker"} <= set(parsed.skills)
