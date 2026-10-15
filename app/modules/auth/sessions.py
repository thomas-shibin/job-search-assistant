"""Signed-in browsers: a random token in an HttpOnly cookie, its hash in
`auth_sessions`. Server-side, so signing out (or changing a password) ends a
session immediately.

How long a session lasts is a platform setting (`sessions`, on the admin
Settings page): an inactivity timeout plus a maximum length, stricter for
anyone who can open the admin dashboard. Both are checked on every request
against the user's current permissions, so granting the admin role tightens
an open session straight away. Guests always get 24 hours."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config.models import AppSetting
from app.core.config.schema import SessionsConfig
from app.modules.access import rbac
from app.modules.auth.models import AuthSession, User
from app.modules.auth.settings import AuthSettings
from app.modules.auth.tokens import hash_secret, new_secret, utcnow
from app.modules.usage.models import ActivityEvent, ActivityKind

logger = logging.getLogger(__name__)

COOKIE = "jsa_session"
GUEST_TTL = timedelta(hours=24)
_TOUCH_EVERY = timedelta(minutes=5)
ENDED_STATE = "session_ended"  # request.state attribute: why a session just ended ("idle" or "expired")


@dataclass(frozen=True)
class SessionPolicy:
    idle: timedelta | None  # None: no inactivity timeout
    lifetime: timedelta

    @property
    def touch_every(self) -> timedelta:
        """How stale `last_seen_at` may get: a tenth of the idle timeout, so
        nobody is signed out noticeably early, at most every 5 minutes."""
        return min(_TOUCH_EVERY, self.idle / 10) if self.idle else _TOUCH_EVERY


def session_settings(session: Session) -> SessionsConfig:
    row = session.get(AppSetting, "sessions")
    try:
        return SessionsConfig.model_validate(row.value if row is not None and isinstance(row.value, dict) else {})
    except ValidationError:
        logger.warning("Session settings are invalid; using the defaults")
        return SessionsConfig()


def policy_for(session: Session, user: User) -> SessionPolicy:
    if user.is_guest:
        return SessionPolicy(idle=None, lifetime=GUEST_TTL)
    settings = session_settings(session)
    rbac.permissions_of(user, session)  # resolve from this database, once per request
    if user.can("admin.dashboard.view"):
        return SessionPolicy(idle=timedelta(minutes=settings.admin_idle_minutes),
                             lifetime=timedelta(hours=settings.admin_max_hours))
    return SessionPolicy(idle=timedelta(days=settings.member_idle_days),
                         lifetime=timedelta(days=settings.member_max_days))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def start_session(session: Session, request: Request, response: Response, user: User, method: str = "password") -> None:
    """Sign `user` in on this browser (always a fresh token: no session fixation)."""
    session.add(ActivityEvent(user_id=user.id, kind=ActivityKind.SIGN_IN, summary=method.title()))
    end_session(session, request, response)
    raw = new_secret()
    ttl = policy_for(session, user).lifetime
    now = utcnow()
    session.add(AuthSession(
        user_id=user.id, token_hash=hash_secret(raw), created_at=now, expires_at=now + ttl, last_seen_at=now,
        ip=client_ip(request)[:64], user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    user.last_login_at = utcnow()
    session.commit()
    response.set_cookie(
        COOKIE, raw, max_age=int(ttl.total_seconds()), httponly=True, samesite="lax",
        secure=AuthSettings().secure_cookies, path="/",
    )


def _end_reason(row: AuthSession, policy: SessionPolicy, now) -> str | None:
    """"expired" past the maximum length (or the expiry set at sign-in, which
    a later, longer setting doesn't extend), "idle" past the inactivity timeout."""
    if row.expires_at < now or (row.created_at is not None and now - row.created_at > policy.lifetime):
        return "expired"
    if policy.idle is not None and now - (row.last_seen_at or row.created_at or now) > policy.idle:
        return "idle"
    return None


def user_for_request(session: Session, request: Request) -> User | None:
    """The signed-in user, or None. A session past its time limits is deleted,
    and why is left on `request.state` for the sign-in page's message."""
    raw = request.cookies.get(COOKIE)
    if not raw or len(raw) > 200:
        return None
    row = session.query(AuthSession).filter(AuthSession.token_hash == hash_secret(raw)).one_or_none()
    now = utcnow()
    if row is None:
        return None
    if row.expires_at < now:
        setattr(request.state, ENDED_STATE, "expired")
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    policy = policy_for(session, user)
    reason = _end_reason(row, policy, now)
    if reason is not None:
        session.delete(row)
        session.commit()
        logger.info("Session ended (%s) for user %s", reason, user.id)
        setattr(request.state, ENDED_STATE, reason)
        return None
    if row.last_seen_at is None or now - row.last_seen_at > policy.touch_every:
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
