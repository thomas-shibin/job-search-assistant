from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import Request

TEST_USER_ID = 1


def pytest_configure(config):
    config.addinivalue_line("markers", "real_auth: use real sign-in instead of the default signed-in test member")
    config.addinivalue_line("markers", "real_limits: enforce the hourly search / resume-action limits and the free plan")
    config.addinivalue_line("markers", "rbac_strict: users get only the roles the app itself assigns")


@pytest.fixture(autouse=True)
def _isolated_resume_uploads(tmp_path, monkeypatch):
    """Resume uploads (Quick Tailor, "Upload resume") go to a temp folder, never
    into the real project's data/resumes/uploads."""
    import app.modules.resumes.uploads as uploads

    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")


@pytest.fixture(autouse=True)
def _isolated_free_plan_key(tmp_path, monkeypatch):
    """The free plan's key file is made in a temp folder, never in the real config/."""
    from app.modules.usage import free_plan

    monkeypatch.setattr(free_plan, "KEY_FILE", tmp_path / "free_plan.key")
    monkeypatch.setattr(free_plan, "_cached_key", None)


@pytest.fixture(autouse=True)
def _single_test_tenant(monkeypatch):
    """Fixtures written for one user: rows they create without an owner belong
    to user 1, the member every web request is signed in as (see below)."""
    import app.core.tenancy as tenancy

    monkeypatch.setattr(tenancy, "unscoped_owner_id", TEST_USER_ID)


@pytest.fixture(autouse=True)
def _no_real_job_board(monkeypatch):
    """Never call the real job-board API from tests (its keys would otherwise
    be read from .env); tests that need it stub the search."""
    monkeypatch.setenv("ADZUNA_APP_ID", "")
    monkeypatch.setenv("ADZUNA_APP_KEY", "")
    monkeypatch.setenv("JOOBLE_API_KEY", "")  # nor the second board
    # Backups: never the real bucket, key or schedule.
    for name in ("BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY", "BACKUP_S3_ENDPOINT",
                 "BACKUP_ENCRYPTION_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("BACKUP_ENABLED", "false")
    # ...nor the analysis provider for search (smart search falls back to its rules).
    monkeypatch.setenv("OPENAI_API_KEY", "")


@pytest.fixture(autouse=True)
def _rate_limits_reset():
    from app.modules.auth.ratelimit import reset_all

    reset_all()
    from app.modules.auth.api import code_limiter
    from app.modules.jobs import postings

    from app.modules.jobs import interpret

    from app.modules.jobs import service as job_search

    code_limiter.reset()
    postings.clear()
    interpret.clear_cache()
    job_search.reset_board_pause()
    yield


@pytest.fixture(autouse=True)
def _no_hourly_limits(request, monkeypatch):
    """Tests run searches and analyses freely (no hourly or free-plan limits) --
    except those marked `real_limits`, which test the limits themselves."""
    if request.node.get_closest_marker("real_limits"):
        return
    from app.modules.usage import quota

    monkeypatch.setattr(quota, "limit_for", lambda user, kind, platform: None)
    monkeypatch.setattr(quota.free_plan, "limit_for", lambda user, default, kind="ai": None)


# Test users are written with the legacy `role` column ("member" / "admin"); give
# them the RBAC roles the migration would have (app/modules/access/rbac.py). Tests of role
# assignment itself mark themselves `rbac_strict` to see exactly what the app does.
LEGACY_ROLES = {"admin": ["SUPER_ADMIN", "JOB_SEEKER"], "member": ["JOB_SEEKER"]}
_legacy_bridge = {"on": True}


def _grant_legacy_roles(mapper, connection, target) -> None:
    if not _legacy_bridge["on"]:
        return
    from sqlalchemy import insert, select

    from app.modules.access.models import RbacRole, UserRole

    for name in LEGACY_ROLES.get(target.role, []):
        role_id = connection.execute(select(RbacRole.id).where(RbacRole.name == name)).scalar_one_or_none()
        if role_id is not None:
            connection.execute(insert(UserRole).values(user_id=target.id, role_id=role_id))


@pytest.fixture(autouse=True, scope="session")
def _legacy_role_bridge():
    from sqlalchemy import event

    from app.modules.auth.models import User

    event.listen(User, "after_insert", _grant_legacy_roles)
    yield
    event.remove(User, "after_insert", _grant_legacy_roles)


@pytest.fixture(autouse=True)
def _rbac_strict(request):
    _legacy_bridge["on"] = not request.node.get_closest_marker("rbac_strict")
    yield
    _legacy_bridge["on"] = True


def make_member(user_id: int = TEST_USER_ID, *, role: str = "member", email: str | None = None):
    from app.modules.access import rbac
    from app.modules.auth.models import User

    user = User(id=user_id, email=email or f"user{user_id}@example.com", first_name="Test", last_name=f"User{user_id}",
                role=role, is_active=True, email_verified_at=datetime(2026, 1, 1), failed_logins=0)
    return rbac.preset(user, LEGACY_ROLES.get(role, []))


@pytest.fixture(autouse=True)
def _signed_in_member(request):
    """Web requests are signed in as test member 1 -- unless the test is marked
    `real_auth` (sign-in tests) or swaps the user itself."""
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    import main
    from app.modules.auth.deps import current_user

    member = make_member()

    def _member(request: Request):
        request.state.user = member  # as the real dependency does, for the page chrome
        return member

    main.app.dependency_overrides[current_user] = _member
    yield member
    main.app.dependency_overrides.pop(current_user, None)

