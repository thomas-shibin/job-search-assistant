"""Resume pages: My resume (upload, view, download, edit), the Canadian resume
check, Quick Tailor, and a tracked job's tailored resume (re-tailor, remove
paragraphs, download DOCX/PDF). Uploads are handled by
app/modules/resumes/uploads.py; tailoring by tailor.py; the Canadian check by
canadian.py and canadian_convert.py."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.tenancy import require_tenant
from app.integrations.ai.factory import AIProviderConfigError
from app.integrations.libreoffice import PdfConversionError, convert_to_pdf
from app.modules.applications.service import get_job_detail
from app.modules.applications.views import get_job_or_404
from app.core.database.session import get_session
from app.modules.access import rbac
from app.modules.auth.deps import current_user, member_session, require_member
from app.modules.auth.sessions import client_ip
from app.modules.auth.settings import AuthSettings
from app.modules.auth.models import User
from app.modules.jobs.ingestion.manual import import_manual_job
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.jobs.models import Job
from app.modules.matching.fit import active_candidate
from app.modules.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.modules.matching.workflows import ANALYSIS_UNAVAILABLE
from app.modules.resumes import ats, canadian, canadian_convert, check_sources, public_check, uploads, versions
from app.modules.resumes.editor import parse_edit_form, save_edit
from app.modules.resumes.models import CandidateProfile
from app.modules.resumes.pipeline import generate_resume_bundle
from app.modules.resumes.tailor import (
    BaseResumeMissingError,
    _base_docx,
    rebuild_tailored_resume,
    resume_download_name,
    tailor_resume,
)
from app.modules.resumes.tailor import NoActiveCandidateProfileError as TailorNoProfileError
from app.modules.resumes.tailoring_models import TailoredBulletProvenance
from app.modules.usage import quota
from app.modules.usage.models import ActivityKind
from app.web.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)


# ---- My resume -------------------------------------------------------------------------

@router.post("/resume/upload")
async def upload_resume(
    resume: UploadFile = File(...), next: str = Form("/"), session: Session = Depends(member_session)
):
    error = (await uploads.import_upload(session, resume))[1]
    path, _, fragment = next.partition("#")
    if not path.startswith("/") or path.startswith("//"):  # only ever redirect within this app
        path, fragment = "/", ""
    if path.startswith("/jobs/"):
        query = f"?{urlencode({'error': error})}" if error else ""
    else:
        query = f"?{urlencode({'msg': error or 'Resume imported -- you can now analyze jobs against it.'})}"
    return RedirectResponse(url=f"{path}{query}{'#' + fragment if fragment else ''}", status_code=303)


@router.get("/resume")
def resume_page(request: Request, msg: str | None = None, error: str | None = None,
                session: Session = Depends(member_session)):
    """Your base resume: what the app read from it, and where to replace it."""
    current = active_candidate(session)
    versions = session.query(CandidateProfile).order_by(CandidateProfile.version.desc()).limit(10).all()
    return templates.TemplateResponse(request, "resume.html", {
        "current": current, "versions": versions, "message": msg, "error": error,
        "file_available": bool(current and uploads.resume_file(current)),
        "is_docx": bool(current and Path(current.raw_resume_path).suffix.lower() == ".docx"),
        "unparsed": [part for part in (current.raw_unparsed_sections or "").split("\n\n---\n\n") if part.strip()]
        if current else [],
    })


@router.post("/resume")
async def resume_page_upload(resume: UploadFile = File(...), session: Session = Depends(member_session)):
    result, error = await uploads.import_upload(session, resume)
    if error:
        return RedirectResponse(url=f"/resume?{urlencode({'error': error})}", status_code=303)
    if not result.created:
        message = "That's the same content as your current resume -- nothing changed."
    else:
        c = result.counts
        message = (f"Resume uploaded and in use (version {result.profile.version}): {c['employments']} roles, "
                   f"{c['bullets']} bullets, {c['skills']} skills. Check below that everything was read correctly.")
    return RedirectResponse(url=f"/resume?{urlencode({'msg': message})}", status_code=303)


@router.get("/resume/edit")
def resume_edit_page(request: Request, session: Session = Depends(member_session)):
    current = active_candidate(session)
    if current is None:
        return RedirectResponse(url="/resume?" + urlencode({"error": "Upload your resume first, then edit it here."}),
                                status_code=303)
    return templates.TemplateResponse(request, "resume_edit.html", {"current": current, "errors": []})


@router.post("/resume/edit")
async def resume_edit_save(request: Request, session: Session = Depends(member_session)):
    """Save edited resume content as a new version (see app/modules/resumes/editor.py)."""
    current = active_candidate(session)
    if current is None:
        return RedirectResponse(url="/resume", status_code=303)
    form = await request.form()
    edit, errors = parse_edit_form(form)

    def again(**extra):
        return templates.TemplateResponse(request, "resume_edit.html", {
            "current": current, "errors": errors, "form": form, **extra}, status_code=400)

    if errors:
        return again()
    try:
        base_docx = _base_docx(current)
    except BaseResumeMissingError:
        base_docx = None  # nothing to keep: the standard layout it is
    result = save_edit(session, current, edit, base_docx=base_docx, upload_dir=uploads.UPLOAD_DIR,
                       allow_layout_change=form.get("allow_layout_change") == "1")
    if result.needs_layout_consent:
        return again(layout_reasons=result.reasons)
    message = f"Saved as version {result.profile.version}."
    quota.record(session, require_tenant(session), ActivityKind.RESUME_EDIT, f"Version {result.profile.version}"
                 + (" (standard layout)" if result.layout_changed else ""))
    if result.layout_changed:
        message += " Your resume now uses the standard layout."
    return RedirectResponse(url="/resume?" + urlencode({"msg": message}), status_code=303)


@router.get("/resume/file")
def download_base_resume(session: Session = Depends(member_session)):
    current = active_candidate(session)
    path = uploads.resume_file(current) if current else None
    if path is None:
        raise HTTPException(status_code=404, detail="No base resume file on disk")
    return FileResponse(path, filename=f"base_resume{path.suffix}")



# ---- versions: compare, download, go back ----------------------------------------------------

def _local(next_url: str | None, default: str) -> str:
    """Only ever redirect within this app."""
    return next_url if next_url and next_url.startswith("/") and not next_url.startswith("//") else default


@router.get("/resume/compare")
def compare_versions(request: Request, session: Session = Depends(member_session)):
    """Two versions side by side: what was changed, added, removed or moved.
    Defaults to the version in use against the one before it."""
    params = request.query_params
    every = versions.all_versions(session)
    if len(every) < 2:
        return RedirectResponse(url="/resume?" + urlencode({"error": "There's only one version of your resume, "
                                                            "so there's nothing to compare yet."}), status_code=303)
    number = lambda key: int(params[key]) if str(params.get(key, "")).isdigit() else None  # noqa: E731
    new = versions.by_number(session, number("to")) if number("to") else active_candidate(session) or every[0]
    old = versions.by_number(session, number("from")) if number("from") else versions.previous(session, new)
    if new is None or old is None:
        return RedirectResponse(url="/resume?" + urlencode({"error": "That version isn't in your history."}),
                                status_code=303)
    old_lines, new_lines = versions.lines(old), versions.lines(new)
    comparison = None
    if old_lines is not None and new_lines is not None:
        comparison = versions.compare(old_lines, new_lines, only_changes=params.get("all") != "1")
    return templates.TemplateResponse(request, "resume_compare.html", {
        "old": old, "new": new, "every": every, "comparison": comparison, "show_all": params.get("all") == "1",
        "current": active_candidate(session), "message": params.get("msg"),
    })


@router.get("/resume/versions/{version}/file")
def download_resume_version(version: int, session: Session = Depends(member_session)):
    profile = versions.by_number(session, version)
    found = versions.file_bytes(profile) if profile else None
    if found is None:
        raise HTTPException(status_code=404, detail="That version's file isn't available")
    data, suffix = found
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if suffix == ".docx"
             else "application/pdf")
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="resume_version_{version}{suffix}"'})


@router.post("/resume/versions/{version}/use")
def use_resume_version(version: int, next: str = Form("/resume"), session: Session = Depends(member_session)):
    """Go back to (or forward to) a version: it becomes the one in use; nothing is deleted."""
    target = _local(next, "/resume")
    profile = versions.by_number(session, version)
    joiner = "&" if "?" in target else "?"
    if profile is None:
        return RedirectResponse(url=target + joiner + urlencode({"error": "That version isn't in your history."}),
                                status_code=303)
    current = active_candidate(session)
    if current is not None and current.id == profile.id:
        message = f"Version {version} is already in use."
    else:
        versions.use(session, profile)
        quota.record(session, require_tenant(session), ActivityKind.RESUME_EDIT, f"Back to version {version}")
        message = (f"Version {version} is in use again. Version {current.version} is kept in your history, "
                   "and you can switch back to it any time." if current else f"Version {version} is in use.")
    return RedirectResponse(url=target + joiner + urlencode({"msg": message}), status_code=303)

# ---- Canadian resume check -------------------------------------------------------------------

def _canadian_page(request: Request, session: Session, *, message: str | None = None, error: str | None = None,
                   suggestions: dict[int, str] | None = None, converted: bool = False, status_code: int = 200):
    current = active_candidate(session)
    token = request.cookies.get(public_check.COOKIE)
    pending, pending_seconds = public_check.held_name(token), public_check.seconds_left(token)
    if pending and current is not None:
        held, mine = public_check.held(token), versions.file_bytes(current)
        if held and mine and held[1] == mine[0]:  # they checked the resume they already use: nothing to offer
            public_check.take(token)
            pending, pending_seconds = None, 0
    if current is None and pending is None:
        return RedirectResponse(url="/resume?" + urlencode({"error": "Upload your resume first, then check it here."}),
                                status_code=303)
    if current is None:  # signed up to fix the resume they checked: offer it
        return templates.TemplateResponse(request, "resume_canadian.html", {
            "current": None, "review": None, "pending": pending, "pending_seconds": pending_seconds,
            "message": message, "error": error})
    try:
        result = canadian_convert.review(current)
    except BaseResumeMissingError as exc:
        result, error = None, error or str(exc)
    if result is None and not error:
        error = "Couldn't read your resume file. Upload it again on the My resume page."
    return templates.TemplateResponse(request, "resume_canadian.html", {
        "current": current, "review": result, "suggestions": suggestions or {}, "converted": converted,
        "titles_available": canadian_convert.titles_available(session), "message": message, "error": error,
        "ai_allowance": quota.ai_allowance_for(session, getattr(request.state, "user", None)),
        "roles": current.employments[:canadian_convert.MAX_ROLES], "pending": pending,
        "pending_seconds": pending_seconds, "ats_layout_fixes": ats.LAYOUT_FIXES,
        "previous": versions.previous(session, current) if converted else None,
    }, status_code=status_code)


@router.get("/resume/canadian")
def canadian_check_page(request: Request, msg: str | None = None, error: str | None = None, converted: int = 0,
                        session: Session = Depends(member_session)):
    """What to change in your resume for Canadian employers, and the fixes to apply."""
    return _canadian_page(request, session, message=msg, error=error, converted=bool(converted))


def _typed_titles(form) -> dict[int, str]:
    """{employment id: equivalent} from the page's title boxes (filled ones only)."""
    return {int(m.group(1)): str(value) for key, value in form.items()
            if (m := re.fullmatch(r"title-(\d+)", key)) and str(value).strip()}


