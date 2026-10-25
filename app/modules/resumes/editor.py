"""Edit your resume's content in the browser.

Every save is a new resume version (the previous one is kept, like a
re-upload), so job analyses and tailored resumes made from the old wording
still trace back to it.

The Word file has to change too, because tailoring works on your own document:
- Rewording or removing the summary or bullets is written straight into your
  Word file -- its design is kept.
- Anything else (added roles or bullets, changed titles, dates, skills,
  education...) can't be placed into an arbitrary Word design, so the resume is
  re-created in the standard layout -- only after you agree to that.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.paths import PROJECT_ROOT
from app.core.tenancy import require_tenant
from app.modules.resumes.documents.docx import render_tailored_resume
from app.modules.resumes.documents.docx_edit import tailor_docx
from app.modules.resumes.models import (
    AccomplishmentBullet,
    CandidateProfile,
    Certification,
    Education,
    Employment,
    Project,
    Skill,
)

MAX_TEXT = 3000
MAX_BULLET = 1000
MAX_ITEMS = 100


@dataclass
class BulletEdit:
    source_id: int | None  # the bullet it was, if any
    text: str


@dataclass
class RoleEdit:
    source_id: int | None
    title: str
    company: str
    location: str
    start: date | None
    end: date | None
    is_current: bool
    bullets: list[BulletEdit] = field(default_factory=list)


@dataclass
class EducationEdit:
    institution: str
    degree: str
    field_of_study: str
    end: date | None


@dataclass
class ResumeEdit:
    full_name: str
    contact_line: str
    summary: str
    skills: list[str]
    roles: list[RoleEdit]
    education: list[EducationEdit]
    certifications: list[str]
    projects: list[tuple[str, str]]


@dataclass
class SaveResult:
    profile: CandidateProfile | None = None
    needs_layout_consent: bool = False
    layout_changed: bool = False
    reasons: list[str] = field(default_factory=list)


def _month(value: str) -> date | None:
    """'2021-03' (an <input type=month>) -> date(2021, 3, 1)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        year, month = value.split("-")[:2]
        return date(int(year), int(month), 1)
    except ValueError:
        raise ValueError(f"'{value}' isn't a month (use YYYY-MM).") from None


