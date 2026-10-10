"""Where each job's generated artifacts live on disk (spec: `data/applications/...`,
Milestone 8's file organization). Kept as its own small module since both resume
generation (Milestone 6/7) and artifact-bundle bookkeeping (Milestone 8) need the same
stable folder path for a given job.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.models.job import Job
from app.utils.filenames import sanitize_filename

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _applications_root() -> Path:
    """Overridable via APPLICATIONS_DATA_DIR (same pattern as DATABASE_URL in
    app/database/session.py) so tests don't write into the real project's
    data/applications folder.
    """
    override = os.environ.get("APPLICATIONS_DATA_DIR")
    return Path(override) if override else PROJECT_ROOT / "data" / "applications"


def application_folder_path(job: Job) -> Path:
    """Where `job`'s generated artifacts live (not created)."""
    return _applications_root() / f"{sanitize_filename(job.company)}_{sanitize_filename(job.title)}_{job.id}"


def get_application_folder(job: Job) -> Path:
    """Returns (creating if needed) the folder for `job`'s generated artifacts.
    Stable across calls for the same job id -- the folder name embeds `job.id`
    specifically so it never collides even if company/title are edited or reused.
    """
    folder = application_folder_path(job)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