@router.post("/resume/canadian/titles")
async def canadian_title_suggestions(request: Request, session: Session = Depends(member_session),
                                     user: User = Depends(require_member)):
    """Suggest Canadian equivalents for the resume's job titles (one resume action).
    Titles the member already typed are kept."""
    current = active_candidate(session)
    if current is None:
        return RedirectResponse(url="/resume", status_code=303)
    typed = _typed_titles(await request.form())
    try:
        with quota.ai_action(session, user, ActivityKind.CANADIAN_TITLES, "Canadian job titles"):
            suggestions = canadian_convert.suggest_titles(session, current)
    except quota.LimitReached as exc:
        return _canadian_page(request, session, error=str(exc), suggestions=typed, status_code=429)
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _canadian_page(request, session, error=ANALYSIS_UNAVAILABLE, suggestions=typed, status_code=503)
    except canadian_convert.TitleSuggestionError as exc:
        return _canadian_page(request, session, error=str(exc), suggestions=typed, status_code=503)
    count = len(suggestions)
    message = (f"Suggested a Canadian equivalent for {count} job title{'s' if count != 1 else ''}. Check each one "
               "below: change or clear it if it doesn't describe your work, then convert.") if count else \
        "Your job titles are already clear to Canadian employers -- no equivalents needed."
    return _canadian_page(request, session, message=message, suggestions={**suggestions, **typed})