def _lines(value: str) -> list[str]:
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _clean(value, limit: int = 255) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_edit_form(form) -> tuple[ResumeEdit | None, list[str]]:
    """The submitted editor form as a `ResumeEdit` (or the problems with it)."""
    errors: list[str] = []
    get = lambda name: str(form.get(name, "") or "")  # noqa: E731

    roles: list[RoleEdit] = []
    for index in sorted({int(k.split("-")[1]) for k in form.keys() if k.startswith("role-") and k.split("-")[1].isdigit()}):
        prefix = f"role-{index}-"
        if get(prefix + "delete") == "1":
            continue
        title, company = _clean(get(prefix + "title")), _clean(get(prefix + "company"))
        bullets = []
        for key in sorted((k for k in form.keys() if k.startswith(prefix + "bullet-") and k.endswith("-text")),
                          key=lambda k: int(k.split("-")[3])):
            n = key.split("-")[3]
            text = " ".join(get(key).split())
            if not text or get(f"{prefix}bullet-{n}-delete") == "1":
                continue
            source = get(f"{prefix}bullet-{n}-id")
            bullets.append(BulletEdit(int(source) if source.isdigit() else None, text[:MAX_BULLET]))
        bullets += [BulletEdit(None, text[:MAX_BULLET]) for text in _lines(get(prefix + "new-bullets"))]
        if not (title or company or bullets):
            continue  # an untouched blank "add a role" block
        if not (title and company):
            errors.append(f"Each role needs a job title and a company (role {len(roles) + 1}).")
        try:
            start, end = _month(get(prefix + "start")), _month(get(prefix + "end"))
        except ValueError as exc:
            errors.append(str(exc))
            start = end = None
        if start and end and end < start:
            errors.append(f"{title or 'A role'}: the end date is before the start date.")
        source = get(prefix + "id")
        roles.append(RoleEdit(int(source) if source.isdigit() else None, title, company,
                              _clean(get(prefix + "location")), start, end, get(prefix + "current") == "1", bullets))

    education = []
    for index in sorted({int(k.split("-")[1]) for k in form.keys() if k.startswith("edu-") and k.split("-")[1].isdigit()}):
        prefix = f"edu-{index}-"
        institution = _clean(get(prefix + "institution"))
        if not institution or get(prefix + "delete") == "1":
            continue
        try:
            end = _month(get(prefix + "end"))
        except ValueError as exc:
            errors.append(str(exc))
            end = None
        education.append(EducationEdit(institution, _clean(get(prefix + "degree")), _clean(get(prefix + "field")), end))

    projects = []
    for index in sorted({int(k.split("-")[1]) for k in form.keys() if k.startswith("project-") and k.split("-")[1].isdigit()}):
        prefix = f"project-{index}-"
        name = _clean(get(prefix + "name"))
        if name and get(prefix + "delete") != "1":
            projects.append((name, " ".join(get(prefix + "description").split())[:MAX_TEXT]))

    skills = list(dict.fromkeys(_clean(s, 100) for line in _lines(get("skills")) for s in line.split(",") if s.strip()))
    edit = ResumeEdit(
        full_name=_clean(get("full_name")), contact_line=_clean(get("contact_line"), 512),
        summary=" ".join(get("summary").split())[:MAX_TEXT], skills=skills, roles=roles, education=education,
        certifications=[_clean(c) for c in _lines(get("certifications"))], projects=projects,
    )
    if not edit.full_name:
        errors.append("Enter your name.")
    if not edit.roles:
        errors.append("Keep at least one role.")
    if len(skills) > MAX_ITEMS or sum(len(r.bullets) for r in roles) > MAX_ITEMS * 2:
        errors.append("That's more content than a resume can hold -- please trim it.")
    return (None, errors) if errors else (edit, [])


def _structural_changes(current: CandidateProfile, edit: ResumeEdit) -> list[str]:
    """Changes that can't be written into the Word design (empty: wording/removals only)."""
    reasons = []
    if edit.full_name != (current.full_name or "") or edit.contact_line != (current.contact_line or ""):
        reasons.append("your name or contact line")
    if edit.skills != [s.name for s in current.skills]:
        reasons.append("skills")
    if [(e.institution, e.degree, e.field_of_study, e.end) for e in edit.education] != [
            (e.institution, e.degree or "", e.field_of_study or "", e.end_date) for e in current.education]:
        reasons.append("education")
    if edit.certifications != [c.name for c in current.certifications] or edit.projects != [
            (p.name, p.description or "") for p in current.projects]:
        reasons.append("certifications or projects")
    by_id = {e.id: e for e in current.employments}
    kept_ids = [r.source_id for r in edit.roles]
    if any(role_id not in by_id for role_id in kept_ids):
        reasons.append("adding a role")
    elif kept_ids != [e.id for e in current.employments if e.id in kept_ids]:
        reasons.append("reordering roles")
    for role in edit.roles:
        old = by_id.get(role.source_id)
        if old is None:
            continue
        if (role.title, role.company, role.location, role.start, role.end, role.is_current) != (
                old.title, old.company, old.location or "", old.start_date, old.end_date, old.is_current):
            reasons.append(f"{old.title}'s title, company, location or dates")
        old_bullets = {b.id for b in old.bullets}
        if any(b.source_id not in old_bullets for b in role.bullets):
            reasons.append(f"new bullets under {old.title}")
    if any(e.id not in kept_ids for e in current.employments):
        reasons.append("removing a whole role")
    return list(dict.fromkeys(reasons))


