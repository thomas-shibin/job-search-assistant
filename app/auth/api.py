"""The sign-in / sign-up API behind the "Welcome" dialog and the /login and
/signup pages (app/ui/static/auth.js). JSON in, JSON out; the session itself
is the same HttpOnly cookie the rest of the app uses (app/auth/sessions.py).

The flow starts from an email address only, and never says whether it has an
account until the visitor has proven they own it:

1. ``POST /api/auth/register/start {email}`` -- emails a six-digit code: a
   sign-in code for an existing account, a verification code for a new one.
   The response is the same either way.
2. ``POST /api/auth/verify-email {email, code}`` -- an existing account is
   signed in; a new address gets a short-lived registration ticket (HttpOnly
   cookie) and is asked for its name and password.
3. ``POST /api/auth/register/complete {first_name, last_name, password,
   confirm_password}`` -- creates the account (email already verified) and
   signs it in.

Or ``POST /api/auth/login {email, password}`` for password sign-in. Errors
are ``{"error": {"code", "message", ...}}`` with a fitting HTTP status.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import email as mailer
from app.auth import otp, rbac, service
from app.auth.deps import current_user
from app.auth.passwords import MIN_LENGTH, PASSWORD_RULES
from app.auth.ratelimit import RateLimiter, email_limiter, login_limiter
from app.auth.service import AuthError, normalize_email
from app.auth.sessions import client_ip, end_session, start_session
from app.auth.settings import AuthSettings
from app.database.session import get_session
from app.models.user import User
from app.profile import service as profiles
from app.ui.templating import safe_next
from app.utils.logging import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")

REG_COOKIE = "jsa_reg"
code_limiter = RateLimiter(limit=20, window_seconds=3600)  # code requests per IP


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def _signed_in(session: Session, request: Request, user: User, next_path: str | None, method: str,
               step: str = "done") -> JSONResponse:
    redirect = profiles.after_sign_in(session, user, safe_next(next_path, "/jobs"))
    response = JSONResponse({"step": step, "redirect": redirect})
    start_session(session, request, response, user, method)
    response.delete_cookie(REG_COOKIE, path="/api/auth")
    return response


class EmailIn(BaseModel):
    email: str = ""
    next: str | None = None


class CodeIn(BaseModel):
    email: str = ""
    code: str = ""
    next: str | None = None


class DetailsIn(BaseModel):
    first_name: str = ""
    last_name: str = ""
    password: str = ""
    confirm_password: str = ""
    next: str | None = None


class LoginIn(BaseModel):
    email: str = ""
    password: str = ""
    next: str | None = None


class ResetIn(BaseModel):
    token: str = ""
    password: str = ""
    confirm_password: str = ""


def _send_code(request: Request, session: Session, raw_email: str) -> JSONResponse:
    problem = service.email_problem(raw_email)
    if problem:
        return _error(400, "invalid_email", problem)
    email = normalize_email(raw_email)
    if not code_limiter.allow(client_ip(request)):
        return _error(429, "rate_limited", "Too many requests from your network. Try again later.", retry_after=600)
    account = service.account_for_email(session, email)
    if account is not None and not account.is_active:
        # Suspended: answer exactly as for anyone else, but send nothing.
        return JSONResponse({"step": "verify", "email": email, "expires_in": int(otp.CODE_TTL.total_seconds()),
                             "resend_in": int(otp.RESEND_AFTER.total_seconds())})
    try:
        code = otp.issue(session, email, otp.LOGIN if account is not None else otp.REGISTER)
    except otp.CodeError as exc:
        return _error(429, exc.reason, str(exc), retry_after=exc.retry_after)
    try:
        mailer.send_code_email(email, code, new_account=account is None)
    except mailer.EmailDeliveryError:
        otp.withdraw(session, email)
        return _error(503, "email_failed", "We couldn't send the email right now. Please try again in a minute.")
    log_event(logger, "EMAIL_CODE_SENT", purpose="login" if account else "register")
    return JSONResponse({"step": "verify", "email": email, "expires_in": int(otp.CODE_TTL.total_seconds()),
                         "resend_in": int(otp.RESEND_AFTER.total_seconds())})


@router.post("/register/start")
def start(body: EmailIn, request: Request, session: Session = Depends(get_session)):
    """Step 1, for new and existing users alike: email a code."""
    return _send_code(request, session, body.email)


@router.post("/resend-otp")
def resend(body: EmailIn, request: Request, session: Session = Depends(get_session)):
    return _send_code(request, session, body.email)


@router.post("/verify-email")
def verify(body: CodeIn, request: Request, session: Session = Depends(get_session)):
    """Step 2: check the code. Signs an existing account in; starts registration otherwise."""
    if service.email_problem(body.email):
        return _error(400, "invalid_email", "Enter a valid email address.")
    email = normalize_email(body.email)
    try:
        row = otp.verify(session, email, body.code)
    except otp.CodeError as exc:
        status = 429 if exc.reason == "too_many_attempts" else 400
        return _error(status, exc.reason, str(exc), attempts_left=exc.attempts_left)
    if row.purpose == otp.LOGIN:
        user = service.sign_in_with_code(session, email)
        if user is None:
            return _error(400, "expired", otp.CodeError.MESSAGES["expired"])
        return _signed_in(session, request, user, body.next, "email_code")
    if service.account_for_email(session, email) is not None:  # registered meanwhile
        user = service.sign_in_with_code(session, email)
        return _signed_in(session, request, user, body.next, "email_code")
    ticket = service.start_registration(session, email)
    response = JSONResponse({"step": "details", "email": email, "password_min": MIN_LENGTH,
                             "password_rules": PASSWORD_RULES})
    response.set_cookie(REG_COOKIE, ticket, max_age=int(service.REGISTRATION_TTL.total_seconds()), httponly=True,
                        samesite="strict", secure=AuthSettings().secure_cookies, path="/api/auth")
    return response


@router.post("/register/complete")
def complete(body: DetailsIn, request: Request, session: Session = Depends(get_session)):
    """Step 3: name and password -> the account, signed in."""
    ticket = request.cookies.get(REG_COOKIE)
    problems = service.account_problems(first_name=body.first_name, last_name=body.last_name,
                                        email=service.registration_email(session, ticket),
                                        password=body.password, confirm_password=body.confirm_password)
    if problems:
        return _error(400, "invalid_details", "Check the highlighted fields.", fields=problems.errors)
    try:
        user = service.complete_registration(session, ticket, first_name=body.first_name, last_name=body.last_name,
                                             password=body.password, confirm_password=body.confirm_password)
    except AuthError as exc:
        return _error(400, "registration_expired", str(exc))
    return _signed_in(session, request, user, body.next, "email_code", step="created")


@router.post("/login")
def login(body: LoginIn, request: Request, session: Session = Depends(get_session)):
    if not login_limiter.allow(client_ip(request)):
        return _error(429, "rate_limited", "Too many attempts from your network. Wait a few minutes and try again.")
    try:
        user = service.authenticate(session, body.email, body.password)
    except AuthError as exc:
        return _error(401, "invalid_credentials", str(exc))
    return _signed_in(session, request, user, body.next, "password")


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)):
    response = JSONResponse({"step": "signed_out"})
    end_session(session, request, response)
    return response


@router.post("/forgot-password")
def forgot_password(body: EmailIn, request: Request, session: Session = Depends(get_session)):
    """Always the same answer, whether or not the address has an account."""
    if service.email_problem(body.email):
        return _error(400, "invalid_email", "Enter a valid email address.")
    if email_limiter.allow(f"{client_ip(request)}|{normalize_email(body.email)}"):
        service.request_password_reset(session, body.email)
    return JSONResponse({"step": "reset_sent"})


@router.post("/reset-password")
def reset_password(body: ResetIn, session: Session = Depends(get_session)):
    if body.password != body.confirm_password:
        return _error(400, "invalid_details", "The passwords don't match.",
                      fields={"confirm_password": "The passwords don't match."})
    try:
        service.reset_password(session, body.token, body.password)
    except AuthError as exc:
        return _error(400, "invalid_reset", str(exc))
    return JSONResponse({"step": "password_reset"})


@router.get("/me")
def me(request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    if user is None or user.is_guest:
        return JSONResponse({"authenticated": False})
    percent = None
    if user.is_member:
        from sqlalchemy.orm import sessionmaker

        from app.tenancy import tenant_session

        with tenant_session(sessionmaker(bind=session.get_bind()), user.id) as own:
            percent = profiles.completion(own, user).percent
    return JSONResponse({
        "authenticated": True, "id": user.id, "email": user.email, "first_name": user.first_name,
        "last_name": user.last_name, "account_type": user.account_type, "email_verified": user.email_verified,
        "is_admin": user.is_admin, "profile_completion": percent,
        # Read fresh from the database on every call: use these to show or hide
        # things; the server still checks each request itself.
        "roles": list(user.roles), "permissions": sorted(user.permissions),
        "features": sorted(key for key in rbac.FEATURES if rbac.can_use(session, user, key)),
    })
