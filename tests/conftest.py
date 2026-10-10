from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_resume_uploads(tmp_path, monkeypatch):
    """Resume uploads (Quick Tailor, "Upload resume") go to a temp folder, never
    into the real project's data/resumes/uploads."""
    import app.ui.routes as ui_routes

    monkeypatch.setattr(ui_routes, "UPLOAD_DIR", tmp_path / "uploads")