def _chosen_changes(form) -> tuple[set[str], dict[str, set[int]]]:
    """The fixes the member chose: "fix=<key>" for every change a fix makes,
    "change=<key>:<n>" for single ones (a line, a table, a text box)."""
    known = set(canadian.FIXES) | set(ats.FIXES)
    whole = {str(v) for v in form.getlist("fix")} & known
    only: dict[str, set[int]] = {}
    for value in form.getlist("change"):
        key, _, n = str(value).partition(":")
        if key in known and key not in whole and re.fullmatch(r"-?\d{1,6}", n):
            only.setdefault(key, set()).add(int(n))
    return whole | set(only), only


@router.post("/resume/canadian")
async def canadian_convert_resume(request: Request, session: Session = Depends(member_session)):
    """Apply the chosen fixes as a new resume version."""
    current = active_candidate(session)
    if current is None:
        return RedirectResponse(url="/resume", status_code=303)
    form = await request.form()
    chosen, only = _chosen_changes(form)
    try:
        result = canadian_convert.convert(session, current, chosen, _typed_titles(form), only)
    except BaseResumeMissingError as exc:
        return RedirectResponse(url="/resume/canadian?" + urlencode({"error": str(exc)}), status_code=303)
    if result.profile is None:
        error = " ".join(result.problems) or "Nothing to change: tick at least one fix or add a job title equivalent."
        return RedirectResponse(url="/resume/canadian?" + urlencode({"error": error}), status_code=303)
    done = "; ".join(result.done)
    message = f"Converted and saved as version {result.profile.version}: {done[:1].upper() + done[1:]}."
    if result.problems:
        message += " " + " ".join(result.problems)
    return RedirectResponse(url="/resume/canadian?" + urlencode({"msg": message, "converted": 1}), status_code=303)


