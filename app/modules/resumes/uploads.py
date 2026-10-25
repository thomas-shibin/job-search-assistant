"""Storing an uploaded resume file and importing it as the user's active
resume. Each user's files live in their own folder under
data/resumes/uploads/; the database stores the path relative to the project
so the same data works for the native install and Docker (/app) alike."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.config.store import save_section
from app.core.paths import PROJECT_ROOT
from app.core.tenancy import require_tenant
from app.modules.resumes import canadian
from app.modules.resumes.models import CandidateProfile
from app.modules.resumes.parser import ImportResult, import_resume
from app.modules.auth.models import User
from app.modules.usage import free_plan, quota
from app.modules.usage.models import ActivityKind
from app.shared.filenames import sanitize_filename

UPLOAD_DIR = PROJECT_ROOT / "data" / "resumes" / "uploads"
ALLOWED_RESUME_SUFFIXES = {".docx", ".pdf"}
MAX_RESUME_BYTES = 10_000_000


def import_resume_file(session: Session, filename: str, data: bytes, *, force: bool = False,
                       record: bool = True) -> tuple[ImportResult | None, str | None]:
    """Save an uploaded resume and import it as the active candidate profile
    (a no-op if its content matches the current one, unless `force`).

    Args:
        filename: The name the user's file had (only its suffix and stem are used).
        data: The file's bytes; anything over MAX_RESUME_BYTES is refused.
        force: Make a new version even when the text is unchanged (a removed photo).
        record: Log a new version as an upload (False: the caller logs what it did).

    Returns:
        (result, None), or (None, a message for the user) if the file can't be used.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_RESUME_SUFFIXES:
        return None, "Resume must be a .docx or .pdf file."
    if not data:
        return None, "That file is empty."
    if len(data) > MAX_RESUME_BYTES:
        return None, f"That file is over {MAX_RESUME_BYTES // 1_000_000} MB -- resumes are much smaller."
    user_dir = UPLOAD_DIR / f"user_{require_tenant(session)}"  # each user's files in their own folder
    user_dir.mkdir(parents=True, exist_ok=True)
    # Unique even for two uploads in the same second (the second, if identical,
    # is deleted below -- it must never be the file the active resume points at).
    dest_name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{sanitize_filename(Path(filename).stem)}{suffix}"
    dest_path = user_dir / dest_name
    dest_path.write_bytes(data)
    # Relative to the project when possible, so the same database works for the
    # native install and Docker (/app) alike.
    stored_path = dest_path.relative_to(PROJECT_ROOT) if dest_path.is_relative_to(PROJECT_ROOT) else dest_path
    text = ""
    try:  # a health card or a letter isn't a resume: every check would pass it as one
        doc = canadian.read(dest_path)
        text = "\n".join(line.text for line in doc.lines)
        looks_right = canadian.evidence(doc).is_resume
    except Exception:
        looks_right = True  # can't be read here: the import below says so
    if not looks_right:
        dest_path.unlink(missing_ok=True)
        return None, canadian.NOT_A_RESUME
    try:
        result = import_resume(session, stored_path if stored_path.exists() else dest_path, force=force)
    except Exception as exc:  # unreadable/corrupt file
        session.rollback()
        dest_path.unlink(missing_ok=True)
        return None, f"Couldn't read that resume ({type(exc).__name__}). Is it a valid Word or PDF file?"
    if not result.created and Path(result.profile.raw_resume_path) not in (stored_path, dest_path):
        dest_path.unlink(missing_ok=True)  # same content as the current resume: keep nothing new
    else:
        try:  # keep Settings -> Candidate -> base resume pointing at the file in use
            candidate = user_config(session).candidate
            save_section(session, "candidate", {**candidate.model_dump(), "base_resume": str(result.profile.raw_resume_path)})
        except ConfigError:
            pass
    if result.created and record:
        quota.record(session, require_tenant(session), ActivityKind.RESUME_UPLOAD, f"Version {result.profile.version}")
    user = session.get(User, require_tenant(session))
    if user is not None:  # the free plan finds one person's accounts by their resume
        free_plan.note_resume(session, user, text, text_hash=result.profile.source_hash,
                              full_name=result.profile.full_name)
    return result, None


async def import_upload(session: Session, upload: UploadFile) -> tuple[ImportResult | None, str | None]:
    """`import_resume_file` for a file posted to a route (reads at most one byte
    past the size limit, so an oversized upload is refused without reading it all)."""
    if Path(upload.filename or "").suffix.lower() not in ALLOWED_RESUME_SUFFIXES:
        return None, "Resume must be a .docx or .pdf file."
    data = await upload.read(MAX_RESUME_BYTES + 1)
    return import_resume_file(session, upload.filename or "", data)


def resume_file(candidate: CandidateProfile) -> Path | None:
    """The resume's original file on disk, wherever the stored path points."""
    for path in (Path(candidate.raw_resume_path), PROJECT_ROOT / candidate.raw_resume_path):
        if path.is_file():
            return path
    return None
