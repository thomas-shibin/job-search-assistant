"""The job-seeker profile: reading and saving it, how complete it is, and
when to offer the (optional) "build your profile" step after signing in."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from sqlalchemy.orm import Session, sessionmaker

from app.core.tenancy import tenant_session
from app.modules.auth.models import User
from app.modules.profiles.models import JobSeekerProfile, ProfileEducation, ProfileExperience
from app.modules.resumes.models import CandidateProfile

PROMPT_BELOW_PERCENT = 60

# (key, weight, label, anchor on the profile page)
_SECTIONS = (
    ("name", 10, "Your name", "personal"),
    ("phone", 10, "Add a phone number", "personal"),
    ("location", 10, "Add where you're based", "personal"),
    ("headline", 10, "Write a professional headline", "professional"),
    ("current", 10, "Add your current title and experience", "professional"),
    ("skills", 10, "List at least 3 skills", "professional"),
    ("preferences", 10, "Tell us the jobs and places you want", "preferences"),
    ("experience", 15, "Add your work experience", "experience"),
    ("education", 5, "Add your education", "education"),
    ("resume", 10, "Upload your resume", "resume"),
)


@dataclass
class Completion:
    percent: int
    missing: list[tuple[str, str]] = field(default_factory=list)  # (suggestion, anchor)

    @property
    def complete(self) -> bool:
        return self.percent >= 100


def get_profile(session: Session) -> JobSeekerProfile:
    """The signed-in user's profile (session bound to them), created empty on first use."""
    profile = session.query(JobSeekerProfile).first()
    if profile is None:
        profile = JobSeekerProfile(skills=[], preferred_titles=[], preferred_locations=[], employment_types=[],
                                   workplaces=[])
        session.add(profile)
        session.commit()
    return profile


def completion(session: Session, user: User, profile: JobSeekerProfile | None = None) -> Completion:
    profile = profile or get_profile(session)
    done = {
        "name": bool(user.first_name and user.last_name),
        "phone": bool(user.phone),
        "location": bool(profile.city and profile.country),
        "headline": bool(profile.headline),
        "current": bool(profile.current_title and profile.years_experience is not None),
        "skills": len(profile.skills or []) >= 3,
        "preferences": bool(profile.preferred_titles or profile.preferred_locations),
        "experience": session.query(ProfileExperience.id).first() is not None,
        "education": session.query(ProfileEducation.id).first() is not None,
        "resume": session.query(CandidateProfile.id).filter(CandidateProfile.is_active.is_(True)).first() is not None,
    }
    percent = sum(weight for key, weight, _, _ in _SECTIONS if done[key])
    return Completion(percent=percent, missing=[(label, anchor) for key, _, label, anchor in _SECTIONS
                                                if not done[key]])


def after_sign_in(session: Session, user: User, next_path: str) -> str:
    """Where to send someone who just signed in: the optional profile step,
    the first time while their profile is mostly empty -- else where they were going."""
    with tenant_session(sessionmaker(bind=session.get_bind()), user.id) as own:
        profile = get_profile(own)
        if profile.prompt_dismissed_at is None and completion(own, user, profile).percent < PROMPT_BELOW_PERCENT:
            return "/welcome?" + urlencode({"next": next_path})
    return next_path


def dismiss_prompt(session: Session) -> None:
    profile = get_profile(session)
    profile.prompt_dismissed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.commit()


# ---- editing ---------------------------------------------------------------------

def clean_list(values, *, limit: int = 30, length: int = 80) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    out = []
    for value in values or []:
        value = " ".join(str(value).split())[:length]
        if value and value.lower() not in {v.lower() for v in out}:
            out.append(value)
    return out[:limit]


def parse_month(value: str | None) -> date | None:
    """'2024-03' (a month input) or '2024-03-15' -> a date; blank or bad -> None."""
    value = (value or "").strip()
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def import_from_resume(session: Session) -> int:
    """Copy roles and education from the active resume into an empty profile.
    Returns how many entries were added (0 when the profile already has some)."""
    if session.query(ProfileExperience.id).first() or session.query(ProfileEducation.id).first():
        return 0
    resume = session.query(CandidateProfile).filter(CandidateProfile.is_active.is_(True)).order_by(
        CandidateProfile.version.desc()).first()
    if resume is None:
        return 0
    added = 0
    for role in resume.employments:
        session.add(ProfileExperience(
            job_title=(role.title or "Role")[:200], company=(role.company or "Company")[:200],
            location=role.location, start_date=role.start_date, end_date=role.end_date, is_current=role.is_current,
            description="\n".join(f"• {b.text}" for b in role.bullets if b.text) or None))
        added += 1
    for school in resume.education:
        session.add(ProfileEducation(
            institution=(school.institution or "School")[:200], qualification=school.degree,
            field_of_study=school.field_of_study, start_date=school.start_date, end_date=school.end_date))
        added += 1
    session.commit()
    return added
