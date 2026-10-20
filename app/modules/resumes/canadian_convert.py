"""Converting the member's resume to Canadian format (the checks and fixes are
in app/modules/resumes/canadian.py).

- A Word resume is fixed inside the member's own file: lines are removed or
  reworded in place (app/modules/resumes/documents/docx_edit.py), so fonts,
  layout and tables stay as designed.
- A PDF can't be edited, so its content goes into the standard layout (a new
  Word file), like a structural edit in the resume editor. That layout already
  writes dates as month and year and has no photo, declaration or references.

Either way the result is a new resume version -- the previous one is kept, and
analyses and tailored resumes made from it still trace back to it.

Canadian equivalents of job titles are suggested by the analysis provider (one
call, counted as a resume action) or typed by the member, and always added next
to the original title, never in its place. A suggestion that sounds more senior
than the original is discarded.
"""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.exceptions import AppError
from app.core.tenancy import require_tenant
from app.integrations.ai.factory import AIProviderConfigError, build_ai_provider
from app.integrations.ai.settings import AISettings
from app.modules.resumes import canadian, uploads
from app.modules.resumes.canadian import DATES, DECLARATION, HIGH_SCHOOL, PERSONAL, PHOTO, REFERENCES, SPELLING
from app.modules.resumes.documents.docx_edit import remove_photos, tailor_docx
from app.modules.resumes.editor import BulletEdit, EducationEdit, ResumeEdit, RoleEdit, save_edit
from app.modules.resumes.models import CandidateProfile
from app.modules.resumes.tailor import _base_docx
from app.modules.usage import quota
from app.modules.usage.ai_calls import AICallLog
from app.modules.usage.models import ActivityKind

logger = logging.getLogger(__name__)

OPERATION = "canadian_titles"
MAX_ROLES = 15
MAX_TITLE = 60
# What the standard layout (a PDF resume's conversion) does whatever is chosen.
LAYOUT_FIXES = {PHOTO, DECLARATION, REFERENCES, DATES}


class TitleSuggestionError(AppError):
    """The analysis provider couldn't suggest titles just now."""


# ---- reading the member's resume -----------------------------------------------------------

def _resume_path(current: CandidateProfile, folder: Path) -> Path | None:
    """The resume file to check: the Word file, else the PDF (restored from the
    database into `folder` when it's no longer on disk)."""
    docx = _base_docx(current)  # raises BaseResumeMissingError for a lost Word file
    if docx is not None:
        return docx
    path = uploads.resume_file(current)
    if path is None and current.source_file:
        path = folder / f"resume{Path(current.raw_resume_path).suffix.lower() or '.pdf'}"
        path.write_bytes(current.source_file)
    return path


@dataclass
class Review:
    """The check of the member's current resume, for the page."""
    findings: list[canadian.Finding]
    is_docx: bool
    pages: int
    dropped_sections: list[str] = field(default_factory=list)  # PDF: sections the standard layout leaves out

    @property
    def counts(self) -> dict[str, int]:
        return {level: sum(1 for f in self.findings if f.level == level) for level in canadian.LEVELS}

    def fixed_by_layout(self, finding: canadian.Finding) -> bool:
        return not self.is_docx and finding.key in LAYOUT_FIXES


def review(current: CandidateProfile) -> Review | None:
    """None when the resume file can't be read."""
    with tempfile.TemporaryDirectory() as folder:
        path = _resume_path(current, Path(folder))
        if path is None:
            return None
        try:
            doc = canadian.read(path)
        except Exception:
            logger.warning("Couldn't read resume version %s for the Canadian check", current.version, exc_info=True)
            return None
    dropped = []
    if not doc.is_docx:
        for part in (current.raw_unparsed_sections or "").split("\n\n---\n\n"):
            match = re.match(r"\[(.+?)\]\n", part)
            if match and match.group(1) != "PREAMBLE":
                dropped.append(match.group(1))
    return Review(canadian.check(doc), doc.is_docx, doc.pages, dropped)


