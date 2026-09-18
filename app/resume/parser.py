"""Deterministic parsing of a base resume (.docx) into the structured
CandidateProfile ground truth.

No AI calls happen here. Per the truthfulness guardrail (spec section 8), the
structured data produced by this module is the single source of truth every
later AI-generated resume claim must trace back to, so this parser favors
"store it somewhere, even unstructured" over silently discarding anything it
can't confidently classify.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docx import Document
from sqlalchemy.orm import Session

from app.models.candidate import (
    AccomplishmentBullet,
    CandidateProfile,
    Certification,
    Education,
    Employment,
    Project,
    Skill,
)
from app.utils.hashing import content_hash

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "professional summary", "profile", "objective", "career summary"),
    "experience": (
        "experience",
        "employment",
        "work history",
        "professional experience",
        "work experience",
        "employment history",
    ),
    "skills": ("skills", "technical skills", "core competencies", "technologies", "key skills"),
    "education": ("education",),
    "certifications": ("certifications", "certificates", "licenses", "licenses and certifications"),
    "projects": ("projects", "personal projects", "key projects", "notable projects"),
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_DATE_TOKEN = r"[A-Za-z]+\.?\s+\d{4}|\d{4}|present|current"
_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE_TOKEN})\s*(?:-|–|—|to)\s*(?P<end>{_DATE_TOKEN})",
    re.IGNORECASE,
)
_TRAILING_YEAR_RE = re.compile(r"(19|20)\d{2}\s*$")
_BULLET_CHARS = ("•", "-", "*", "‣", "●", "◦", "·")
_TITLE_KEYWORDS = (
    "engineer", "developer", "manager", "architect", "lead", "director",
    "analyst", "scientist", "consultant", "specialist", "administrator",
    "designer", "coordinator",
)


@dataclass
class ParsedEmployment:
    company: str
    title: str
    location: str | None
    start_date: date | None
    end_date: date | None
    is_current: bool
    bullets: list[str] = field(default_factory=list)


@dataclass
class ParsedEducation:
    institution: str
    degree: str | None
    field_of_study: str | None
    start_date: date | None
    end_date: date | None
    raw_line: str


@dataclass
class ParsedCertification:
    name: str
    issuer: str | None
    issue_date: date | None
    raw_line: str


@dataclass
class ParsedProject:
    name: str
    description: str | None


@dataclass
class ParsedResume:
    summary: str | None
    employments: list[ParsedEmployment]
    skills: list[str]
    education: list[ParsedEducation]
    certifications: list[ParsedCertification]
    projects: list[ParsedProject]
    raw_unparsed_sections: list[str]
    raw_text_for_hash: str


@dataclass
class _Para:
    text: str
    is_heading_style: bool
    is_bullet_style: bool
    is_bold_whole_line: bool


@dataclass
class ImportResult:
    profile: CandidateProfile
    created: bool
    counts: dict[str, int]


def _extract_paragraphs(doc: Document) -> list[_Para]:
    paragraphs: list[_Para] = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style_name = (p.style.name or "") if p.style else ""
        runs = [r for r in p.runs if r.text.strip()]
        is_bold_whole_line = bool(runs) and all(r.bold for r in runs)
        paragraphs.append(
            _Para(
                text=text,
                is_heading_style=style_name.lower().startswith("heading"),
                is_bullet_style="list" in style_name.lower(),
                is_bold_whole_line=is_bold_whole_line,
            )
        )
    return paragraphs


def _is_section_boundary(p: _Para) -> bool:
    if p.is_heading_style:
        return True
    if p.is_bullet_style:
        return False
    return len(p.text) <= 40 and (p.text.isupper() or p.is_bold_whole_line)


def _match_section_key(header_text: str) -> str | None:
    normalized = header_text.strip().rstrip(":").strip().lower()
    for key, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return key
    return None


def _strip_bullet_char(text: str) -> str:
    stripped = text.strip()
    for ch in _BULLET_CHARS:
        if stripped.startswith(ch):
            return stripped[len(ch):].strip()
    return stripped


def _parse_month_year(token: str) -> date | None:
    token = token.strip().rstrip(".,")
    if re.fullmatch(r"\d{4}", token):
        return date(int(token), 1, 1)
    m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{4})", token)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return date(int(m.group(2)), month, 1)
    return None


def _split_title_company(remainder: str) -> tuple[str, str]:
    for delim_pattern in (r"\|", r"[–—]", r","):
        parts = re.split(delim_pattern, remainder, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(" -–—"), parts[1].strip(" -–—")
    return remainder.strip(), ""


def _parse_job_header(line: str) -> ParsedEmployment | None:
    m = _DATE_RANGE_RE.search(line)
    if not m:
        return None

    start_date = _parse_month_year(m.group("start"))
    is_current = m.group("end").strip().lower() in ("present", "current")
    end_date = None if is_current else _parse_month_year(m.group("end"))

    remainder = line[: m.start()] + line[m.end():]
    remainder = re.sub(r"\(\s*\)", "", remainder)
    remainder = remainder.strip(" -–—()|,\t")

    title, company = _split_title_company(remainder)
    title_lower, company_lower = title.lower(), company.lower()
    title_has_kw = any(kw in title_lower for kw in _TITLE_KEYWORDS)
    company_has_kw = any(kw in company_lower for kw in _TITLE_KEYWORDS)
    if company_has_kw and not title_has_kw:
        title, company = company, title

    return ParsedEmployment(
        company=company or "Unknown",
        title=title or remainder,
        location=None,
        start_date=start_date,
        end_date=end_date,
        is_current=is_current,
    )


def _parse_experience_section(paras: list[_Para]) -> tuple[list[ParsedEmployment], list[str]]:
    employments: list[ParsedEmployment] = []
    unparsed: list[str] = []
    preamble: list[str] = []
    current: ParsedEmployment | None = None

    for p in paras:
        if not p.is_bullet_style:
            job = _parse_job_header(p.text)
            if job is not None:
                current = job
                employments.append(current)
                continue
        if current is not None:
            current.bullets.append(_strip_bullet_char(p.text))
        else:
            preamble.append(p.text)

    if preamble:
        unparsed.append("[EXPERIENCE (unclassified)]\n" + "\n".join(preamble))
    return employments, unparsed


def _parse_skills_section(paras: list[_Para]) -> list[str]:
    skills: list[str] = []
    seen: set[str] = set()
    for p in paras:
        for token in re.split(r"[,|;•●•]", p.text):
            token = token.strip(" \t.")
            if token and token.lower() not in seen:
                seen.add(token.lower())
                skills.append(token)
    return skills


def _parse_education_line(line: str) -> ParsedEducation:
    m = _DATE_RANGE_RE.search(line)
    start_date = end_date = None
    remainder = line
    if m:
        start_date = _parse_month_year(m.group("start"))
        end_token = m.group("end").strip().lower()
        end_date = None if end_token in ("present", "current") else _parse_month_year(m.group("end"))
        remainder = line[: m.start()] + line[m.end():]

    remainder = re.sub(r"\(\s*\)", "", remainder).strip(" -–—()|,\t")
    parts = re.split(r",", remainder, maxsplit=1)
    if len(parts) == 2:
        degree_and_field, institution = parts[0].strip(), parts[1].strip()
    else:
        degree_and_field, institution = "", remainder

    degree, field_of_study = degree_and_field or None, None
    m2 = re.search(r"(.+?)\s+in\s+(.+)", degree_and_field, re.IGNORECASE)
    if m2:
        degree, field_of_study = m2.group(1).strip(), m2.group(2).strip()

    return ParsedEducation(
        institution=institution or line,
        degree=degree,
        field_of_study=field_of_study,
        start_date=start_date,
        end_date=end_date,
        raw_line=line,
    )


def _parse_certification_line(line: str) -> ParsedCertification:
    year_match = _TRAILING_YEAR_RE.search(line)
    issue_date = None
    remainder = line
    if year_match:
        issue_date = date(int(year_match.group()), 1, 1)
        remainder = line[: year_match.start()].rstrip(", ")

    parts = re.split(r",", remainder, maxsplit=1)
    if len(parts) == 2:
        name, issuer = parts[0].strip(), parts[1].strip()
    else:
        name, issuer = remainder.strip(), None

    return ParsedCertification(name=name or line, issuer=issuer, issue_date=issue_date, raw_line=line)


def _parse_projects_section(paras: list[_Para]) -> list[ParsedProject]:
    projects: list[ParsedProject] = []
    for p in paras:
        if not p.is_bullet_style:
            projects.append(ParsedProject(name=p.text, description=None))
        elif projects:
            desc = _strip_bullet_char(p.text)
            projects[-1].description = (
                desc if projects[-1].description is None else f"{projects[-1].description}; {desc}"
            )
    return projects


def parse_resume_docx(path: Path) -> ParsedResume:
    doc = Document(str(path))
    paragraphs = _extract_paragraphs(doc)

    buckets: list[tuple[str | None, str | None, list[_Para]]] = []
    current_key: str | None = None
    current_header: str | None = None
    current_paras: list[_Para] = []

    for p in paragraphs:
        if _is_section_boundary(p):
            buckets.append((current_key, current_header, current_paras))
            current_key = _match_section_key(p.text)
            current_header = p.text
            current_paras = []
        else:
            current_paras.append(p)
    buckets.append((current_key, current_header, current_paras))

    summary_parts: list[str] = []
    employments: list[ParsedEmployment] = []
    skills: list[str] = []
    education: list[ParsedEducation] = []
    certifications: list[ParsedCertification] = []
    projects: list[ParsedProject] = []
    raw_unparsed: list[str] = []

    for key, header, paras in buckets:
        if not paras:
            continue
        if key is None and header is None:
            raw_unparsed.append("[PREAMBLE]\n" + "\n".join(p.text for p in paras))
        elif key is None:
            raw_unparsed.append(f"[{header}]\n" + "\n".join(p.text for p in paras))
        elif key == "summary":
            summary_parts.extend(p.text for p in paras)
        elif key == "experience":
            parsed_jobs, exp_unparsed = _parse_experience_section(paras)
            employments.extend(parsed_jobs)
            raw_unparsed.extend(exp_unparsed)
        elif key == "skills":
            skills.extend(_parse_skills_section(paras))
        elif key == "education":
            education.extend(_parse_education_line(p.text) for p in paras)
        elif key == "certifications":
            certifications.extend(_parse_certification_line(p.text) for p in paras)
        elif key == "projects":
            projects.extend(_parse_projects_section(paras))

    raw_text_for_hash = "\n".join(p.text for p in paragraphs)

    return ParsedResume(
        summary=" ".join(summary_parts) if summary_parts else None,
        employments=employments,
        skills=skills,
        education=education,
        certifications=certifications,
        projects=projects,
        raw_unparsed_sections=raw_unparsed,
        raw_text_for_hash=raw_text_for_hash,
    )


def import_resume(session: Session, docx_path: Path, *, force: bool = False) -> ImportResult:
    """Parse ``docx_path`` and persist it as a new CandidateProfile version.

    If the document's content is unchanged from the currently active profile,
    this is a no-op (``created=False``) unless ``force`` is set.
    """
    parsed = parse_resume_docx(docx_path)
    file_hash = content_hash(parsed.raw_text_for_hash)

    existing_active = (
        session.query(CandidateProfile)
        .filter_by(is_active=True)
        .order_by(CandidateProfile.version.desc())
        .first()
    )

    if existing_active is not None and existing_active.source_hash == file_hash and not force:
        counts = {
            "employments": len(existing_active.employments),
            "bullets": sum(len(e.bullets) for e in existing_active.employments),
            "skills": len(existing_active.skills),
            "education": len(existing_active.education),
            "certifications": len(existing_active.certifications),
            "projects": len(existing_active.projects),
        }
        return ImportResult(profile=existing_active, created=False, counts=counts)

    new_version = existing_active.version + 1 if existing_active is not None else 1
    if existing_active is not None:
        existing_active.is_active = False

    profile = CandidateProfile(
        summary=parsed.summary,
        raw_resume_path=str(docx_path),
        source_hash=file_hash,
        version=new_version,
        is_active=True,
        raw_unparsed_sections="\n\n---\n\n".join(parsed.raw_unparsed_sections) or None,
    )

    for job in parsed.employments:
        employment = Employment(
            company=job.company,
            title=job.title,
            location=job.location,
            start_date=job.start_date,
            end_date=job.end_date,
            is_current=job.is_current,
        )
        for idx, bullet_text in enumerate(job.bullets):
            employment.bullets.append(AccomplishmentBullet(text=bullet_text, order_index=idx))
        profile.employments.append(employment)

    for skill_name in parsed.skills:
        profile.skills.append(Skill(name=skill_name))

    for edu in parsed.education:
        profile.education.append(
            Education(
                institution=edu.institution,
                degree=edu.degree,
                field_of_study=edu.field_of_study,
                start_date=edu.start_date,
                end_date=edu.end_date,
            )
        )

    for cert in parsed.certifications:
        profile.certifications.append(
            Certification(name=cert.name, issuer=cert.issuer, issue_date=cert.issue_date)
        )

    for proj in parsed.projects:
        profile.projects.append(Project(name=proj.name, description=proj.description))

    session.add(profile)
    session.commit()
    session.refresh(profile)

    counts = {
        "employments": len(profile.employments),
        "bullets": sum(len(e.bullets) for e in profile.employments),
        "skills": len(profile.skills),
        "education": len(profile.education),
        "certifications": len(profile.certifications),
        "projects": len(profile.projects),
    }
    return ImportResult(profile=profile, created=True, counts=counts)
