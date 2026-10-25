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


# Countries a provider names by ISO code, spelled as the profile page does.
_COUNTRIES = {"CA": "Canada", "US": "United States", "GB": "United Kingdom", "IN": "India", "AU": "Australia",
              "IE": "Ireland", "NZ": "New Zealand", "PH": "Philippines", "NG": "Nigeria", "PK": "Pakistan",
              "CN": "China", "FR": "France", "DE": "Germany", "MX": "Mexico", "BR": "Brazil", "AE": "United Arab Emirates"}


def fill_from_provider(session: Session, user: User, provider_profile) -> None:
    """What a sign-in provider shared beyond name and email (LinkedIn's country)
    goes into the profile -- only where it's still empty, never over the member's own."""
    country = _COUNTRIES.get((getattr(provider_profile, "country", None) or "").upper())
    if not country:
        return
    with tenant_session(sessionmaker(bind=session.get_bind()), user.id) as own:
        profile = get_profile(own)
        if not profile.country:
            profile.country = country
            own.commit()


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


# ---- LinkedIn ------------------------------------------------------------------------

@dataclass
class LinkedInImport:
    roles: int = 0
    schools: int = 0
    skills: int = 0
    fields: list[str] = field(default_factory=list)  # e.g. ["headline", "city"]

    @property
    def anything(self) -> bool:
        return bool(self.roles or self.schools or self.skills or self.fields)

    def summary(self) -> str:
        parts = [f"{n} {word}{'' if n == 1 else 's'}" for n, word in
                 ((self.roles, "job"), (self.schools, "school"), (self.skills, "skill")) if n]
        if self.fields:
            parts.append(", ".join(self.fields))
        return "Imported from LinkedIn: " + "; ".join(parts) + ". Review them below."


def _place(location: str | None) -> tuple[str | None, str | None, str | None]:
    """'Vancouver, British Columbia, Canada' -> (city, province, country);
    'Toronto, Canada' -> (city, None, country); 'Canada' -> (None, None, country).
    LinkedIn's 'Greater Vancouver Metropolitan Area' style names stay the city."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    countries = {c.lower() for c in _COUNTRIES.values()}
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return (parts[0], None, parts[1]) if parts[1].lower() in countries else (parts[0], parts[1], None)
    if len(parts) == 1:
        return (None, None, parts[0]) if parts[0].lower() in countries else (parts[0], None, None)
    return None, None, None


def import_linkedin(session: Session, data) -> LinkedInImport:
    """Add a LinkedIn profile (app/modules/profiles/linkedin.py) to the member's
    own: empty fields are filled, skills merged, and jobs and schools the
    profile doesn't list yet are added. Nothing the member wrote is replaced,
    so importing the same file twice changes nothing."""
    result = LinkedInImport()
    profile = get_profile(session)
    current = next((p for p in data.positions if p.is_current), None) or max(
        data.positions, key=lambda p: p.start or date.min, default=None)
    for name, value, limit in (("headline", data.headline, 200),
                               ("current_title", current.title if current else None, 200)):
        if value and not getattr(profile, name):
            setattr(profile, name, value[:limit])
            result.fields.append(name.replace("_", " "))
    starts = [p.start for p in data.positions if p.start and p.start <= date.today()]
    if profile.years_experience is None and starts:
        profile.years_experience = max(0, min(60, (date.today() - min(starts)).days // 365))
        result.fields.append("years of experience")
    city, province, country = _place(data.location)
    for name, value in (("city", city), ("province", province), ("country", country)):
        if value and not getattr(profile, name):
            setattr(profile, name, value[:120])
            result.fields.append(name)
    skills = clean_list(list(profile.skills or []) + list(data.skills))
    result.skills = len(skills) - len(profile.skills or [])
    profile.skills = skills

    def same(a: str | None, b: str | None) -> bool:
        return " ".join((a or "").lower().split()) == " ".join((b or "").lower().split())

    have = session.query(ProfileExperience).all()
    for p in data.positions:
        if any(same(e.job_title, p.title) and same(e.company, p.company)
               and (e.start_date == p.start or not e.start_date or not p.start) for e in have):
            continue
        entry = ProfileExperience(job_title=p.title[:200], company=p.company[:200], location=p.location,
                                  start_date=p.start, end_date=None if p.is_current else p.end,
                                  is_current=p.is_current, description=p.description)
        session.add(entry)
        have.append(entry)
        result.roles += 1
    schools = session.query(ProfileEducation).all()
    for s in data.schools:
        if any(same(e.institution, s.institution) and (same(e.qualification, s.degree) or not s.degree)
               for e in schools):
            continue
        entry = ProfileEducation(institution=s.institution[:200], qualification=s.degree,
                                 field_of_study=s.field_of_study, start_date=s.start, end_date=s.end)
        session.add(entry)
        schools.append(entry)
        result.schools += 1
    session.commit()
    return result
