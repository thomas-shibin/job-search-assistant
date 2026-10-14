"""No user can see or change another user's data -- through any page, action,
download or API, or by going around the routes to the database layer.

Two members, Alice (1) and Bob (2), each with a resume, a job, a match
analysis, a tailored resume (with files on disk), a watch, settings and typed
search terms. Signed in as Alice, every attempt to reach Bob's data fails, and
Bob's data is unchanged afterwards.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.database.base import Base
from app.database.session import get_session
from app.models.candidate import AccomplishmentBullet, CandidateProfile, Employment
from app.models.catalog import UserSearchTerm
from app.models.job import Job, JobStatus
from app.models.matching import MatchAnalysis
from app.models.resume import TailoredBulletProvenance, TailoredResume
from app.models.settings import UserSetting, WatchlistEntry
from app.models.user import User
from app.tenancy import TenantContextError, system_session, tenant_session
from tests.conftest import make_member

ALICE, BOB = 1, 2


def _seed(factory, tmp_path, user_id: int, name: str) -> dict:
    files = tmp_path / f"files_{user_id}"
    files.mkdir()
    (files / "resume.docx").write_bytes(f"{name} tailored docx".encode())
    (files / "resume.pdf").write_bytes(f"{name} tailored pdf".encode())
    (files / "base.docx").write_bytes(f"{name} base resume".encode())
    with tenant_session(factory, user_id) as s:
        bullet = AccomplishmentBullet(text=f"{name} secret achievement", order_index=0)
        s.add(CandidateProfile(raw_resume_path=str(files / "base.docx"), source_hash=f"h{user_id}", is_active=True,
                               summary=f"{name} private summary", full_name=name,
                               employments=[Employment(company=f"{name} Corp", title="Engineer", bullets=[bullet])]))
        job = Job(source="manual", source_url=f"https://e.com/{name}", company=f"{name}Co", title=f"{name} Secret Role",
                  description=f"{name} description", status=JobStatus.READY_FOR_REVIEW, fingerprint=f"fp{user_id}",
                  description_hash=f"dh{user_id}")
        s.add(job)
        s.commit()
        s.add(MatchAnalysis(job_id=job.id, overall_score=88, explanation=f"{name} explanation"))
        tailored = TailoredResume(job_id=job.id, summary_text=f"{name} tailored", skills_order=[],
                                  validation_status="PASSED", docx_path=str(files / "resume.docx"),
                                  pdf_path=str(files / "resume.pdf"))
        s.add(tailored)
        s.commit()
        provenance = TailoredBulletProvenance(tailored_resume_id=tailored.id, generated_text=f"{name} rewrite",
                                              source_bullet_id=bullet.id, source_text=bullet.text,
                                              validation_passed=True)
        s.add(provenance)
        watch = WatchlistEntry(name=f"{name} watch", enabled=True, titles=[f"{name} title"], companies=[],
                               keywords=[], locations=[], exclude_keywords=[], exclude_companies=[], job_types=[])
        s.add(watch)
        s.add(UserSetting(key="search", value={"titles": [f"{name} Default Title"], "locations": [], "keywords": []}))
        s.add(UserSearchTerm(kind="title", label=f"{name} Typed Title", norm=f"{name.lower()} typed title"))
        s.commit()
        return {"job": job.id, "watch": watch.id, "provenance": provenance.id, "tailored": tailored.id}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})
    with system_session(factory) as s:
        for uid, name in ((ALICE, "Alice"), (BOB, "Bob")):
            s.add(User(id=uid, email=f"{name.lower()}@example.com", first_name=name, last_name="X", role="member",
                       email_verified_at=datetime(2026, 1, 1)))
        s.commit()
    ids = {ALICE: _seed(factory, tmp_path, ALICE, "Alice"), BOB: _seed(factory, tmp_path, BOB, "Bob")}

    def override_get_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    import main
    from app.auth.deps import current_user

    alice = make_member(ALICE, email="alice@example.com")
    main.app.dependency_overrides[get_session] = override_get_session
    main.app.dependency_overrides[current_user] = lambda: alice
    yield TestClient(main.app), factory, ids
    main.app.dependency_overrides.clear()


def _bobs_data_untouched(factory, ids) -> None:
    with tenant_session(factory, BOB) as s:
        job = s.get(Job, ids[BOB]["job"])
        assert job is not None and job.status == JobStatus.READY_FOR_REVIEW and job.description == "Bob description"
        watch = s.get(WatchlistEntry, ids[BOB]["watch"])
        assert watch is not None and watch.enabled and watch.name == "Bob watch"
        assert s.get(TailoredBulletProvenance, ids[BOB]["provenance"]).removed is False
        assert s.query(CandidateProfile).one().summary == "Bob private summary"


def test_pages_show_only_your_own_data(world):
    http, _, ids = world
    for path in ("/", "/tracked-jobs", "/resume", "/watchlist", "/settings", f"/jobs/{ids[ALICE]['job']}"):
        text = http.get(path).text
        assert "Bob" not in text, path
    assert "Alice Secret Role" in http.get("/tracked-jobs").text
    assert "Alice watch" in http.get("/watchlist").text
    assert "Alice private summary" in http.get("/resume").text


@pytest.mark.parametrize("path", [
    "/jobs/{job}", "/jobs/{job}/resume.docx", "/jobs/{job}/resume.pdf", "/api/jobs/{job}",
    "/watchlist?edit={watch}",
])
def test_reading_another_users_records_is_not_found(world, path):
    http, _, ids = world
    response = http.get(path.format(**ids[BOB]))
    assert response.status_code == 404
    assert "Bob" not in response.text


@pytest.mark.parametrize("path, data", [
    ("/jobs/{job}/approve", {}), ("/jobs/{job}/skip", {}), ("/jobs/{job}/open-application", {}),
    ("/jobs/{job}/mark-applied", {}), ("/jobs/{job}/archive", {}), ("/jobs/{job}/assess", {}),
    ("/jobs/{job}/analyze", {}), ("/jobs/{job}/retailor", {}),
    ("/jobs/{job}/description", {"description": "Overwritten by Alice " * 20}),
    ("/jobs/{job}/removals", {"remove": "{provenance}"}),
    ("/watchlist/watches/{watch}/toggle", {}), ("/watchlist/watches/{watch}/delete", {}),
    ("/watchlist/watches/{watch}", {"name": "hijacked", "titles": "x", "enabled": "true"}),
])
def test_changing_another_users_records_is_not_found(world, path, data):
    http, factory, ids = world
    data = {k: v.format(**ids[BOB]) for k, v in data.items()}
    response = http.post(path.format(**ids[BOB]), data=data, follow_redirects=False)
    assert response.status_code == 404
    _bobs_data_untouched(factory, ids)


def test_removals_on_your_own_job_ignore_another_users_paragraph_ids(world, monkeypatch):
    http, factory, ids = world
    import app.ui.routes as ui_routes

    monkeypatch.setattr(ui_routes, "rebuild_tailored_resume", lambda *a, **k: None)
    http.post(f"/jobs/{ids[ALICE]['job']}/removals", data={"remove": [str(ids[BOB]["provenance"])]})
    _bobs_data_untouched(factory, ids)


def test_api_and_suggestions_are_per_user(world):
    http, _, ids = world
    jobs = http.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [ids[ALICE]["job"]]
    labels = [t["label"] for t in http.get("/api/terms/title").json()]
    assert "Alice Typed Title" in labels and "Bob Typed Title" not in labels


def test_downloads_serve_only_your_own_files(world):
    http, _, ids = world
    assert http.get(f"/jobs/{ids[ALICE]['job']}/resume.docx").content == b"Alice tailored docx"
    assert http.get("/resume/file").content == b"Alice base resume"


def test_settings_saves_never_touch_another_user(world):
    http, factory, _ = world
    http.post("/settings/matching", data={"matching.minimum_score": "55"})
    with tenant_session(factory, BOB) as s:
        assert s.query(UserSetting).filter(UserSetting.key == "matching").one_or_none() is None
        assert s.query(UserSetting).filter(UserSetting.key == "search").one().value["titles"] == ["Bob Default Title"]


def test_database_layer_refuses_cross_user_access(world):
    _, factory, ids = world
    # No user bound: user data is off limits (fail closed).
    with factory() as s, pytest.raises(TenantContextError):
        s.query(Job).all()
    with tenant_session(factory, ALICE) as s:
        assert s.get(Job, ids[BOB]["job"]) is None
        assert s.query(Job).filter(Job.id == ids[BOB]["job"]).update({Job.title: "pwned"}) == 0
        assert s.query(Job).filter(Job.user_id == BOB).delete() == 0
        s.commit()
        s.add(Job(user_id=BOB, source="manual", source_url="x", company="x", title="planted", description="x",
                  fingerprint="x", description_hash="x"))
        with pytest.raises(TenantContextError):
            s.flush()
        s.rollback()
    with system_session(factory) as s:  # someone else's row can't be changed even if obtained
        bobs_job = s.get(Job, ids[BOB]["job"])
    with tenant_session(factory, ALICE) as s:
        merged = s.merge(bobs_job)
        merged.title = "pwned"
        with pytest.raises(TenantContextError):
            s.flush()
    _bobs_data_untouched(factory, ids)


def test_deleting_an_account_removes_only_that_users_data(world, tmp_path):
    _, factory, ids = world
    from app.auth.service import delete_account

    delete_account(factory, ALICE)
    with system_session(factory) as s:
        assert s.get(User, ALICE) is None and s.get(User, BOB) is not None
        for model in (Job, CandidateProfile, WatchlistEntry, UserSetting, UserSearchTerm, TailoredResume,
                      MatchAnalysis, TailoredBulletProvenance, AccomplishmentBullet, Employment):
            assert s.query(model).filter(model.user_id == ALICE).count() == 0, model.__name__
            assert s.query(model).filter(model.user_id == BOB).count() >= 1, model.__name__
    _bobs_data_untouched(factory, ids)