# ---- Canadian job titles ----------------------------------------------------------------------

_RANKS = [
    (7, re.compile(r"(?i)\b(?:chief|ceo|cfo|cto|coo|cio|owner|founder|managing\s+partner)\b|(?<!vice\s)(?<!vice-)\bpresident\b")),
    (6, re.compile(r"(?i)\b(?:vice[\s-]president|s?vp|avp|evp)\b")),
    (5, re.compile(r"(?i)\b(?:director|head)\b")),
    (4, re.compile(r"(?i)\b(?:manager|gm)\b")),
    (3, re.compile(r"(?i)\b(?:senior|sr|lead|principal|supervisor|team\s+leader)\b")),
    (1, re.compile(r"(?i)\b(?:junior|jr|assistant|trainee|intern|apprentice|entry)\b")),
]


def seniority(title: str) -> int:
    """A rough level, to keep a suggested title from sounding more senior."""
    return next((rank for rank, pattern in _RANKS if pattern.search(title)), 2)


def clean_title(value: str | None) -> str:
    return " ".join(str(value or "").replace("(", " ").replace(")", " ").split()).strip(" ,.;:-–")[:MAX_TITLE]


def _acceptable(original: str, equivalent: str) -> bool:
    return bool(equivalent) and equivalent.casefold() != original.casefold() and seniority(equivalent) <= seniority(original)


def titles_available(session: Session) -> bool:
    """Whether a provider that can suggest titles is set up (not the free mock)."""
    try:
        config = user_config(session).ai
        return config.provider != "mock" and build_ai_provider(config, AISettings()) is not None
    except (ConfigError, AIProviderConfigError):
        return False


def suggest_titles(session: Session, current: CandidateProfile) -> dict[int, str]:
    """{employment id: Canadian equivalent} for the titles that need one.
    Raises TitleSuggestionError when the provider fails (AIProviderConfigError
    when it isn't set up)."""
    config = user_config(session).ai
    roles = [{"id": e.id, "title": e.title, "company": e.company, "location": e.location or ""}
             for e in current.employments[:MAX_ROLES]]
    if not roles:
        return {}
    provider = build_ai_provider(config, AISettings())
    try:
        answer = provider.canadian_titles(roles)
    except Exception as exc:
        logger.warning("Canadian title suggestions failed", exc_info=True)
        raise TitleSuggestionError("Title suggestions aren't available right now. Please try again shortly, "
                                   "or type the equivalents yourself.") from exc
    finally:
        usage = getattr(provider, "last_usage", None)
        session.add(AICallLog(provider=config.provider, operation=OPERATION,
                              tokens_in=usage.tokens_in if usage else None, tokens_out=usage.tokens_out if usage else None,
                              estimated_cost=usage.estimated_cost if usage else None, cache_hit=False))
        session.commit()
    titles = {role["id"]: role["title"] for role in roles}
    suggestions = {}
    for role_id, value in (answer or {}).items():
        equivalent = clean_title(value)
        if role_id in titles and _acceptable(titles[role_id], equivalent):
            suggestions[role_id] = equivalent
    return suggestions


# ---- converting ----------------------------------------------------------------------------------

@dataclass
class Conversion:
    profile: CandidateProfile | None  # the new version; None when nothing changed
    done: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    standard_layout: bool = False


def convert(session: Session, current: CandidateProfile, chosen: set[str], titles: dict[int, str]) -> Conversion:
    """Apply the `chosen` fixes and title equivalents ({employment id: title}) as
    a new resume version. Raises BaseResumeMissingError when the Word file is lost."""
    by_id = {e.id: e for e in current.employments}
    titles = {role_id: clean_title(t) for role_id, t in titles.items() if role_id in by_id and clean_title(t)}
    titles = {role_id: t for role_id, t in titles.items() if t.casefold() != by_id[role_id].title.casefold()}
    base = _base_docx(current)
    result = _convert_docx(session, current, base, chosen, titles) if base else _convert_pdf(session, current, chosen, titles)
    if result.profile is not None:
        quota.record(session, require_tenant(session), ActivityKind.RESUME_CANADIAN,
                     f"Version {result.profile.version}: {', '.join(result.done)}"[:300])
    return result


