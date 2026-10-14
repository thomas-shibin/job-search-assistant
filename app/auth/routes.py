"""Sign-in pages (/login, /signup -- the email-code flow in app/auth/api.py,
plus a password form that works without JavaScript), Google and Facebook
sign-in, email verification for an email change, password reset, and the
account (security) page."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from app.auth import oauth
from app.auth import service
from app.auth import rbac
from app.auth.deps import current_user, require_member, require_viewer
from app.auth.passwords import PASSWORD_RULES
from app.auth.ratelimit import email_limiter, login_limiter
from app.auth.service import AuthError
from app.auth.sessions import client_ip, end_other_sessions, end_session, start_session
from app.auth.settings import AuthSettings
from app.core.database.session import get_session
from app.models.user import AuthSession, User
from app.ui.templating import safe_next, templates

router = APIRouter()

_TOO_MANY = "Too many attempts from your network. Please wait a few minutes and try again."


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _page(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    settings = AuthSettings()
    base = {"google": settings.provider_enabled("google"), "facebook": settings.provider_enabled("facebook"),
            "auth_providers": {"google": settings.provider_enabled("google")}, "password_rules": PASSWORD_RULES}
    return templates.TemplateResponse(request, name, {**base, **(context or {})}, status_code=status_code)


def _signed_in_redirect(session: Session, request: Request, user: User, next_path: str,
                        method: str = "password") -> RedirectResponse:
    response = _redirect(safe_next(next_path))
    start_session(session, request, response, user, method)
    return response


# ---- sign in / sign up ---------------------------------------------------------

def _signed_in_target(session: Session, user: User, next_path: str) -> str:
    from app.profile.service import after_sign_in

    return after_sign_in(session, user, safe_next(next_path, "/jobs"))


@router.get("/login")
def login_page(request: Request, next: str = "/jobs", msg: str | None = None, error: str | None = None,
               user: User | None = Depends(current_user)):
    if user is not None and not user.is_guest:
        return _redirect(safe_next(next, "/jobs"))
    return _page(request, "auth/login.html", {"next": safe_next(next, "/jobs"), "msg": msg, "error": error})


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/jobs"),
          session: Session = Depends(get_session)):
    """Password sign-in for browsers without JavaScript (the page's script uses /api/auth/login)."""
    if not login_limiter.allow(client_ip(request)):
        return _page(request, "auth/login.html", {"next": next, "error": _TOO_MANY}, 429)
    try:
        user = service.authenticate(session, email, password)
    except AuthError as exc:
        return _page(request, "auth/login.html", {"next": next, "error": str(exc)}, 400)
    return _signed_in_redirect(session, request, user, _signed_in_target(session, user, next))


@router.get("/signup")
def signup_page(request: Request, next: str = "/jobs", user: User | None = Depends(current_user)):
    if user is not None and not user.is_guest:
        return _redirect(safe_next(next, "/jobs"))
    return _page(request, "auth/signup.html", {"next": safe_next(next, "/jobs")})


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    response = _redirect("/?" + urlencode({"msg": "You've signed out."}))
    end_session(session, request, response)
    return response


# ---- email verification ------------------------------------------------------------

@router.get("/verify-email")
def verify_email(request: Request, token: str = "", session: Session = Depends(get_session),
                 user: User | None = Depends(current_user)):
    try:
        verified = service.verify_email(session, token)
    except AuthError as exc:
        return _page(request, "auth/notice.html", {"title": "Link not valid", "body": str(exc),
                                                   "link": ("/login", "Go to sign in")}, 400)
    if user is not None and user.id == verified.id:
        return _redirect("/jobs?" + urlencode({"msg": "Email confirmed -- welcome!"}))
    return _redirect("/login?" + urlencode({"msg": "Email confirmed. Sign in to get started."}))


@router.get("/verify-email/pending")
def verify_pending(request: Request, user: User = Depends(require_viewer)):
    if user.is_guest or user.email_verified:
        return _redirect("/")
    return _page(request, "auth/verify_pending.html", {"user": user})


@router.post("/verify-email/resend")
def resend_verification(request: Request, user: User = Depends(require_viewer),
                        session: Session = Depends(get_session)):
    if user.is_guest or user.email_verified:
        return _redirect("/")
    if email_limiter.allow(f"{client_ip(request)}|{user.id}"):
        service.send_verification(session, user)
        session.commit()
    return _page(request, "auth/verify_pending.html", {"user": user, "message": "We've sent a new link."})


# ---- password reset ------------------------------------------------------------------

@router.get("/forgot-password")
def forgot_page(request: Request):
    return _page(request, "auth/forgot.html")


@router.post("/forgot-password")
def forgot(request: Request, email: str = Form(""), session: Session = Depends(get_session)):
    if email_limiter.allow(f"{client_ip(request)}|{service.normalize_email(email)}"):
        service.request_password_reset(session, email)
    return _page(request, "auth/notice.html", {
        "title": "Check your email",
        "body": "If there's an account for that address, we've sent a link to reset its password. "
                "It expires in 1 hour.",
        "link": ("/login", "Back to sign in"),
    })


@router.get("/reset-password")
def reset_page(request: Request, token: str = ""):
    return _page(request, "auth/reset.html", {"token": token})


@router.post("/reset-password")
def reset(request: Request, token: str = Form(""), password: str = Form(""), confirm_password: str = Form(""),
          session: Session = Depends(get_session)):
    if password != confirm_password:
        return _page(request, "auth/reset.html", {"token": token, "error": "The passwords don't match."}, 400)
    try:
        service.reset_password(session, token, password)
    except AuthError as exc:
        return _page(request, "auth/reset.html", {"token": token, "error": str(exc)}, 400)
    return _redirect("/login?" + urlencode({"msg": "Password changed. Sign in with your new password."}))


# ---- Google / Facebook --------------------------------------------------------------------

@router.get("/auth/{provider}/start")
def oauth_start(request: Request, provider: str, next: str = "/", session: Session = Depends(get_session)):
    try:
        url, state = oauth.authorization_url(session, provider, safe_next(next))
    except AuthError as exc:
        return _redirect("/login?" + urlencode({"error": str(exc)}))
    response = _redirect(url)
    response.set_cookie(oauth.STATE_COOKIE, state, max_age=int(oauth.STATE_TTL.total_seconds()), httponly=True,
                        samesite="lax", secure=AuthSettings().secure_cookies, path="/auth")
    return response


@router.get("/auth/{provider}/callback")
def oauth_callback(request: Request, provider: str, code: str | None = None, state: str | None = None,
                   session: Session = Depends(get_session)):
    try:
        profile, next_path = oauth.complete(session, provider, code=code, state=state,
                                            cookie_state=request.cookies.get(oauth.STATE_COOKIE))
        user = service.social_login(session, profile)
    except AuthError as exc:
        return _redirect("/login?" + urlencode({"error": str(exc)}))
    response = _signed_in_redirect(session, request, user, _signed_in_target(session, user, next_path), provider)
    response.delete_cookie(oauth.STATE_COOKIE, path="/auth")
    return response


# ---- account (profile) page ------------------------------------------------------------------

def _account(request: Request, session: Session, user: User, *, message: str | None = None,
             errors: dict | None = None, status_code: int = 200):
    sessions = session.query(AuthSession).filter(AuthSession.user_id == user.id).count()
    return _page(request, "account.html", {
        "user": user, "message": message, "errors": errors or {}, "other_sessions": max(sessions - 1, 0),
        "providers": sorted({i.provider for i in user.identities}),
    }, status_code)


@router.get("/account")
def account_page(request: Request, msg: str | None = None, user: User = Depends(require_member),
                 session: Session = Depends(get_session)):
    return _account(request, session, user, message=msg)


@router.post("/account/profile")
def update_profile(request: Request, first_name: str = Form(""), last_name: str = Form(""), phone: str = Form(""),
                   user: User = Depends(require_member), session: Session = Depends(get_session)):
    errors = {k: v for k, v in (("first_name", service.name_problem(first_name, "first name")),
                                ("last_name", service.name_problem(last_name, "last name"))) if v}
    try:
        normalized_phone = service.normalize_phone(phone)
    except AuthError as exc:
        errors["phone"] = str(exc)
    if errors:
        return _account(request, session, user, errors=errors, status_code=400)
    user.first_name, user.last_name, user.phone = first_name.strip(), last_name.strip(), normalized_phone
    session.commit()
    return _redirect("/account?" + urlencode({"msg": "Profile saved."}))


@router.post("/account/email")
def change_email(request: Request, email: str = Form(""), user: User = Depends(require_member),
                 session: Session = Depends(get_session)):
    problem = service.email_problem(email)
    if problem:
        return _account(request, session, user, errors={"email": problem}, status_code=400)
    new = service.normalize_email(email)
    if new == user.email:
        return _redirect("/account")
    if email_limiter.allow(f"{client_ip(request)}|{user.id}"):
        service.send_verification(session, user, new_email=new)
        session.commit()
    return _redirect("/account?" + urlencode({
        "msg": f"We've sent a confirmation link to {new}. Your email changes once you open it."}))


@router.post("/account/password")
def change_password(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                    confirm_password: str = Form(""), user: User = Depends(require_member),
                    session: Session = Depends(get_session)):
    if new_password != confirm_password:
        return _account(request, session, user, errors={"password": "The new passwords don't match."}, status_code=400)
    try:
        service.change_password(session, user, current_password, new_password)
    except AuthError as exc:
        return _account(request, session, user, errors={"password": str(exc)}, status_code=400)
    end_other_sessions(session, request, user.id)
    return _redirect("/account?" + urlencode({"msg": "Password updated. Other devices have been signed out."}))


@router.post("/account/sessions/revoke")
def revoke_sessions(request: Request, user: User = Depends(require_member), session: Session = Depends(get_session)):
    count = end_other_sessions(session, request, user.id)
    return _redirect("/account?" + urlencode({"msg": f"Signed out {count} other device{'s' if count != 1 else ''}."}))


@router.post("/account/delete")
def delete_account(request: Request, confirm_email: str = Form(""), user: User = Depends(require_member),
                   session: Session = Depends(get_session)):
    if service.normalize_email(confirm_email) != user.email:
        return _account(request, session, user, errors={"delete": "Type your email address exactly to confirm."},
                        status_code=400)
    if rbac.is_last_super_admin(session, user.id):
        return _account(request, session, user, status_code=400, errors={
            "delete": "You're the only Super Admin. Make someone else a Super Admin before deleting your account."})
    user_id = user.id
    response = _redirect("/login?" + urlencode({"msg": "Your account and all its data have been deleted."}))
    end_session(session, request, response)
    factory = sessionmaker(bind=session.get_bind())  # the request's own database
    session.close()
    service.delete_account(factory, user_id)
    return response
