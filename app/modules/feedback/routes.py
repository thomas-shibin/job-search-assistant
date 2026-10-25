"""The review popup's two buttons, the Contact us page, and the admin page
that lists reviews and messages (see service.py)."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.modules.access import service as access
from app.modules.auth.deps import admin_session, current_user, member_session, require_admin, require_member
from app.modules.auth.models import User
from app.modules.feedback import service
from app.modules.usage.plans import CONTACT_EMAIL
from app.web.templating import safe_next, templates

router = APIRouter()


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _back(next_path: str | None, **params: str) -> RedirectResponse:
    target = safe_next(next_path, "/jobs")
    if params:
        target += ("&" if "?" in target else "?") + urlencode(params)
    return RedirectResponse(target, status_code=303)


# ---- the review popup --------------------------------------------------------------

@router.post("/feedback")
def send_review(request: Request, jobs_rating: str = Form(""), enough_jobs_rating: str = Form(""),
                resume_rating: str = Form(""), features_rating: str = Form(""), comment: str = Form(""),
                page: str = Form(""), user: User = Depends(require_member),
                session: Session = Depends(member_session)):
    ratings = {"jobs_rating": jobs_rating, "enough_jobs_rating": enough_jobs_rating,
               "resume_rating": resume_rating, "features_rating": features_rating}
    try:
        service.save_review(session, user, ratings, comment, page=safe_next(page, "") or None)
    except service.FeedbackError as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _back(page, msg=str(exc))
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return _back(page, msg="Thank you for your feedback!")


@router.post("/feedback/later")
def review_later(request: Request, never: str = Form(""), page: str = Form(""),
                 user: User = Depends(require_member), session: Session = Depends(member_session)):
    service.postpone(session, user, never=never == "1")
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return _back(page)


# ---- Contact us ----------------------------------------------------------------------

def _contact(request: Request, user: User | None, *, values: dict | None = None, error: str | None = None,
             sent: bool = False, status_code: int = 200):
    return templates.TemplateResponse(request, "contact.html", {
        "topics": service.TOPICS, "values": values or {}, "error": error, "sent": sent, "contact": CONTACT_EMAIL,
        "max_title": service.MAX_TITLE, "max_message": service.MAX_MESSAGE, "member": user,
    }, status_code)


@router.get("/contact")
def contact_page(request: Request, sent: str | None = None, topic: str | None = None,
                 user: User | None = Depends(current_user)):
    member = user if user is not None and not user.is_guest else None
    return _contact(request, member, values={"topic": topic or ""}, sent=sent == "1")


@router.post("/contact")
def send_contact(request: Request, topic: str = Form("other"), title: str = Form(""), message: str = Form(""),
                 user: User = Depends(require_member), session: Session = Depends(member_session)):
    try:
        service.send_contact(session, user, topic, title, message)
    except service.FeedbackError as exc:
        return _contact(request, user, values={"topic": topic, "title": title, "message": message},
                        error=str(exc), status_code=400)
    return RedirectResponse("/contact?sent=1", status_code=303)


# ---- admin -----------------------------------------------------------------------------

@router.get("/admin/feedback")
def feedback_page(request: Request, show: str = "reviews", msg: str | None = None, unanswered: str = "",
                  session: Session = Depends(admin_session)):
    from app.modules.help import models as help_log

    show = show if show in ("reviews", "open", "handled", "help") else "reviews"
    return templates.TemplateResponse(request, "admin/feedback.html", {
        "show": show, "message": msg, "questions": service.QUESTIONS, "short": service.SHORT_LABELS, "topics": service.TOPICS,
        "summary": service.review_summary(session), "counts": service.message_counts(session),
        "reviews": service.recent_reviews(session) if show == "reviews" else [],
        "messages": service.messages(session, handled=show == "handled") if show in ("open", "handled") else [],
        "help_counts": help_log.question_counts(session), "unanswered": unanswered == "1",
        "help_questions": help_log.recent_questions(session, unanswered=unanswered == "1") if show == "help" else [],
    })


@router.post("/admin/feedback/messages/{message_id}")
def handle_message(request: Request, message_id: int, handled: str = Form("1"),
                   session: Session = Depends(admin_session), admin: User = Depends(require_admin)):
    saved = service.mark_handled(session, message_id, handled=handled == "1")
    if saved is None:
        raise HTTPException(status_code=404, detail="No such message")
    access.audit(session, admin, "contact.message.handled" if handled == "1" else "contact.message.reopened",
                 "contact_message", message_id, None, None, request)
    session.commit()
    show = "open" if handled == "1" else "handled"
    return RedirectResponse(f"/admin/feedback?{urlencode({'show': show, 'msg': 'Saved.'})}", status_code=303)
