"""End-to-end test of resume tailoring (Milestone 6) against a real sqlite
session: AI draft -> truthfulness validation -> persisted TailoredResume +
TailoredBulletProvenance rows -> rendered DOCX -> Job status transition.
Uses MockAIProvider throughout so this stays free/deterministic/offline.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config.schema import AddressProfile, AppConfig, CandidateConfig, SearchProfile
from app.core.database.base import Base
from app.integrations.ai.base import TailoredBulletDraft, TailoredResumeDraft
from app.integrations.ai.mock import MockAIProvider
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.jobs.models import Job, JobStatus
from app.modules.resumes.models import AccomplishmentBullet, CandidateProfile, Employment, Skill
from app.modules.resumes.tailor import NoActiveCandidateProfileError, tailor_resume
from app.modules.resumes.tailoring_models import TailoredBulletProvenance


@pytest.fixture()
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setenv("APPLICATIONS_DATA_DIR", str(tmp_path / "applications"))
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_candidate(session: Session) -> CandidateProfile:
    profile = CandidateProfile(raw_resume_path="x.pdf", source_hash="h", summary="Backend engineer.", is_active=True)
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


def test_address_follows_search_profile_then_the_jobs_province(session):
    _seed_candidate(session)
    config = _config(
        address_profiles={
            "bc": AddressProfile(city="Victoria", province="BC", country="Canada"),
            "ontario": AddressProfile(city="Toronto", province="ON", country="Canada"),
        },
        search_profiles=[SearchProfile(name="BC Backend", address_profile="bc", locations=["Vancouver, BC"])],
    )

    # Found by a search profile: that profile's address, wherever the job is.
    job = _seed_job(session, search_profile_name="BC Backend", location="Toronto, Ontario",
                    fingerprint="fp1", description_hash="dh1")
    assert "Victoria, BC, Canada" in _docx_text(tailor_resume(session, job, config, ai_provider=MockAIProvider()).docx_path)

    # Found by the global search: the address in the job's own province.
    job = _seed_job(session, location="Toronto, Ontario", fingerprint="fp2", description_hash="dh2")
    assert "Toronto, ON, Canada" in _docx_text(tailor_resume(session, job, config, ai_provider=MockAIProvider()).docx_path)

    # A province with no address profile (or a remote job) keeps the resume's own.
    for i, location in enumerate(("Calgary, AB", "Remote")):
        job = _seed_job(session, location=location, fingerprint=f"fp3{i}", description_hash=f"dh3{i}")
        text = _docx_text(tailor_resume(session, job, config, ai_provider=MockAIProvider()).docx_path)
        assert "Toronto, ON" not in text and "Victoria, BC, Canada" not in text


def test_job_province_from_the_location_catalog(session):
    from app.modules.jobs.catalog.geo import province_of
    from app.modules.jobs.catalog.store import Term, upsert_terms
    from app.modules.jobs.catalog.models import TermKind, TermSource

    upsert_terms(session, TermKind.LOCATION, [
        Term("Mississauga, ON", geo_path=["Canada", "Ontario", "Peel region", "Mississauga"]),
        Term("Richmond, BC", geo_path=["Canada", "British Columbia", "Greater Vancouver", "Richmond"]),
        Term("Richmond, QC", geo_path=["Canada", "Québec", "Estrie", "Richmond"]),
    ], TermSource.SCAN)

    assert province_of(session, "Mississauga, Peel region") == "ON"
    assert province_of(session, "Toronto, Ontario") == "ON"
    assert province_of(session, "Montréal, Québec") == "QC"
    assert province_of(session, "Richmond") is None  # in two provinces: can't tell
    assert province_of(session, "Remote") is None


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
    profile = CandidateProfile(raw_resume_path="x.pdf", source_hash="h", is_active=True)
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


def test_location_stated_in_the_description_beats_the_listing(session):
    from app.modules.jobs.catalog.geo import stated_province
    from app.modules.jobs.catalog.store import Term, upsert_terms
    from app.modules.jobs.catalog.models import TermKind, TermSource

    upsert_terms(session, TermKind.LOCATION, [
        Term("Vancouver, BC", geo_path=["Canada", "British Columbia", "Greater Vancouver", "Vancouver"]),
    ], TermSource.SCAN)

    assert stated_province(session, "Role: Java Location: Vancouver folks its Hybrid, One day office") == "BC"
    assert stated_province(session, "Work location - Calgary, Alberta") == "AB"
    assert stated_province(session, "Location: Remote") is None

    _seed_candidate(session)
    config = _config(address_profiles={
        "bc": AddressProfile(city="Victoria", province="BC", country="Canada"),
        "on": AddressProfile(city="Toronto", province="ON", country="Canada"),
    })
    job = _seed_job(session, location="Mississauga, Ontario", description="Java role. Location: Vancouver (hybrid).")
    text = _docx_text(tailor_resume(session, job, config, ai_provider=MockAIProvider()).docx_path)
    assert "Victoria, BC, Canada" in text and "Toronto" not in text


def test_address_rules_for_one_profile_and_no_profiles(session):
    from app.modules.resumes.tailor import resolve_address

    home = AddressProfile(street="1234 Main St", unit="Unit 5", city="Victoria", province="BC",
                          postal_code="V8W 1A1", country="Canada")
    assert home.one_line() == "1234 Main St, Unit 5, Victoria, BC V8W 1A1, Canada"

    ontario_job = _seed_job(session, location="Toronto, Ontario", fingerprint="o", description_hash="o")
    remote_job = _seed_job(session, location="Remote", fingerprint="r", description_hash="r")

    # Exactly one profile: always that address, wherever (or whether) the job is.
    one = _config(address_profiles={"home": home})
    assert resolve_address(one, ontario_job, session) == home
    assert resolve_address(one, remote_job, session) == home

    # No profiles: the job's own city and province, when it has one.
    none = _config(address_profiles={})
    picked = resolve_address(none, ontario_job, session)
    assert (picked.city, picked.province, picked.country) == ("Toronto", "ON", "Canada")
    assert resolve_address(none, remote_job, session) is None

    # Turned off: nothing changes.
    off = _config(address_profiles={"home": home})
    off.resume.change_address = False
    assert resolve_address(off, ontario_job, session) is None


def test_full_street_address_replaces_the_resume_location_line(tmp_path):
    from docx import Document

    from app.modules.resumes.documents.docx_edit import tailor_docx

    for original in ("Victoria, Canada", "99 Old Rd, Victoria, BC V8W 2B2"):
        base, out = tmp_path / "base.docx", tmp_path / "out.docx"
        doc = Document()
        doc.add_paragraph("Jordan Casey")
        doc.add_paragraph(original)
        doc.add_paragraph("jordan@example.com | 555-123-4567")
        doc.save(str(base))

        tailor_docx(base, out, [], location_line="1234 Main St, Unit 5, Toronto, ON M5V 1A1, Canada", full_address=True)
        assert [p.text for p in Document(str(out)).paragraphs] == [
            "Jordan Casey", "1234 Main St, Unit 5, Toronto, ON M5V 1A1, Canada", "jordan@example.com | 555-123-4567",
        ]

        # Without a street, a "City, Country" line keeps its shape; a street-address line is fully replaced.
        tailor_docx(base, out, [], location_line="Toronto, ON, Canada")
        expected = "Toronto, Canada" if original == "Victoria, Canada" else "Toronto, ON, Canada"
        assert Document(str(out)).paragraphs[1].text == expected
