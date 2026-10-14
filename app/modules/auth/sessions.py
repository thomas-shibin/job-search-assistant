"""Signed-in browsers: a random token in an HttpOnly cookie, its hash in
`auth_sessions`. Server-side, so signing out (or changing a password) ends a
session immediately."""
from __future__ import annotations

from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy.orm import Session

from app.modules.auth.settings import AuthSettings
from app.modules.auth.tokens import hash_secret, new_secret, utcnow
from app.modules.auth.models import AuthSession, User

COOKIE = "jsa_session"
MEMBER_TTL = timedelta(days=14)
GUEST_TTL = timedelta(hours=24)
_TOUCH_EVERY = timedelta(minutes=5)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def start_session(session: Session, request: Request, response: Response, user: User, method: str = "password") -> None:
    """Sign `user` in on this browser (always a fresh token: no session fixation)."""
    from app.modules.usage.models import ActivityEvent, ActivityKind

    session.add(ActivityEvent(user_id=user.id, kind=ActivityKind.SIGN_IN, summary=method.title()))
    end_session(session, request, response)
    raw = new_secret()
    ttl = GUEST_TTL if user.is_guest else MEMBER_TTL
    session.add(AuthSession(
        user_id=user.id, token_hash=hash_secret(raw), expires_at=utcnow() + ttl, last_seen_at=utcnow(),
        ip=client_ip(request)[:64], user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    user.last_login_at = utcnow()
    session.commit()
    response.set_cookie(
        COOKIE, raw, max_age=int(ttl.total_seconds()), httponly=True, samesite="lax",
        secure=AuthSettings().secure_cookies, path="/",
    )


def user_for_request(session: Session, request: Request) -> User | None:
    raw = request.cookies.get(COOKIE)
    if not raw or len(raw) > 200:
        return None
    row = session.query(AuthSession).filter(AuthSession.token_hash == hash_secret(raw)).one_or_none()
    now = utcnow()
    if row is None or row.expires_at < now:
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    if row.last_seen_at is None or now - row.last_seen_at > _TOUCH_EVERY:
        row.last_seen_at = now
        session.commit()
    return user


def end_session(session: Session, request: Request, response: Response) -> None:
    raw = request.cookies.get(COOKIE)
    if raw:
        session.query(AuthSession).filter(AuthSession.token_hash == hash_secret(raw)).delete()
        session.commit()
    response.delete_cookie(COOKIE, path="/")


def end_other_sessions(session: Session, request: Request, user_id: int) -> int:
    """Sign out every other browser (after a password change, or on request)."""
    current = hash_secret(request.cookies.get(COOKIE, ""))
    count = session.query(AuthSession).filter(
        AuthSession.user_id == user_id, AuthSession.token_hash != current
    ).delete()
    session.commit()
    return count
