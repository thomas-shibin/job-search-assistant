"""End-to-end test of resume tailoring (Milestone 6) against a real sqlite
session: AI draft -> truthfulness validation -> persisted TailoredResume +
TailoredBulletProvenance rows -> rendered DOCX -> Job status transition.
Uses MockAIProvider throughout so this stays free/deterministic/offline.
"""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.ai.base import TailoredBulletDraft, TailoredResumeDraft
from app.ai.mock import MockAIProvider
from app.config.schema import AddressProfile, AppConfig, CandidateConfig, SearchProfile
from app.database.base import Base
from app.models.candidate import AccomplishmentBullet, CandidateProfile, Employment, Skill
from app.models.job import Job, JobStatus
from app.models.resume import TailoredBulletProvenance
from app.resume.tailor import NoActiveCandidateProfileError, tailor_resume


@pytest.fixture()
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_candidate(session: Session) -> CandidateProfile:
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", summary="Backend engineer.", is_active=True)
    profile.skills = [Skill(name=n) for n in ["Java", "AWS"]]
    profile.employments = [
        Employment(
            company="Prior Co", title="Senior Backend Engineer", start_date=date(2019, 1, 1), is_current=True,
            bullets=[AccomplishmentBullet(text="Reduced latency by 40% via caching", order_index=0)],
        )
    ]
    session.add(profile)
    session.commit()
    return profile


def _seed_job(session: Session, **overrides) -> Job:
    defaults = dict(
        source="manual", source_url="https://example.com/job", company="Example Corp",
        title="Senior Backend Engineer", location="Vancouver, BC",
        description="Senior Backend Engineer with Java and AWS experience.",
        status=JobStatus.QUALIFIED, fingerprint="fp", description_hash="dh",
    )
    defaults.update(overrides)
    job = Job(**defaults)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _config(**overrides) -> AppConfig:
    defaults = dict(candidate=CandidateConfig(name="Jordan Casey", base_resume="x.docx"))
    defaults.update(overrides)
    return AppConfig(**defaults)


def test_tailor_resume_persists_and_renders_with_mock_provider(session):
    _seed_candidate(session)
    job = _seed_job(session)

    result = tailor_resume(session, job, _config(), ai_provider=MockAIProvider())

    assert result.tailored_resume.validation_status == "PASSED"
    assert result.tailored_resume.docx_path is not None
    assert result.docx_path.exists()
    assert job.status == JobStatus.RESUME_GENERATED

    provenance = session.query(TailoredBulletProvenance).filter_by(
        tailored_resume_id=result.tailored_resume.id
    ).all()
    assert len(provenance) == 1
    assert provenance[0].validation_passed is True
    assert provenance[0].source_bullet_id is not None


def test_tailor_resume_falls_back_to_source_text_on_invented_number(session):
    _seed_candidate(session)
    job = _seed_job(session)
    source_bullet_id = session.query(AccomplishmentBullet).one().id

    class FabricatingProvider:
        last_usage = None

        def tailor_resume(self, candidate, job, requirements, address):
            return TailoredResumeDraft(
                summary="Backend engineer.",
                skills_order=["Java"],
                bullets=[TailoredBulletDraft(text="Reduced latency by 99% via caching", source_bullet_id=source_bullet_id)],
            )

    result = tailor_resume(session, job, _config(), ai_provider=FabricatingProvider())

    assert result.tailored_resume.validation_status == "PARTIAL"
    provenance = session.query(TailoredBulletProvenance).filter_by(
        tailored_resume_id=result.tailored_resume.id
    ).one()
    assert provenance.validation_passed is False
    assert provenance.generated_text == "Reduced latency by 40% via caching"  # fell back verbatim

    docx_text = _docx_text(result.docx_path)
    assert "Reduced latency by 40% via caching" in docx_text
    assert "99%" not in docx_text


def test_tailor_resume_without_active_candidate_raises(session):
    job = _seed_job(session)
    with pytest.raises(NoActiveCandidateProfileError):
        tailor_resume(session, job, _config(), ai_provider=MockAIProvider())


