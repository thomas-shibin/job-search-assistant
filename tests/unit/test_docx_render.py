"""Smoke tests for DOCX rendering (Milestone 6/7): the generated file must be
a valid, readable DOCX containing the sections/content it was given.
"""
from __future__ import annotations

from datetime import date

from docx import Document

from app.config.schema import AddressProfile
from app.documents.docx import render_tailored_resume
from app.models.candidate import Certification, Education, Employment


def _all_text(path) -> str:
    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)


def test_render_includes_all_sections(tmp_path):
    out = tmp_path / "resume.docx"
    employment = Employment(
        id=1, company="Acme", title="Senior Engineer",
        start_date=date(2021, 1, 1), end_date=None, is_current=True,
    )
    render_tailored_resume(
        out,
        candidate_name="Jordan Casey",
        contact_line="jordan.casey@example.com | (555) 123-4567",
        address=AddressProfile(city="Victoria", province="BC", country="Canada"),
        summary="Backend engineer tailored for this role.",
        skills_order=["Java", "AWS"],
        employment_bullets=[(employment, ["Did the thing well"])],
        education=[Education(id=1, institution="Some University", degree="BSc")],
        certifications=[Certification(id=1, name="AWS Certified", issuer="Amazon")],
    )

    assert out.exists()
    text = _all_text(out)
    assert "Jordan Casey" in text
    assert "jordan.casey@example.com | (555) 123-4567" in text
    assert "Victoria, BC, Canada" in text
    assert "Backend engineer tailored for this role." in text
    assert "Java, AWS" in text
    assert "Senior Engineer, Acme" in text
    assert "Jan 2021 - Present" in text
    assert "Did the thing well" in text
    assert "Some University" in text
    assert "AWS Certified, Amazon" in text


def test_render_omits_empty_sections(tmp_path):
    out = tmp_path / "resume.docx"
    render_tailored_resume(
        out,
        candidate_name="Jordan Casey",
        contact_line=None,
        address=None,
        summary="",
        skills_order=[],
        employment_bullets=[],
        education=[],
        certifications=[],
    )

    text = _all_text(out)
    assert "Skills" not in text
    assert "Education" not in text
    assert "Certifications" not in text
