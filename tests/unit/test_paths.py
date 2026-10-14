"""Where data lives on disk must not depend on which package a module sits in
(a regression guard from the modular-monolith move: the default database was
briefly resolved inside app/)."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_project_root_is_the_repository():
    from app.core.paths import DATA_DIR, DEFAULT_CONFIG_PATH, PROJECT_ROOT

    assert PROJECT_ROOT == REPO and (REPO / "main.py").exists()
    assert DATA_DIR == REPO / "data"
    assert DEFAULT_CONFIG_PATH == REPO / "config" / "config.yaml"


def test_default_database_is_data_job_assistant_db(monkeypatch):
    from app.core.database import session

    assert session._default_sqlite_url() == f"sqlite:///{(REPO / 'data' / 'job_assistant.db').as_posix()}"


def test_data_folders_resolve_under_the_repository(monkeypatch):
    from app.modules.applications.folder import _applications_root
    from app.workers.lock import _lock_path

    monkeypatch.delenv("APPLICATIONS_DATA_DIR", raising=False)
    monkeypatch.delenv("SCHEDULER_DATA_DIR", raising=False)
    assert _applications_root() == REPO / "data" / "applications"
    assert _lock_path().parent == REPO / "data"