def test_address_swapped_only_for_job_with_matching_search_profile(session):
    _seed_candidate(session)
    config = _config(
        address_profiles={"bc": AddressProfile(city="Victoria", province="BC", country="Canada")},
        search_profiles=[SearchProfile(name="BC Backend", address_profile="bc", locations=["Vancouver, BC"])],
    )

    job_with_profile = _seed_job(session, search_profile_name="BC Backend", fingerprint="fp1", description_hash="dh1")
    result = tailor_resume(session, job_with_profile, config, ai_provider=MockAIProvider())
    assert "Victoria, BC, Canada" in _docx_text(result.docx_path)

    job_without_profile = _seed_job(session, fingerprint="fp2", description_hash="dh2")
    result2 = tailor_resume(session, job_without_profile, config, ai_provider=MockAIProvider())
    assert "Victoria, BC, Canada" not in _docx_text(result2.docx_path)


def test_parsed_name_wins_over_config_name_by_default(session):
    profile = _seed_candidate(session)
    profile.full_name = "Jordan Q. Casey"
    session.commit()
    job = _seed_job(session)

    config = _config(candidate=CandidateConfig(name="Config Placeholder Name", base_resume="x.docx"))
    result = tailor_resume(session, job, config, ai_provider=MockAIProvider())

    text = _docx_text(result.docx_path)
    assert "Jordan Q. Casey" in text
    assert "Config Placeholder Name" not in text


def test_ai_omitting_a_whole_employer_and_skills_is_backfilled_verbatim(session):
    """Regression test: a real observed failure mode is the AI treating
    "tailor" as "shorten" -- dropping an entire employer's bullets and most
    skills as "irrelevant" rather than just reordering/re-emphasizing.
    Nothing true on the candidate's profile should be able to silently
    vanish from the final resume just because the AI chose to omit it.
    """
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", is_active=True)
    profile.skills = [Skill(name="Java"), Skill(name="COBOL")]
    recent_bullet = AccomplishmentBullet(id=1, text="Did recent things", order_index=0)
    old_bullet = AccomplishmentBullet(id=2, text="Did old things", order_index=0)
    profile.employments = [
        Employment(company="New Co", title="Engineer", start_date=date(2023, 1, 1), is_current=True, bullets=[recent_bullet]),
        Employment(company="Old Co", title="Engineer", start_date=date(2015, 1, 1), end_date=date(2020, 1, 1), bullets=[old_bullet]),
    ]
    session.add(profile)
    job = _seed_job(session)
    session.commit()

    class OverlyAggressiveProvider:
        last_usage = None

        def tailor_resume(self, candidate, job, requirements, address):
            # Only returns the recent bullet and one skill, dropping "Old Co" and "COBOL" entirely.
            return TailoredResumeDraft(
                summary="Engineer.",
                skills_order=["Java"],
                bullets=[TailoredBulletDraft(text="Did recent things", source_bullet_id=1)],
            )

    result = tailor_resume(session, job, _config(), ai_provider=OverlyAggressiveProvider())

    provenance = session.query(TailoredBulletProvenance).filter_by(
        tailored_resume_id=result.tailored_resume.id
    ).all()
    covered_ids = {p.source_bullet_id for p in provenance}
    assert covered_ids == {1, 2}  # both bullets present, including the "dropped" one

    assert set(result.tailored_resume.skills_order) == {"Java", "COBOL"}

    docx_text = _docx_text(result.docx_path)
    assert "Did old things" in docx_text
    assert "Old Co" in docx_text


def test_override_parsed_name_forces_config_name_to_win(session):
    profile = _seed_candidate(session)
    profile.full_name = "Jordan Q. Casey"
    session.commit()
    job = _seed_job(session)

    config = _config(
        candidate=CandidateConfig(name="Preferred Name", base_resume="x.docx", override_parsed_name=True)
    )
    result = tailor_resume(session, job, config, ai_provider=MockAIProvider())

    text = _docx_text(result.docx_path)
    assert "Preferred Name" in text
    assert "Jordan Q. Casey" not in text


def _docx_text(path) -> str:
    from docx import Document

    return "\n".join(p.text for p in Document(str(path)).paragraphs)
