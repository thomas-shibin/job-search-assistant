"""Editing your resume in the browser: rewording and removals are written into
your own Word file (design kept); structural changes need your consent before
switching to the standard layout; every save is a new version."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database.base import Base
from app.core.database.session import get_session
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.resumes.models import CandidateProfile
from app.modules.resumes.parser import import_resume

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_resume.docx"


@pytest.fixture()
def editor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    resume = tmp_path / "resume.docx"
    shutil.copy(SAMPLE, resume)
    with factory() as s:
        import_resume(s, resume)

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    import main

    main.app.dependency_overrides[get_session] = override_get_session
    yield TestClient(main.app), factory
    main.app.dependency_overrides.pop(get_session, None)


def _active(factory) -> CandidateProfile:
    with factory() as s:
        profile = s.query(CandidateProfile).filter_by(is_active=True).one()
        _ = ([(e.bullets, e.title) for e in profile.employments], profile.skills, profile.education,
             profile.certifications, profile.projects)
        s.expunge_all()
        return profile


def _form_for(profile: CandidateProfile) -> dict:
    """What the editor page submits for an unchanged resume."""
    data = {"full_name": profile.full_name or "", "contact_line": profile.contact_line or "",
            "summary": profile.summary or "", "skills": "\n".join(s.name for s in profile.skills),
            "certifications": "\n".join(c.name for c in profile.certifications)}
    for i, emp in enumerate(profile.employments):
        p = f"role-{i}-"
        data.update({p + "id": str(emp.id), p + "title": emp.title, p + "company": emp.company,
                     p + "location": emp.location or "",
                     p + "start": emp.start_date.strftime("%Y-%m") if emp.start_date else "",
                     p + "end": emp.end_date.strftime("%Y-%m") if emp.end_date else "",
                     p + "current": "1" if emp.is_current else ""})
        for j, bullet in enumerate(sorted(emp.bullets, key=lambda b: b.order_index)):
            data[f"{p}bullet-{j}-id"] = str(bullet.id)
            data[f"{p}bullet-{j}-text"] = bullet.text
    for i, edu in enumerate(profile.education):
        p = f"edu-{i}-"
        data.update({p + "institution": edu.institution, p + "degree": edu.degree or "",
                     p + "field": edu.field_of_study or "",
                     p + "end": edu.end_date.strftime("%Y-%m") if edu.end_date else ""})
    return data


def _docx_text(path: str) -> str:
    full = Path(path) if Path(path).is_absolute() else Path(__file__).resolve().parents[2] / path
    return "\n".join(p.text for p in Document(str(full)).paragraphs)


def test_editor_page_shows_the_current_resume(editor):
    http, factory = editor
    profile = _active(factory)
    page = http.get("/resume/edit").text
    assert profile.employments[0].title in page and profile.employments[0].bullets[0].text in page


def test_rewording_and_removing_bullets_keeps_the_word_design(editor):
    http, factory = editor
    before = _active(factory)
    first, second = sorted(before.employments[0].bullets, key=lambda b: b.order_index)[:2]
    data = _form_for(before)
    data["role-0-bullet-0-text"] = "Led a team of five to rebuild the billing platform"
    data["role-0-bullet-1-delete"] = "1"

    response = http.post("/resume/edit", data=data, follow_redirects=False)

    assert response.status_code == 303 and "standard layout" not in response.headers["location"]
    after = _active(factory)
    assert after.version == before.version + 1
    texts = [b.text for b in after.employments[0].bullets]
    assert texts[0] == "Led a team of five to rebuild the billing platform" and second.text not in texts
    document = _docx_text(after.raw_resume_path)
    assert "Led a team of five to rebuild the billing platform" in document
    assert first.text not in document and second.text not in document
    # Everything else in the Word file is untouched -- e.g. the name and headings.
    assert (before.full_name or "") in document


def test_structural_changes_need_consent_then_use_the_standard_layout(editor):
    http, factory = editor
    before = _active(factory)
    data = _form_for(before)
    n = len(before.employments)
    data.update({f"role-{n}-title": "Volunteer Coach", f"role-{n}-company": "City League",
                 f"role-{n}-start": "2019-04", f"role-{n}-new-bullets": "Coached 20 kids\nRan weekly practices"})

    asked = http.post("/resume/edit", data=data)
    assert asked.status_code == 400 and "standard layout" in asked.text and "adding a role" in asked.text
    assert _active(factory).version == before.version  # nothing saved yet

    response = http.post("/resume/edit", data={**data, "allow_layout_change": "1"}, follow_redirects=False)
    assert response.status_code == 303
    after = _active(factory)
    assert after.version == before.version + 1
    assert [e.title for e in after.employments][-1] == "Volunteer Coach"
    assert "Coached 20 kids" in _docx_text(after.raw_resume_path)


def test_invalid_edits_are_explained_and_nothing_is_saved(editor):
    http, factory = editor
    before = _active(factory)
    data = _form_for(before)
    data["role-0-company"] = ""
    data["role-0-start"] = "not-a-month"

    response = http.post("/resume/edit", data=data)

    assert response.status_code == 400
    assert "needs a job title and a company" in response.text and "isn&#39;t a month" in response.text
    assert _active(factory).version == before.version
