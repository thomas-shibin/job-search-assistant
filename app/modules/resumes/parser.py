"""Deterministic parsing of a base resume (.docx or .pdf) into the structured
CandidateProfile ground truth.

No AI calls happen here. Per the truthfulness guardrail (spec section 8), the
structured data produced by this module is the single source of truth every
later AI-generated resume claim must trace back to, so this parser favors
"store it somewhere, even unstructured" over silently discarding anything it
can't confidently classify.

.docx and .pdf inputs both get converted into a common intermediate
representation (``LineInfo``) before section-detection/job-entry parsing runs,
so that logic is written once and shared across formats. PDF extraction is
inherently lossier than docx: there is no bold/paragraph-style signal, so
heading detection for PDFs falls back to short-line + ALL-CAPS/known-alias
heuristics only. Resumes with unusual PDF layouts (multi-column, heavy use of
tables/text-boxes) may land more content in ``raw_unparsed_sections`` than an
equivalent docx would.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator

import pdfplumber
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database.session import PROJECT_ROOT
from app.modules.resumes.models import (
    AccomplishmentBullet,
    CandidateProfile,
    Certification,
    Education,
    Employment,
    Project,
    Skill,
)
from app.shared.hashing import content_hash

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
    "skills": (
        "skills", "technical skills", "core competencies", "technologies",
        "key skills", "core skills",
    ),
    "education": ("education",),
    "certifications": (
        "certifications", "certificates", "licenses", "licenses and certifications",
        "certifications & training", "certifications and training",
    ),
    "projects": ("projects", "personal projects", "key projects", "notable projects"),
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Dash-like separators seen across resume exports, including the fullwidth
# hyphen-minus (U+FF0D) some PDF generators substitute for a plain "-".
_DASH_CHARS = "-–—－"
_DASH_ALT = rf"[{_DASH_CHARS}]|to"
_DATE_TOKEN = r"[A-Za-z]+\.?\s+\d{4}|\d{1,2}/\d{4}|\d{4}|present|current"
_DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE_TOKEN})\s*(?:{_DASH_ALT})\s*(?P<end>{_DATE_TOKEN})",
    re.IGNORECASE,
)
_TRAILING_YEAR_RE = re.compile(r"(19|20)\d{2}\s*$")
_BULLET_CHARS = ("•", "-", "*", "‣", "●", "◦", "·")


def _is_bullet_prefixed(text: str) -> bool:
    """True if ``text`` starts with a bullet glyph.

    PDF exports of a Word bulleted list commonly render the bullet as a
    Wingdings glyph that lands in the Unicode Private Use Area (e.g. "•"
    becomes U+F0B7) rather than a normal bullet character, so that range is
    treated as a bullet prefix too, in addition to the common ASCII/Unicode
    bullet characters.
    """
    stripped = text.lstrip()
    if not stripped:
        return False
    first = stripped[0]
    return first in _BULLET_CHARS or 0xE000 <= ord(first) <= 0xF8FF
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
    full_name: str | None
    contact_line: str | None
    summary: str | None
    employments: list[ParsedEmployment]
    skills: list[str]
    education: list[ParsedEducation]
    certifications: list[ParsedCertification]
    projects: list[ParsedProject]
    raw_unparsed_sections: list[str]
    raw_text_for_hash: str


@dataclass
class LineInfo:
    """One logical line of resume text plus format-agnostic hints.

    docx and PDF extraction both converge on this shape so that section
    detection, job-entry parsing, and bullet collection are written once.
    """
    text: str
    is_heading_hint: bool
    is_bullet_hint: bool


@dataclass
class ImportResult:
    profile: CandidateProfile
    created: bool
    counts: dict[str, int]


def _iter_docx_paragraphs(doc) -> Iterator[tuple[Paragraph, bool]]:
    """Body paragraphs and table-cell paragraphs in document order, flagged
    `in_table`. Resume templates often lay out skills (or the contact header)
    as a table, which `doc.paragraphs` alone skips entirely.
    """
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc), False
        elif child.tag == qn("w:tbl"):
            seen_cells: set[int] = set()
            for row in Table(child, doc).rows:
                for cell in row.cells:
                    if id(cell._tc) in seen_cells:  # a merged cell repeats across the row
                        continue
                    seen_cells.add(id(cell._tc))
                    for paragraph in cell.paragraphs:
                        yield paragraph, True


def _extract_docx_lines(path: Path) -> list[LineInfo]:
    doc = Document(str(path))
    lines: list[LineInfo] = []
    for p, in_table in _iter_docx_paragraphs(doc):
        text = p.text.strip()
        if not text:
            continue
        style_name = (p.style.name or "") if p.style else ""
        runs = [r for r in p.runs if r.text.strip()]
        is_bold_whole_line = bool(runs) and all(r.bold for r in runs)
        is_heading_style = style_name.lower().startswith("heading")
        is_bullet_hint = "list" in style_name.lower()
        # Short bold text inside a table is a cell label (e.g. a skill category),
        # not a section heading.
        is_heading_hint = not is_bullet_hint and not in_table and (
            is_heading_style or (len(text) <= 40 and (text.isupper() or is_bold_whole_line))
        )
        lines.append(LineInfo(text=text, is_heading_hint=is_heading_hint, is_bullet_hint=is_bullet_hint))
    return lines


def _looks_like_heading_text(text: str) -> bool:
    """Format-agnostic heading heuristic: short line that's either ALL CAPS
    or an exact (case-insensitive) match against a known section alias.

    Used as the sole heading signal for PDFs (no style/bold metadata
    available) and as a fallback signal for docx.
    """
    if len(text) > 40:
        return False
    return bool(text.isupper()) or _match_section_key(text) is not None


def _extract_pdf_lines(path: Path) -> list[LineInfo]:
    lines: list[LineInfo] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw_line in text.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                is_bullet_hint = _is_bullet_prefixed(line)
                is_heading_hint = not is_bullet_hint and _looks_like_heading_text(line)
                lines.append(LineInfo(text=line, is_heading_hint=is_heading_hint, is_bullet_hint=is_bullet_hint))
    return lines


def _is_section_boundary(p: LineInfo) -> bool:
    return p.is_heading_hint


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _extract_header_info(preamble_lines: list[str]) -> tuple[str | None, str | None]:
    """The header block above the first recognized section (name, location,
    phone, email, links -- see the module docstring's "store it somewhere"
    principle) isn't a named section a resume author labels, so it can't be
    matched via `SECTION_ALIASES` like everything else. Convention carries
    the whole signal instead: the very first line is the candidate's name
    unless it already looks like contact info itself (an email/phone/a very
    long line), in which case there's no separate name line to find and
    everything becomes the contact line.
    """
    lines = [line.strip() for line in preamble_lines if line.strip()]
    if not lines:
        return None, None

    first = lines[0]
    if _EMAIL_RE.search(first) or _PHONE_RE.search(first) or len(first) > 80:
        return None, " | ".join(lines)

    contact_line = " | ".join(lines[1:]) if len(lines) > 1 else None
    return first, contact_line


def _match_section_key(header_text: str) -> str | None:
    normalized = header_text.strip().rstrip(":").strip().lower()
    for key, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return key
    return None


def _strip_bullet_char(text: str) -> str:
    stripped = text.strip()
    if stripped and (stripped[0] in _BULLET_CHARS or 0xE000 <= ord(stripped[0]) <= 0xF8FF):
        return stripped[1:].strip()
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
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", token)
    if m and 1 <= int(m.group(1)) <= 12:
        return date(int(m.group(2)), int(m.group(1)), 1)
    return None


def _split_title_company(remainder: str) -> tuple[str, str]:
    for delim_pattern in (r"\|", rf"[{_DASH_CHARS}]", r","):
        parts = re.split(delim_pattern, remainder, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(f" {_DASH_CHARS}"), parts[1].strip(f" {_DASH_CHARS}")
    return remainder.strip(), ""


def _split_company_location(line: str) -> tuple[str, str | None]:
    """Split a "Company — Location" line, e.g. one that stands alone below a
    job title/date line rather than sharing a line with them."""
    for delim_pattern in (r"\|", rf"[{_DASH_CHARS}]", r","):
        parts = re.split(delim_pattern, line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(f" {_DASH_CHARS}"), parts[1].strip(f" {_DASH_CHARS}")
    return line.strip(), None


def _parse_job_header(line: str) -> ParsedEmployment | None:
    m = _DATE_RANGE_RE.search(line)
    if not m:
        return None

    start_date = _parse_month_year(m.group("start"))
    is_current = m.group("end").strip().lower() in ("present", "current")
    end_date = None if is_current else _parse_month_year(m.group("end"))

    remainder = line[: m.start()] + line[m.end():]
    remainder = re.sub(r"\(\s*\)", "", remainder)
    remainder = remainder.strip(f" {_DASH_CHARS}()|,\t")

    title, company = _split_title_company(remainder)
    title_lower, company_lower = title.lower(), company.lower()
    title_has_kw = any(kw in title_lower for kw in _TITLE_KEYWORDS)
    company_has_kw = any(kw in company_lower for kw in _TITLE_KEYWORDS)
    if company_has_kw and not title_has_kw:
        title, company = company, title

    return ParsedEmployment(
        company=company,
        title=title or remainder,
        location=None,
        start_date=start_date,
        end_date=end_date,
        is_current=is_current,
    )


def _parse_experience_section(paras: list[LineInfo]) -> tuple[list[ParsedEmployment], list[str]]:
    """Collect job entries and their bullets from an EXPERIENCE section.

    Handles two common layouts: "Title, Company | Dates" all on one line
    (docx-style), and "Title, Dates" on one line with "Company — Location"
    on the very next non-bullet line (a common resume-builder/PDF layout,
    where the date range's remainder yields no company via delimiter
    splitting) — in that case the following line is consumed as the
    company/location rather than being misread as a bullet.
    """
    employments: list[ParsedEmployment] = []
    unparsed: list[str] = []
    preamble: list[str] = []
    current: ParsedEmployment | None = None
    skip_next = False

    for idx, p in enumerate(paras):
        if skip_next:
            skip_next = False
            continue
        if not p.is_bullet_hint:
            job = _parse_job_header(p.text)
            if job is not None:
                if not job.company:
                    next_p = paras[idx + 1] if idx + 1 < len(paras) else None
                    if (
                        next_p is not None
                        and not next_p.is_bullet_hint
                        and _parse_job_header(next_p.text) is None
                    ):
                        job.company, job.location = _split_company_location(next_p.text)
                        skip_next = True
                job.company = job.company or "Unknown"
                current = job
                employments.append(current)
                continue
        if current is not None:
            if p.is_bullet_hint or not current.bullets:
                current.bullets.append(_strip_bullet_char(p.text))
            elif current.bullets[-1].endswith((".", "!", "?", ":")):
                current.bullets.append(_strip_bullet_char(p.text))
            else:
                # No bullet marker and the previous bullet doesn't look
                # sentence-complete: this is very likely wrapped text from a
                # PDF export (a bullet whose text ran onto a second line
                # without repeating the bullet glyph), so merge rather than
                # creating a spurious extra bullet.
                current.bullets[-1] = f"{current.bullets[-1]} {p.text}".strip()
        else:
            preamble.append(p.text)

    if preamble:
        unparsed.append("[EXPERIENCE (unclassified)]\n" + "\n".join(preamble))
    return employments, unparsed


_PUA_CHAR_RE = re.compile(r"[-]")


def _parse_skills_section(paras: list[LineInfo]) -> list[str]:
    skills: list[str] = []
    seen: set[str] = set()
    for p in paras:
        # PDF exports of a Word skills list often use a Wingdings bullet
        # glyph (Private Use Area) as the separator between skills on the
        # same line, not just as a line-leading bullet.
        text = _PUA_CHAR_RE.sub(",", p.text)
        for token in re.split(r"[,|;•●•]", text):
            token = token.strip(" \t.")
            if token and token.lower() not in seen:
                seen.add(token.lower())
                skills.append(token)
    return skills


_DEGREE_KEYWORDS = (
    "bachelor", "master", "phd", "doctor", "b.sc", "m.sc", "b.tech", "m.tech",
    "associate degree", "diploma", "b.e.", "m.e.", "mba", "bsc", "msc",
)
_INSTITUTION_KEYWORDS = ("university", "college", "institute", "polytechnic")


def _parse_education_line(line: str) -> ParsedEducation:
    m = _DATE_RANGE_RE.search(line)
    start_date = end_date = None
    remainder = line
    if m:
        start_date = _parse_month_year(m.group("start"))
        end_token = m.group("end").strip().lower()
        end_date = None if end_token in ("present", "current") else _parse_month_year(m.group("end"))
        remainder = line[: m.start()] + line[m.end():]

    remainder = re.sub(r"\(\s*\)", "", remainder).strip(f" {_DASH_CHARS}()|,\t")
    parts = [part.strip() for part in re.split(r",", remainder, maxsplit=1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        a, b = parts
        a_is_institution = any(kw in a.lower() for kw in _INSTITUTION_KEYWORDS)
        b_is_institution = any(kw in b.lower() for kw in _INSTITUTION_KEYWORDS)
        if a_is_institution and not b_is_institution:
            institution, degree_and_field = a, b
        else:
            degree_and_field, institution = a, b
    else:
        degree_and_field, institution = "", remainder

    degree, field_of_study = degree_and_field or None, None
    m2 = re.search(r"(.+?)\s+in\s+(.+)", degree_and_field, re.IGNORECASE)
    if m2:
        degree, field_of_study = m2.group(1).strip(), m2.group(2).strip()
    else:
        m3 = re.search(rf"(.+?)\s*[{_DASH_CHARS}]\s*(.+)", degree_and_field)
        if m3 and any(kw in m3.group(1).lower() for kw in _DEGREE_KEYWORDS):
            degree, field_of_study = m3.group(1).strip(), m3.group(2).strip()

    return ParsedEducation(
        institution=institution or line,
        degree=degree,
        field_of_study=field_of_study,
        start_date=start_date,
        end_date=end_date,
        raw_line=line,
    )


def _parse_education_section(paras: list[LineInfo]) -> list[ParsedEducation]:
    """Group EDUCATION lines into entries.

    A single line already containing a date range (the common "Degree in
    Field, Institution (dates)" one-liner) is its own entry immediately.
    Otherwise consecutive lines are buffered together (a common layout is
    "Institution" on one line and "Degree — Field" on the next, neither of
    which mentions dates) and flushed as one combined entry once a
    date-bearing line is hit or the section ends.
    """
    entries: list[ParsedEducation] = []
    buffer: list[str] = []
    for p in paras:
        buffer.append(p.text)
        if _DATE_RANGE_RE.search(p.text) or _TRAILING_YEAR_RE.search(p.text):
            entries.append(_parse_education_line(", ".join(buffer)))
            buffer = []
    if buffer:
        entries.append(_parse_education_line(", ".join(buffer)))
    return entries


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


def _parse_projects_section(paras: list[LineInfo]) -> list[ParsedProject]:
    projects: list[ParsedProject] = []
    for p in paras:
        if not p.is_bullet_hint:
            projects.append(ParsedProject(name=p.text, description=None))
        elif projects:
            desc = _strip_bullet_char(p.text)
            projects[-1].description = (
                desc if projects[-1].description is None else f"{projects[-1].description}; {desc}"
            )
    return projects


def _parse_resume_lines(lines: list[LineInfo]) -> ParsedResume:
    """Shared section-detection/extraction logic, format-agnostic.

    Both docx and PDF parsing converge here after producing a list[LineInfo].
    """
    buckets: list[tuple[str | None, str | None, list[LineInfo]]] = []
    current_key: str | None = None
    current_header: str | None = None
    current_paras: list[LineInfo] = []

    for p in lines:
        if _is_section_boundary(p):
            buckets.append((current_key, current_header, current_paras))
            current_key = _match_section_key(p.text)
            current_header = p.text
            current_paras = []
        else:
            current_paras.append(p)
    buckets.append((current_key, current_header, current_paras))

    full_name: str | None = None
    contact_line: str | None = None
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
            full_name, contact_line = _extract_header_info([p.text for p in paras])
            if full_name is None and contact_line is None:
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
            education.extend(_parse_education_section(paras))
        elif key == "certifications":
            certifications.extend(_parse_certification_line(_strip_bullet_char(p.text)) for p in paras)
        elif key == "projects":
            projects.extend(_parse_projects_section(paras))

    raw_text_for_hash = "\n".join(p.text for p in lines)

    return ParsedResume(
        full_name=full_name,
        contact_line=contact_line,
        summary=" ".join(summary_parts) if summary_parts else None,
        employments=employments,
        skills=skills,
        education=education,
        certifications=certifications,
        projects=projects,
        raw_unparsed_sections=raw_unparsed,
        raw_text_for_hash=raw_text_for_hash,
    )


def parse_resume_docx(path: Path) -> ParsedResume:
    return _parse_resume_lines(_extract_docx_lines(path))


def parse_resume_pdf(path: Path) -> ParsedResume:
    return _parse_resume_lines(_extract_pdf_lines(path))


_PARSERS_BY_SUFFIX = {
    ".docx": parse_resume_docx,
    ".pdf": parse_resume_pdf,
}


def parse_resume(path: Path) -> ParsedResume:
    """Dispatch to the right parser based on file extension (.docx or .pdf)."""
    parser = _PARSERS_BY_SUFFIX.get(path.suffix.lower())
    if parser is None:
        supported = ", ".join(sorted(_PARSERS_BY_SUFFIX))
        raise ValueError(f"Unsupported resume file type '{path.suffix}'. Supported types: {supported}")
    return parser(path)


def _resume_file_exists(raw_path: str) -> bool:
    path = Path(raw_path)

    return path.is_file() or (PROJECT_ROOT / path).is_file()


def import_resume(session: Session, resume_path: Path, *, force: bool = False) -> ImportResult:
    """Parse ``resume_path`` (.docx or .pdf) and persist it as a new CandidateProfile version.

    If the document's content is unchanged from the currently active profile,
    this is a no-op (``created=False``) unless ``force`` is set.
    """
    parsed = parse_resume(resume_path)
    file_hash = content_hash(parsed.raw_text_for_hash)

    existing_active = (
        session.query(CandidateProfile)
        .filter_by(is_active=True)
        .order_by(CandidateProfile.version.desc())
        .first()
    )

    if existing_active is not None and existing_active.source_hash == file_hash and not force:
        if existing_active.source_file is None or not _resume_file_exists(existing_active.raw_resume_path):
            # Same content, but the stored original is gone: adopt this copy of it.
            existing_active.raw_resume_path = str(resume_path)
            existing_active.source_file = resume_path.read_bytes()
            session.commit()
        counts = {
            "employments": len(existing_active.employments),
            "bullets": sum(len(e.bullets) for e in existing_active.employments),
            "skills": len(existing_active.skills),
            "education": len(existing_active.education),
            "certifications": len(existing_active.certifications),
            "projects": len(existing_active.projects),
        }
        return ImportResult(profile=existing_active, created=False, counts=counts)

    # After the member has gone back to an older version, the next is still numbered after the newest.
    new_version = (session.query(func.max(CandidateProfile.version)).scalar() or 0) + 1
    if existing_active is not None:
        existing_active.is_active = False

    profile = CandidateProfile(
        full_name=parsed.full_name,
        contact_line=parsed.contact_line,
        summary=parsed.summary,
        raw_resume_path=str(resume_path),
        source_file=resume_path.read_bytes(),
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