def _new_profile(session: Session, current: CandidateProfile, edit: ResumeEdit) -> CandidateProfile:
    payload = json.dumps([edit.full_name, edit.contact_line, edit.summary, edit.skills,
                          [[r.title, r.company, r.location, str(r.start), str(r.end), r.is_current,
                            [b.text for b in r.bullets]] for r in edit.roles],
                          [[e.institution, e.degree, e.field_of_study, str(e.end)] for e in edit.education],
                          edit.certifications, edit.projects])
    profile = CandidateProfile(
        full_name=edit.full_name, contact_line=edit.contact_line or None, summary=edit.summary or None,
        raw_resume_path=current.raw_resume_path, source_hash=hashlib.sha256(payload.encode()).hexdigest(),
        version=(session.query(func.max(CandidateProfile.version)).scalar() or 0) + 1, is_active=True, raw_unparsed_sections=current.raw_unparsed_sections,
    )
    for role in edit.roles:
        employment = Employment(company=role.company, title=role.title, location=role.location or None,
                                start_date=role.start, end_date=None if role.is_current else role.end,
                                is_current=role.is_current)
        employment.bullets = [AccomplishmentBullet(text=b.text, order_index=i) for i, b in enumerate(role.bullets)]
        profile.employments.append(employment)
    profile.skills = [Skill(name=name) for name in edit.skills]
    profile.education = [Education(institution=e.institution, degree=e.degree or None,
                                   field_of_study=e.field_of_study or None, end_date=e.end) for e in edit.education]
    profile.certifications = [Certification(name=name) for name in edit.certifications]
    profile.projects = [Project(name=name, description=description or None) for name, description in edit.projects]
    return profile


def _edited_path(upload_dir: Path, session: Session, version: int) -> Path:
    folder = upload_dir / f"user_{require_tenant(session)}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{int(time.time())}_{uuid.uuid4().hex[:6]}_resume_v{version}_edited.docx"


def save_edit(session: Session, current: CandidateProfile, edit: ResumeEdit, *, base_docx: Path | None,
              upload_dir: Path, allow_layout_change: bool) -> SaveResult:
    """Save `edit` as the new active resume version."""
    structural = _structural_changes(current, edit)
    profile = _new_profile(session, current, edit)
    out = _edited_path(upload_dir, session, profile.version)

    in_place = base_docx is not None and not structural
    if in_place:
        old_text = {b.id: b.text for e in current.employments for b in e.bullets}
        kept = {b.source_id for r in edit.roles for b in r.bullets}
        replacements = [(old_text[b.source_id], b.text) for r in edit.roles for b in r.bullets
                        if b.source_id in old_text and old_text[b.source_id] != b.text]
        if current.summary and edit.summary != current.summary:
            replacements.insert(0, (current.summary, edit.summary))
        removed = [text for bid, text in old_text.items() if bid not in kept]
        report = tailor_docx(base_docx, out, replacements, remove=removed)
        if report.not_found:  # the Word file didn't contain the text where we expected it
            out.unlink(missing_ok=True)
            in_place = False
            structural = ["wording the Word file doesn't contain as written"]

    if not in_place:
        if base_docx is not None and not allow_layout_change:
            return SaveResult(needs_layout_consent=True, reasons=structural)
        employments = sorted(profile.employments, key=lambda e: (e.is_current, e.start_date or date.min), reverse=True)
        render_tailored_resume(
            out, candidate_name=profile.full_name or "", contact_line=profile.contact_line, address=None,
            summary=profile.summary or "", skills_order=[s.name for s in profile.skills],
            employment_bullets=[(e, [b.text for b in e.bullets]) for e in employments],
            education=list(profile.education), certifications=list(profile.certifications),
        )

    profile.raw_resume_path = str(out.relative_to(PROJECT_ROOT) if out.is_relative_to(PROJECT_ROOT) else out)
    profile.source_file = out.read_bytes()
    current.is_active = False
    session.add(profile)
    session.commit()
    return SaveResult(profile=profile, layout_changed=not in_place and base_docx is not None, reasons=structural)
