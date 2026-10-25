"""The free Canadian resume check anyone can use, signed in or not
(/canadian-resume-check). It runs the same rule-based check as the members'
page (app/modules/resumes/canadian.py) plus the ATS readability check
(app/modules/resumes/ats.py) -- no AI, so nothing to pay for. Fixing the
resume needs an account.

The page shows the resume's own lines with the problems marked, and a preview
of the fixed resume with the score the same check gives it afterwards: the
"after" score is measured, never assumed.

The uploaded file is read in a temporary folder that's deleted straight away.
So the visitor needn't upload it again after signing up to fix it, the file is
also held in memory -- never on disk -- for `HOLD_SECONDS`, under a random
token kept in their browser's cookie (`COOKIE`). Signing in and choosing "Fix
my resume" takes it from there (`take`); otherwise it expires.
"""
from __future__ import annotations

import io
import logging
import secrets
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.modules.auth.ratelimit import RateLimiter
from app.modules.resumes import ats, canadian
from app.modules.resumes.uploads import MAX_RESUME_BYTES

logger = logging.getLogger(__name__)

COOKIE = "jsa_resume_check"
HOLD_SECONDS = 30 * 60
MAX_PAGES = 10            # resumes are 1-3 pages; longer PDFs are something else
_MAX_UNZIPPED = 60_000_000  # a Word file's parts, unpacked: refuses zip bombs
_MAX_HELD = 200
_MAX_HELD_BYTES = 150_000_000

limiter = RateLimiter(limit=20, window_seconds=3600)  # checks per IP


class CheckError(ValueError):
    """A message for the visitor about their file."""


READY = 90  # both scores at least this: nothing worth fixing


def counts(findings: list[canadian.Finding]) -> dict[str, int]:
    return {level: sum(1 for f in findings if f.level == level) for level in canadian.LEVELS}


def score(findings: list[canadian.Finding]) -> int:
    """Out of 100: each "must fix" costs the most."""
    n = counts(findings)
    return max(0, 100 - 20 * n[canadian.MUST] - 8 * n[canadian.SHOULD] - 2 * n[canadian.TIP])


@dataclass
class PreviewLine:
    text: str
    heading: bool
    changed: bool


@dataclass
class PublicReview:
    findings: list[canadian.Finding]
    is_docx: bool
    pages: int
    filename: str
    ats: list[canadian.Finding] = field(default_factory=list)
    lines: list[canadian.Line] = field(default_factory=list)
    photos: int = 0
    preview: list[PreviewLine] = field(default_factory=list)  # the resume after "Fix my resume"
    removed: int = 0  # lines the fixes take out
    after: list[canadian.Finding] = field(default_factory=list)  # Canadian findings the fixed resume still has
    ats_after: list[canadian.Finding] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return counts(self.findings)

    @property
    def score(self) -> int:
        return score(self.findings)

    @property
    def ats_score(self) -> int:
        return score(self.ats)

    @property
    def after_score(self) -> int:
        return score(self.after)

    @property
    def ats_after_score(self) -> int:
        return score(self.ats_after)

    @property
    def verdict(self) -> str:
        if self.score >= READY:
            return "Ready for Canadian employers"
        return "Nearly there" if self.score >= 70 else "Needs work for Canadian employers"

    @property
    def ats_verdict(self) -> str:
        if self.ats_score >= READY:
            return "Easy for ATS software to read"
        return "Mostly readable" if self.ats_score >= 70 else "ATS software may misread it"

    @property
    def ready(self) -> bool:
        return self.score >= READY and self.ats_score >= READY

    @property
    def improves(self) -> bool:
        return self.after_score > self.score or self.ats_after_score > self.ats_score

    @property
    def fixable(self) -> int:
        """Findings "Fix my resume" fixes for them (a PDF becomes a Word file in
        the standard layout, which fixes some by itself)."""
        return max(0, len(self.findings) - len(self.after)) + max(0, len(self.ats) - len(self.ats_after))

    @property
    def marks(self) -> dict[int, str]:
        """Line index -> the most serious level of the findings about it."""
        out: dict[int, str] = {}
        for f in reversed(self.findings + self.ats):  # sorted most serious first, so it wins
            for i in f.lines:
                out[i] = f.level
        return out

    @property
    def notes(self) -> dict[int, list[str]]:
        """Line index -> titles of the findings about it."""
        out: dict[int, list[str]] = {}
        for f in self.findings + self.ats:
            for i in f.lines:
                out.setdefault(i, []).append(f.title)
        return out


def _kind(filename: str, data: bytes) -> str:
    """'.pdf' or '.docx' by what the file is, not just its name."""
    if not data:
        raise CheckError("That file is empty. Choose your resume as a PDF or Word (.docx) file.")
    if len(data) > MAX_RESUME_BYTES:
        raise CheckError(f"That file is over {MAX_RESUME_BYTES // 1_000_000} MB. Resumes are much smaller: "
                         "save it again without large images.")
    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                unzipped = sum(info.file_size for info in archive.infolist())
        except zipfile.BadZipFile:
            raise CheckError("That Word file is damaged. Open it in Word, save it again and retry.") from None
        if "word/document.xml" not in names:
            raise CheckError("That isn't a Word document. Choose your resume as a PDF or Word (.docx) file.")
        if unzipped > _MAX_UNZIPPED:
            raise CheckError("That Word file is too large to check. Save it again without large images.")
        return ".docx"
    if Path(filename or "").suffix.lower() == ".doc" or data[:4] == b"\xd0\xcf\x11\xe0":
        raise CheckError("That's an old Word (.doc) file. In Word, choose File > Save As > Word Document (.docx), "
                         "or save it as a PDF, and check that.")
    raise CheckError("Choose your resume as a PDF or Word (.docx) file.")


