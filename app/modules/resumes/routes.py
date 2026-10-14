"""Resume pages: My resume (upload, view, download, edit), Quick Tailor, and a
tracked job's tailored resume (re-tailor, remove paragraphs, download DOCX/PDF).
Uploads are handled by app/modules/resumes/uploads.py; tailoring by tailor.py."""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.tenancy import require_tenant
from app.integrations.ai.factory import AIProviderConfigError
from app.integrations.libreoffice import PdfConversionError, convert_to_pdf
from app.modules.applications.service import get_job_detail
from app.modules.applications.views import get_job_or_404
from app.modules.auth.deps import member_session, require_member
from app.modules.auth.models import User
from app.modules.jobs.ingestion.manual import import_manual_job
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.jobs.models import Job
from app.modules.matching.fit import active_candidate
from app.modules.matching.pipeline import NoActiveCandidateProfileError, analyze_job
from app.modules.matching.workflows import ANALYSIS_UNAVAILABLE
from app.modules.resumes import uploads
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