def _convert_docx(session: Session, current: CandidateProfile, base: Path, chosen: set[str],
                  titles: dict[int, str]) -> Conversion:
    by_id = {e.id: e for e in current.employments}
    doc = canadian.read_docx(base)
    plan = canadian.plan(doc, chosen, {by_id[role_id].title: t for role_id, t in titles.items()})
    problems = [f"Couldn't find the job title \"{title}\" in your Word file as written, so it's unchanged."
                for title in plan.titles_missed]
    if not plan.edits and not plan.photos:
        return Conversion(None, problems=problems)
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "resume_canadian.docx"
        replacements = [(doc.lines[i].text, new) for i, new in plan.edits.items() if new is not None]
        removals = [doc.lines[i].text for i, new in plan.edits.items() if new is None]
        report = tailor_docx(base, out, replacements, remove=removals)
        if plan.photos:
            remove_photos(out)
        if report.not_found:
            problems.append(f"{len(report.not_found)} change{'s' if len(report.not_found) != 1 else ''} couldn't be "
                            "made in your Word file, so those lines are as they were.")
        imported, error = uploads.import_resume_file(session, out.name, out.read_bytes(), force=True, record=False)
    if error:
        return Conversion(None, problems=[*problems, error])
    return Conversion(imported.profile, plan.done, problems)


def _convert_pdf(session: Session, current: CandidateProfile, chosen: set[str], titles: dict[int, str]) -> Conversion:
    """The standard layout, from what the app read from the PDF."""
    done = ["moved your resume into the standard layout (dates as month and year, no photo, declaration or "
            "references)"]
    words = 0

    def spell(text: str | None) -> str:
        nonlocal words
        if not text or SPELLING not in chosen:
            return text or ""
        new, changed = canadian.respell(text)
        words += len(changed)
        return new

    contact = current.contact_line or ""
    if PERSONAL in chosen and contact:
        reduced, kinds = canadian.without_personal(contact)
        if kinds:
            contact = reduced or ""
            done.append("removed personal details from your contact line")
    roles = []
    for e in current.employments:
        title = f"{e.title} ({titles[e.id]})" if e.id in titles else e.title
        bullets = [BulletEdit(b.id, spell(b.text)) for b in sorted(e.bullets, key=lambda b: b.order_index)]
        roles.append(RoleEdit(e.id, title, e.company, e.location or "", e.start_date, e.end_date, e.is_current, bullets))
    education = [EducationEdit(ed.institution, ed.degree or "", ed.field_of_study or "", ed.end_date)
                 for ed in current.education]
    if HIGH_SCHOOL in chosen:
        kept = [ed for ed in education
                if not canadian.is_high_school(f"{ed.degree} {ed.field_of_study} {ed.institution}")]
        if kept and len(kept) < len(education):
            education = kept
            done.append("removed high school")
    edit = ResumeEdit(
        full_name=current.full_name or "", contact_line=contact, summary=spell(current.summary),
        skills=[s.name for s in current.skills], roles=roles, education=education,
        certifications=[c.name for c in current.certifications],
        projects=[(p.name, spell(p.description)) for p in current.projects])
    if words:
        done.append(f"changed the spelling of {words} word{'s' if words != 1 else ''}")
    if titles:
        done.append(f"added the Canadian equivalent to {len(titles)} job title{'s' if len(titles) != 1 else ''}")
    saved = save_edit(session, current, edit, base_docx=None, upload_dir=uploads.UPLOAD_DIR, allow_layout_change=True)
    return Conversion(saved.profile, done, standard_layout=True)
