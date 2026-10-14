"""Every place a user's files live on disk -- each a per-user folder, so one
user's files never share a directory with another's, and deleting an account
can remove all of them."""
from __future__ import annotations

import shutil
from pathlib import Path


def user_file_dirs(user_id: int) -> list[Path]:
    from app.modules.applications.folder import user_applications_root
    from app.modules.resumes.tailor import RESTORED_DIR
    from app.web import routes

    return [routes.UPLOAD_DIR / f"user_{user_id}", user_applications_root(user_id), RESTORED_DIR / f"user_{user_id}"]


def delete_user_files(user_id: int) -> None:
    for folder in user_file_dirs(user_id):
        shutil.rmtree(folder, ignore_errors=True)
