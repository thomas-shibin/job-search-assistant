"""Where each job's generated artifacts live on disk (spec: `data/applications/...`,
Milestone 8's file organization). Kept as its own small module since both resume
generation (Milestone 6/7) and artifact-bundle bookkeeping (Milestone 8) need the same
stable folder path for a given job.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.modules.jobs.models import Job
from app.shared.filenames import sanitize_filename

from app.core.paths import PROJECT_ROOT


def _applications_root() -> Path:
    """Overridable via APPLICATIONS_DATA_DIR (same pattern as DATABASE_URL in
    app/database/session.py) so tests don't write into the real project's
    data/applications folder.
    """
    override = os.environ.get("APPLICATIONS_DATA_DIR")
    return Path(override) if override else PROJECT_ROOT / "data" / "applications"


def user_applications_root(user_id: int | None) -> Path:
    """One folder per user, so no two users' files ever share a directory."""
    root = _applications_root()
    return root / f"user_{user_id}" if user_id is not None else root


def application_folder_path(job: Job) -> Path:
    """Where `job`'s generated artifacts live (not created)."""
    return user_applications_root(job.user_id) / f"{sanitize_filename(job.company)}_{sanitize_filename(job.title)}_{job.id}"


def get_application_folder(job: Job) -> Path:
    """Returns (creating if needed) the folder for `job`'s generated artifacts.
    Stable across calls for the same job id -- the folder name embeds `job.id`
    specifically so it never collides even if company/title are edited or reused.
    """
    folder = application_folder_path(job)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
