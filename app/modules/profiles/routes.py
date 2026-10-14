"""The Profile page (/profile) and its JSON API (/api/profile...). Members
only; every query goes through the member's own tenant-bound session."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.auth.deps import member_session, require_member
from app.modules.auth.models import User
from app.modules.auth.service import AuthError, name_problem, normalize_phone
from app.modules.jobs.postings import EMPLOYMENT_TYPES, WORKPLACES
from app.modules.profiles import service as profiles
from app.modules.profiles.models import ProfileEducation, ProfileExperience
from app.modules.resumes.models import CandidateProfile
from app.modules.resumes.uploads import import_upload
from app.web.templating import templates

router = APIRouter()


def _back(msg: str | None = None, error: str | None = None, anchor: str = "") -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("error", error)) if v}
    return RedirectResponse(url="/profile" + (f"?{urlencode(params)}" if params else "") + (f"#{anchor}" if anchor else ""),
                            status_code=303)


def _text(value: str | None, limit: int = 200) -> str | None:
    value = " ".join((value or "").split())[:limit]
    return value or None


def _years(value) -> int | None:
    try:
        years = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0, min(60, years))


@router.get("/profile")
def profile_page(request: Request, msg: str | None = None, error: str | None = None, next: str | None = None,
                 user: User = Depends(require_member), session: Session = Depends(member_session)):
    profile = profiles.get_profile(session)
    resume = session.query(CandidateProfile).filter(CandidateProfile.is_active.is_(True)).order_by(
        CandidateProfile.version.desc()).first()
    return templates.TemplateResponse(request, "profile.html", {
        "user": user, "profile": profile, "completion": profiles.completion(session, user, profile),
        "experiences": session.query(ProfileExperience).order_by(ProfileExperience.is_current.desc(),
                                                                ProfileExperience.start_date.desc()).all(),
        "educations": session.query(ProfileEducation).order_by(ProfileEducation.end_date.desc()).all(),
        "resume": resume, "message": msg, "error": error, "next": next,
        "employment_types": EMPLOYMENT_TYPES, "workplaces": WORKPLACES,
    })


def _save_personal(session: Session, user: User, data: dict) -> str | None:
    for key, label in (("first_name", "first name"), ("last_name", "last name")):
        problem = name_problem(data.get(key, ""), label)
        if problem:
            return problem
    try:
        phone = normalize_phone(data.get("phone") or "")
    except AuthError as exc:
        return str(exc)
    profile = profiles.get_profile(session)
    account = session.get(User, user.id)  # the request's copy may be detached
    account.first_name, account.last_name, account.phone = data["first_name"].strip(), data["last_name"].strip(), phone
    profile.city, profile.province, profile.country = (_text(data.get(k), 120) for k in ("city", "province", "country"))
    session.commit()
    return None


def _save_professional(session: Session, data: dict) -> None:
    profile = profiles.get_profile(session)
    profile.headline = _text(data.get("headline"))
    profile.current_title = _text(data.get("current_title"))
    profile.years_experience = _years(data.get("years_experience"))
    profile.skills = profiles.clean_list(data.get("skills"), limit=50)
    session.commit()


def _save_preferences(session: Session, data: dict) -> None:
    profile = profiles.get_profile(session)
    profile.preferred_titles = profiles.clean_list(data.get("preferred_titles"))
    profile.preferred_locations = profiles.clean_list(data.get("preferred_locations"))
    profile.employment_types = [t for t in data.get("employment_types") or [] if t in EMPLOYMENT_TYPES]
    profile.workplaces = [w for w in data.get("workplaces") or [] if w in WORKPLACES]
    session.commit()


def _experience_values(data: dict) -> tuple[dict | None, str | None]:
    title, company = _text(data.get("job_title")), _text(data.get("company"))
    if not title or not company:
        return None, "Add the job title and the company."
    current = str(data.get("is_current", "")).lower() in {"1", "true", "on", "yes"}
    start, end = profiles.parse_month(data.get("start_date")), profiles.parse_month(data.get("end_date"))
    if start and end and end < start and not current:
        return None, "The end date is before the start date."
    return {"job_title": title, "company": company, "location": _text(data.get("location")), "start_date": start,
            "end_date": None if current else end, "is_current": current,
            "description": (data.get("description") or "").strip()[:5000] or None}, None


def _education_values(data: dict) -> tuple[dict | None, str | None]:
    institution = _text(data.get("institution"))
    if not institution:
        return None, "Add the school or institution."
    start, end = profiles.parse_month(data.get("start_date")), profiles.parse_month(data.get("end_date"))
    if start and end and end < start:
        return None, "The end date is before the start date."
    return {"institution": institution, "qualification": _text(data.get("qualification")),
            "field_of_study": _text(data.get("field_of_study")), "start_date": start, "end_date": end}, None


# ---- page forms --------------------------------------------------------------------

@router.post("/profile/personal")
async def save_personal(request: Request, user: User = Depends(require_member), session: Session = Depends(member_session)):
    error = _save_personal(session, user, dict((await request.form()).items()))
    return _back(error=error, anchor="personal") if error else _back(msg="Personal information saved.", anchor="personal")


@router.post("/profile/professional")
async def save_professional(request: Request, session: Session = Depends(member_session)):
    _save_professional(session, dict((await request.form()).items()))
    return _back(msg="Professional information saved.", anchor="professional")


@router.post("/profile/preferences")
async def save_preferences(request: Request, session: Session = Depends(member_session)):
    form = await request.form()
    _save_preferences(session, {**dict(form.items()), "employment_types": form.getlist("employment_types"),
                                "workplaces": form.getlist("workplaces")})
    return _back(msg="Job preferences saved.", anchor="preferences")


@router.post("/profile/experience")
async def add_experience(request: Request, session: Session = Depends(member_session)):
    values, error = _experience_values(dict((await request.form()).items()))
    if error:
        return _back(error=error, anchor="experience")
    session.add(ProfileExperience(**values))
    session.commit()
    return _back(msg="Experience added.", anchor="experience")


@router.post("/profile/experience/{item_id}")
async def update_experience(request: Request, item_id: int, session: Session = Depends(member_session)):
    item = session.get(ProfileExperience, item_id)
    if item is None:
        return _back(error="That entry no longer exists.", anchor="experience")
    values, error = _experience_values(dict((await request.form()).items()))
    if error:
        return _back(error=error, anchor="experience")
    for key, value in values.items():
        setattr(item, key, value)
    session.commit()
    return _back(msg="Experience updated.", anchor="experience")


@router.post("/profile/experience/{item_id}/delete")
def delete_experience(item_id: int, session: Session = Depends(member_session)):
    session.query(ProfileExperience).filter(ProfileExperience.id == item_id).delete(synchronize_session=False)
    session.commit()
    return _back(msg="Experience removed.", anchor="experience")


@router.post("/profile/education")
async def add_education(request: Request, session: Session = Depends(member_session)):
    values, error = _education_values(dict((await request.form()).items()))
    if error:
        return _back(error=error, anchor="education")
    session.add(ProfileEducation(**values))
    session.commit()
    return _back(msg="Education added.", anchor="education")


@router.post("/profile/education/{item_id}")
async def update_education(request: Request, item_id: int, session: Session = Depends(member_session)):
    item = session.get(ProfileEducation, item_id)
    if item is None:
        return _back(error="That entry no longer exists.", anchor="education")
    values, error = _education_values(dict((await request.form()).items()))
    if error:
        return _back(error=error, anchor="education")
    for key, value in values.items():
        setattr(item, key, value)
    session.commit()
    return _back(msg="Education updated.", anchor="education")


@router.post("/profile/education/{item_id}/delete")
def delete_education(item_id: int, session: Session = Depends(member_session)):
    session.query(ProfileEducation).filter(ProfileEducation.id == item_id).delete(synchronize_session=False)
    session.commit()
    return _back(msg="Education removed.", anchor="education")


@router.post("/profile/import-resume")
def import_resume_into_profile(session: Session = Depends(member_session)):
    added = profiles.import_from_resume(session)
    if not added:
        return _back(error="Nothing to import: add a resume first, or your profile already lists experience.",
                     anchor="experience")
    return _back(msg=f"Imported {added} entries from your resume. Review them below.", anchor="experience")


# ---- JSON API ------------------------------------------------------------------------

def _profile_json(session: Session, user: User) -> dict:
    profile = profiles.get_profile(session)
    completion = profiles.completion(session, user, profile)

    def d(value):
        return value.isoformat() if value else None
    return {
        "first_name": user.first_name, "last_name": user.last_name, "email": user.email, "phone": user.phone,
        "city": profile.city, "province": profile.province, "country": profile.country,
        "headline": profile.headline, "current_title": profile.current_title,
        "years_experience": profile.years_experience, "skills": profile.skills or [],
        "preferred_titles": profile.preferred_titles or [], "preferred_locations": profile.preferred_locations or [],
        "employment_types": profile.employment_types or [], "workplaces": profile.workplaces or [],
        "experience": [{"id": e.id, "job_title": e.job_title, "company": e.company, "location": e.location,
                        "start_date": d(e.start_date), "end_date": d(e.end_date), "is_current": e.is_current,
                        "description": e.description} for e in session.query(ProfileExperience)],
        "education": [{"id": e.id, "institution": e.institution, "qualification": e.qualification,
                       "field_of_study": e.field_of_study, "start_date": d(e.start_date), "end_date": d(e.end_date)}
                      for e in session.query(ProfileEducation)],
        "completion_percentage": completion.percent,
        "suggestions": [label for label, _ in completion.missing],
    }


class ProfileIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    headline: str | None = None
    current_title: str | None = None
    years_experience: int | None = None
    skills: list[str] | None = None
    preferred_titles: list[str] | None = None
    preferred_locations: list[str] | None = None
    employment_types: list[str] | None = None
    workplaces: list[str] | None = None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@router.get("/api/profile")
def api_get_profile(user: User = Depends(require_member), session: Session = Depends(member_session)):
    return _profile_json(session, user)


@router.put("/api/profile")
def api_put_profile(body: ProfileIn, user: User = Depends(require_member), session: Session = Depends(member_session)):
    """Update any of the profile's fields (fields left out are unchanged)."""
    current = _profile_json(session, user)
    merged = {**current, **body.model_dump(exclude_unset=True)}
    error = _save_personal(session, user, {**merged, "phone": merged.get("phone") or ""})
    if error:
        return _error(400, "invalid_profile", error)
    _save_professional(session, merged)
    _save_preferences(session, merged)
    return _profile_json(session, session.get(User, user.id))


@router.post("/api/profile/experience", status_code=201)
def api_add_experience(body: dict, user: User = Depends(require_member), session: Session = Depends(member_session)):
    values, error = _experience_values(body)
    if error:
        return _error(400, "invalid_experience", error)
    item = ProfileExperience(**values)
    session.add(item)
    session.commit()
    return JSONResponse({"id": item.id}, status_code=201)


@router.post("/api/profile/education", status_code=201)
def api_add_education(body: dict, user: User = Depends(require_member), session: Session = Depends(member_session)):
    values, error = _education_values(body)
    if error:
        return _error(400, "invalid_education", error)
    item = ProfileEducation(**values)
    session.add(item)
    session.commit()
    return JSONResponse({"id": item.id}, status_code=201)


@router.post("/api/profile/resume", status_code=201)
async def api_upload_resume(resume: UploadFile = File(...), session: Session = Depends(member_session)):
    """PDF or DOCX, up to 10 MB; becomes the active resume (private to the user)."""
    result, error = await import_upload(session, resume)
    if error:
        return _error(400, "invalid_resume", error)
    return JSONResponse({"version": result.profile.version, "created": result.created}, status_code=201)
