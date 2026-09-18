"""Tests for the deterministic truthfulness guardrail (Milestone 6, spec §8):
a generated bullet is only trusted if it cites a real source bullet and
doesn't introduce a number that bullet's own text doesn't already contain.
"""
from __future__ import annotations

from app.ai.base import TailoredBulletDraft, TailoredResumeDraft
from app.models.candidate import AccomplishmentBullet, CandidateProfile, Employment, Skill
from app.resume.validator import validate_tailored_resume


def _candidate() -> CandidateProfile:
    profile = CandidateProfile(raw_resume_path="x.docx", source_hash="h", summary="Backend engineer with 5 years experience.")
    profile.employments = [
        Employment(
            id=1,
            company="Acme",
            title="Engineer",
            bullets=[
                AccomplishmentBullet(id=1, text="Reduced latency by 40% via caching", order_index=0),
                AccomplishmentBullet(id=2, text="Mentored a team of 5 engineers", order_index=1),
            ],
        )
    ]
    profile.skills = [Skill(name="Java"), Skill(name="AWS")]
    return profile


def test_bullet_with_verbatim_text_is_accepted():
    draft = TailoredResumeDraft(
        summary="Backend engineer.",
        skills_order=["Java"],
        bullets=[TailoredBulletDraft(text="Reduced latency by 40% via caching", source_bullet_id=1)],
    )
    result = validate_tailored_resume(draft, _candidate())

    assert result.bullets[0].accepted is True
    assert result.bullets[0].text == "Reduced latency by 40% via caching"


def test_bullet_rephrased_without_new_numbers_is_accepted():
    draft = TailoredResumeDraft(
        summary="Backend engineer.",
        skills_order=["Java"],
        bullets=[TailoredBulletDraft(text="Cut request latency 40% by introducing a caching layer", source_bullet_id=1)],
    )
    result = validate_tailored_resume(draft, _candidate())

    assert result.bullets[0].accepted is True


def test_bullet_with_invented_number_falls_back_to_source_text():
    draft = TailoredResumeDraft(
        summary="Backend engineer.",
        skills_order=["Java"],
        bullets=[TailoredBulletDraft(text="Reduced latency by 90% via caching", source_bullet_id=1)],
    )
    result = validate_tailored_resume(draft, _candidate())

    assert result.bullets[0].accepted is False
    assert result.bullets[0].text == "Reduced latency by 40% via caching"
    assert "90" in result.bullets[0].rejection_reason


def test_bullet_with_unknown_source_id_is_dropped():
    draft = TailoredResumeDraft(
        summary="Backend engineer.",
        skills_order=["Java"],
        bullets=[TailoredBulletDraft(text="Invented an achievement", source_bullet_id=999)],
    )
    result = validate_tailored_resume(draft, _candidate())

    assert result.bullets[0].accepted is False
    assert result.bullets[0].text == ""


def test_summary_with_invented_number_is_rejected():
    draft = TailoredResumeDraft(summary="Engineer with 20 years experience.", skills_order=[], bullets=[])
    result = validate_tailored_resume(draft, _candidate())

    assert result.summary_accepted is False


def test_summary_reusing_existing_numbers_is_accepted():
    draft = TailoredResumeDraft(summary="Engineer with 5 years experience, cut latency by 40%.", skills_order=[], bullets=[])
    result = validate_tailored_resume(draft, _candidate())

    assert result.summary_accepted is True


def test_skill_not_on_candidate_profile_is_rejected():
    draft = TailoredResumeDraft(summary="Backend engineer.", skills_order=["Java", "Terraform"], bullets=[])
    result = validate_tailored_resume(draft, _candidate())

    assert result.skills_accepted == ["Java"]
    assert result.skills_rejected == ["Terraform"]


def test_skill_matching_case_insensitively_uses_candidate_profile_casing():
    draft = TailoredResumeDraft(summary="Backend engineer.", skills_order=["java"], bullets=[])
    result = validate_tailored_resume(draft, _candidate())

    assert result.skills_accepted == ["Java"]