def check(filename: str, data: bytes) -> PublicReview:
    suffix = _kind(filename, data)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"resume{suffix}"
        path.write_bytes(data)
        try:
            if suffix == ".pdf":
                import pdfplumber

                with pdfplumber.open(str(path)) as pdf:
                    if len(pdf.pages) > MAX_PAGES:
                        raise CheckError(f"That PDF has {len(pdf.pages)} pages. A resume for Canada should be one "
                                         "or two; check your resume rather than a longer document.")
            doc = canadian.read(path)
        except CheckError:
            raise
        except Exception as exc:
            logger.info("Public resume check couldn't read a %s file (%s)", suffix, type(exc).__name__)
            raise CheckError("Couldn't read that file. If it's a PDF, make sure it isn't password-protected or a "
                             "scan (a photo of a page has no text to check).") from None
        if sum(len(line.text.split()) for line in doc.lines) < canadian.MIN_RESUME_WORDS:
            raise CheckError("That file has almost no text to check. If it's a scanned PDF, upload the Word "
                             "version or a PDF saved from Word or Google Docs.")
        if not canadian.evidence(doc).is_resume:
            raise CheckError(canadian.NOT_A_RESUME)
        try:
            ats_findings = ats.check(path, doc, len(data))
        except Exception as exc:  # the Canadian check still stands without it
            logger.info("Public resume check couldn't run the ATS check (%s)", type(exc).__name__)
            ats_findings = []
    review = PublicReview(canadian.check(doc), doc.is_docx, doc.pages, _display_name(filename, suffix),
                          ats=ats_findings, lines=doc.lines, photos=doc.photos)
    _preview(review, doc)
    return review


def _preview(review: PublicReview, doc: canadian.ResumeDocument) -> None:
    """The resume with every one-click fix made, and what the same checks find in it."""
    plan = canadian.plan(doc, set(canadian.FIXES))
    kept: list[canadian.Line] = []
    for i, line in enumerate(doc.lines):
        text = plan.edits.get(i, line.text)
        if text is None:
            review.removed += 1
            continue
        kept.append(canadian.Line(text, line.heading, line.section))
        review.preview.append(PreviewLine(text, line.heading, text != line.text))
    fixed = canadian.ResumeDocument(kept, 0 if plan.photos else doc.photos, doc.pages, doc.is_docx)
    review.after = canadian.check(fixed)
    # A PDF becomes a plain Word file in the standard layout; a Word file has its layout fixed inside it.
    review.ats_after = [f for f in review.ats if f.key not in (ats.FIXES if doc.is_docx else ats.LAYOUT_FIXES)]


def _display_name(filename: str, suffix: str) -> str:
    stem = Path(filename or "resume").stem.strip()[:60] or "resume"
    return stem + suffix


# ---- holding the file for "Fix my resume" -----------------------------------------------------

_held: dict[str, tuple[float, str, bytes]] = {}
_lock = threading.Lock()


def _expire(now: float) -> None:
    for token in [t for t, (at, _, _) in _held.items() if now - at > HOLD_SECONDS]:
        del _held[token]
    while _held and (len(_held) > _MAX_HELD or sum(len(d) for _, _, d in _held.values()) > _MAX_HELD_BYTES):
        del _held[min(_held, key=lambda t: _held[t][0])]  # oldest first


def hold(filename: str, data: bytes, replaces: str | None = None) -> str:
    """Keep the checked file for HOLD_SECONDS; returns its token for the cookie."""
    token = secrets.token_urlsafe(32)
    with _lock:
        if replaces:
            _held.pop(replaces, None)
        _held[token] = (time.monotonic(), filename, data)
        _expire(time.monotonic())
    return token


def seconds_left(token: str | None) -> int:
    """How much longer the file under `token` is held (0: it isn't)."""
    if not token:
        return 0
    with _lock:
        now = time.monotonic()
        _expire(now)
        entry = _held.get(token)
    return max(0, int(HOLD_SECONDS - (now - entry[0]))) if entry else 0


def held_name(token: str | None) -> str | None:
    """The held file's name, if the token still names one."""
    if not token:
        return None
    with _lock:
        _expire(time.monotonic())
        entry = _held.get(token)
    return entry[1] if entry else None


def held(token: str | None) -> tuple[str, bytes] | None:
    """The held file, left in memory."""
    if not token:
        return None
    with _lock:
        _expire(time.monotonic())
        entry = _held.get(token)
    return (entry[1], entry[2]) if entry else None


def take(token: str | None) -> tuple[str, bytes] | None:
    """The held file, removed from memory."""
    if not token:
        return None
    with _lock:
        _expire(time.monotonic())
        entry = _held.pop(token, None)
    return (entry[1], entry[2]) if entry else None


def clear() -> None:
    with _lock:
        _held.clear()