# ---- a tracked job's tailored resume -------------------------------------------------------

def _job_redirect(job_id: int, **params: str) -> RedirectResponse:
    return RedirectResponse(url=f"/jobs/{job_id}?{urlencode(params)}#resume", status_code=303)


@router.post("/jobs/{job_id}/retailor")
def retailor_job(job_id: int, session: Session = Depends(member_session), user: User = Depends(require_member)):
    """Re-tailor an already-tailored job with AI (whatever its score), in
    your resume's own design, without changing the job's status."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job found with id={job_id}")
    try:
        config = user_config(session)
        with quota.ai_action(session, user, ActivityKind.AI_TAILOR, quota.job_label(job), job_id=job.id):
            result = tailor_resume(session, job, config, update_status=False)
    except quota.LimitReached as exc:
        return _job_redirect(job_id, error=str(exc))
    except ConfigError as exc:
        return _job_redirect(job_id, error=f"Settings are invalid: {'; '.join(exc.errors)}")
    except TailorNoProfileError as exc:
        return _job_redirect(job_id, error=str(exc))
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _job_redirect(job_id, error=ANALYSIS_UNAVAILABLE)
    try:
        result.tailored_resume.pdf_path = str(convert_to_pdf(result.docx_path))
        session.commit()
    except PdfConversionError:
        pass  # DOCX is still there; PDF needs LibreOffice
    return _job_redirect(job_id, msg="Resume re-tailored -- download it below.")


@router.post("/jobs/{job_id}/removals")
async def apply_removals(job_id: int, request: Request, session: Session = Depends(member_session)):
    """Take the paragraphs the user ticked out of this job's tailored resume (and
    put back any they unticked), then rebuild it from the base resume -- no AI call."""
    get_job_or_404(session, job_id)
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None:
        raise HTTPException(status_code=404, detail="No tailored resume for this job")
    chosen = {int(v) for v in (await request.form()).getlist("remove") if str(v).isdigit()}
    rows = session.query(TailoredBulletProvenance).filter(
        TailoredBulletProvenance.tailored_resume_id == detail.tailored_resume.id,
        TailoredBulletProvenance.source_text.isnot(None)).all()
    for row in rows:
        row.removed = row.id in chosen
    session.commit()
    try:
        config = user_config(session)
        path = rebuild_tailored_resume(session, detail.tailored_resume, config)
    except (ConfigError, TailorNoProfileError) as exc:
        return _job_redirect(job_id, error=str(exc))
    if path is None:
        return _job_redirect(job_id, error="Couldn't rebuild the resume: your base resume is missing.")
    try:
        detail.tailored_resume.pdf_path = str(convert_to_pdf(path))
        session.commit()
    except PdfConversionError:
        pass
    count = sum(1 for row in rows if row.removed)
    return _job_redirect(job_id, msg=f"Resume rebuilt with {count} paragraph{'s' if count != 1 else ''} removed."
                         if count else "Resume rebuilt with every paragraph included.")


@router.get("/jobs/{job_id}/resume.docx")
def download_resume_docx(job_id: int, session: Session = Depends(member_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.docx_path:
        raise HTTPException(status_code=404, detail="No tailored resume DOCX for this job")
    return FileResponse(detail.tailored_resume.docx_path, filename=_download_name(session, detail, ".docx"))


@router.get("/jobs/{job_id}/resume.pdf")
def download_resume_pdf(job_id: int, session: Session = Depends(member_session)):
    detail = get_job_detail(session, job_id)
    if detail is None or detail.tailored_resume is None or not detail.tailored_resume.pdf_path:
        raise HTTPException(status_code=404, detail="No tailored resume PDF for this job")
    return FileResponse(detail.tailored_resume.pdf_path, filename=_download_name(session, detail, ".pdf"))


def _download_name(session: Session, detail, suffix: str) -> str:
    """"First Last - Company - timestamp.docx" (see resume_download_name)."""
    try:
        config = user_config(session)
    except ConfigError:
        return f"tailored_resume{suffix}"
    return resume_download_name(config, active_candidate(session), detail.job.company,
                                detail.tailored_resume.generated_at, suffix)


# ---- Quick Tailor ------------------------------------------------------------------------

def _quick_form_response(request: Request, *, error: str, status_code: int = 400):
    return templates.TemplateResponse(request, "quick.html", {"error": error}, status_code=status_code)


@router.get("/quick")
def quick_tailor_form(request: Request, session: Session = Depends(member_session)):
    return templates.TemplateResponse(request, "quick.html", {
        "error": None, "ai_allowance": quota.ai_allowance_for(session, getattr(request.state, "user", None))})


@router.post("/quick")
async def quick_tailor_submit(
    request: Request,
    resume: UploadFile | None = File(None),
    description: str = Form(...),
    url: str = Form(""),
    company: str = Form(""),
    title: str = Form(""),
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
):
    """One-shot upload-resume + paste-job-description -> tailored resume.
    Unlike the automated pipeline, this always tailors regardless of
    `matching.minimum_score` -- the user already picked this job by pasting
    it in, so there's nothing left to filter for.
    """
    if not description.strip():
        return _quick_form_response(request, error="Paste the job description text.")

    uploading = resume is not None and bool(resume.filename)
    if uploading and Path(resume.filename).suffix.lower() not in uploads.ALLOWED_RESUME_SUFFIXES:
        return _quick_form_response(request, error="Resume must be a .docx or .pdf file.")
    if not uploading and active_candidate(session) is None:
        return _quick_form_response(request, error="Upload your resume (here, or once on the My resume page) first.")

    try:
        config = user_config(session)
    except ConfigError as exc:
        return _quick_form_response(request, error=f"Settings are invalid: {'; '.join(exc.errors)}", status_code=500)
    try:
        quota.check_ai(session, user)  # say so before saving anything
    except quota.LimitReached as exc:
        return _quick_form_response(request, error=str(exc), status_code=429)

    if uploading:
        upload_error = (await uploads.import_upload(session, resume))[1]
        if upload_error:
            return _quick_form_response(request, error=upload_error)

    raw_job = import_manual_job(
        description=description, url=url or None, company=company or None, title=title or None
    )
    ingest_result = ingest_raw_job(session, raw_job)
    job = ingest_result.job

    try:
        with quota.ai_action(session, user, ActivityKind.QUICK_TAILOR, quota.job_label(job), job_id=job.id):
            try:
                analyze_job(session, job, config)
            except NoActiveCandidateProfileError:
                pass  # the import above just created one; this can't actually happen
            generate_resume_bundle(session, job, config)
    except quota.LimitReached as exc:
        return _quick_form_response(request, error=str(exc), status_code=429)
    except AIProviderConfigError:
        logger.exception("Analysis provider is not configured")
        return _quick_form_response(request, error=ANALYSIS_UNAVAILABLE, status_code=503)
    except NoActiveCandidateProfileError:
        return _quick_form_response(request, error="Upload your resume first (here, or on the My resume page).")

    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


# ---- the free check, for everyone (/canadian-resume-check) --------------------------------------

def _public_check_page(request: Request, *, review=None, error: str | None = None, status_code: int = 200,
                       held_seconds: int = 0):
    user = getattr(request.state, "user", None)
    response = templates.TemplateResponse(request, "canadian_check_public.html", {
        "review": review, "error": error, "member": user is not None and not user.is_guest,
        "hold_minutes": public_check.HOLD_SECONDS // 60, "held_seconds": held_seconds,
        "sources": check_sources}, status_code=status_code)
    if review is not None:  # their resume's text is on the page: keep it out of every cache
        response.headers["Cache-Control"] = "no-store"
    return response


def _check_switched_off(session: Session) -> RedirectResponse | None:
    if rbac.feature_on(session, "canadian_resume"):
        return None
    return RedirectResponse(url="/?" + urlencode({"msg": "The resume check is switched off right now."}),
                            status_code=303)


@router.get("/canadian-resume-check")
def public_check_page(request: Request, user: User | None = Depends(current_user),
                      session: Session = Depends(get_session)):
    """Check any resume against Canadian conventions: free, no account needed."""
    return _check_switched_off(session) or _public_check_page(request)


@router.post("/canadian-resume-check")
async def public_check_upload(request: Request, resume: UploadFile | None = File(None),
                              user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    if (off := _check_switched_off(session)) is not None:
        return off
    if not public_check.limiter.allow(client_ip(request)):
        return _public_check_page(request, error="You've checked a lot of resumes in the last hour. "
                                                 "Try again a little later.", status_code=429)
    if resume is None or not resume.filename:
        return _public_check_page(request, error="Choose your resume (PDF or Word) first.", status_code=400)
    data = await resume.read(uploads.MAX_RESUME_BYTES + 1)
    try:
        review = public_check.check(resume.filename, data)
    except public_check.CheckError as exc:
        return _public_check_page(request, error=str(exc), status_code=400)
    # Kept for "Fix my resume" -- or, when it's ready, for matching it to jobs after signing up.
    token = public_check.hold(review.filename, data, replaces=request.cookies.get(public_check.COOKIE))
    response = _public_check_page(request, review=review, held_seconds=public_check.seconds_left(token))
    response.set_cookie(public_check.COOKIE, token, max_age=public_check.HOLD_SECONDS, httponly=True,
                        samesite="lax", secure=AuthSettings().secure_cookies, path="/")
    return response


@router.post("/resume/canadian/discard")
def discard_checked_resume(request: Request, session: Session = Depends(member_session)):
    """No thanks: delete the resume held from the free check now, rather than in 30 minutes."""
    public_check.take(request.cookies.get(public_check.COOKIE))
    response = RedirectResponse(url="/resume/canadian?" + urlencode({"msg": "Deleted the resume you checked. "
                                                                     "Your current resume is unchanged."}),
                                status_code=303)
    response.delete_cookie(public_check.COOKIE, path="/")
    return response


_CLAIM_NEXT = {"/resume/canadian": "Your resume is ready to fix. The fixes are ticked below: check them and convert.",
               "/jobs": "Your resume is in. Every job now shows how well you match it."}


@router.post("/resume/canadian/claim")
async def claim_checked_resume(request: Request, session: Session = Depends(member_session)):
    """Make the resume checked on the free page this member's resume: to fix it,
    or (when it needed nothing) to see match scores on jobs."""
    form = await request.form()
    next_url = str(form.get("next") or "/resume/canadian")
    next_url = next_url if next_url in _CLAIM_NEXT else "/resume/canadian"
    held = public_check.take(request.cookies.get(public_check.COOKIE))
    if held is None:
        response = RedirectResponse(url="/resume?" + urlencode({
            "error": f"The resume you checked is no longer held (we keep it for {public_check.HOLD_SECONDS // 60} "
                     "minutes). Upload it here, then open the Canadian resume check."}), status_code=303)
    else:
        result, error = uploads.import_resume_file(session, *held)
        if error:
            response = RedirectResponse(url="/resume?" + urlencode({"error": error}), status_code=303)
        else:
            response = RedirectResponse(url=f"{next_url}?" + urlencode({"msg": _CLAIM_NEXT[next_url]}),
                                        status_code=303)
    response.delete_cookie(public_check.COOKIE, path="/")
    return response
