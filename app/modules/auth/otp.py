"""Six-digit email codes, for signing up (proving the address) and signing in
without a password.

- A code is random (`secrets`), lives 60 seconds, and only its HMAC is stored
  -- keyed by `APP_SECRET_KEY`, or a per-process random key when that isn't
  set (codes are so short-lived that a restart invalidating them is harmless).
- Issuing a code retires the address's earlier ones; a new code can't be
  requested again for 30 seconds, and at most 6 an hour per address.
- 5 wrong guesses retire a code. Expiry and attempts are checked here, on the
  server; the code itself is never returned by an API or written to a log.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.modules.auth.tokens import utcnow
from app.modules.auth.models import EmailCode

CODE_TTL = timedelta(seconds=60)
RESEND_AFTER = timedelta(seconds=30)
MAX_ATTEMPTS = 5
MAX_PER_HOUR = 6
REGISTER, LOGIN = "register", "login"

_KEY = (os.environ.get("APP_SECRET_KEY") or "").encode() or secrets.token_bytes(32)


class CodeError(Exception):
    """Why a code couldn't be issued or accepted. `reason` is one of
    invalid, expired, too_many_attempts, cooldown, rate_limited."""

    MESSAGES = {
        "invalid": "That code isn't right. Check the email and try again.",
        "expired": "That code has expired. Request a new one.",
        "too_many_attempts": "Too many incorrect attempts. Request a new code.",
        "cooldown": "Please wait a few seconds before requesting another code.",
        "rate_limited": "Too many codes requested for this email. Try again later.",
    }

    def __init__(self, reason: str, *, retry_after: int | None = None, attempts_left: int | None = None) -> None:
        super().__init__(self.MESSAGES[reason])
        self.reason = reason
        self.retry_after = retry_after
        self.attempts_left = attempts_left


def _digest(email: str, code: str) -> str:
    return hmac.new(_KEY, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def issue(session: Session, email: str, purpose: str) -> str:
    """A new code for `email` (already normalized); returns it to be emailed."""
    now = utcnow()
    latest = session.query(EmailCode).filter(EmailCode.email == email).order_by(EmailCode.created_at.desc()).first()
    if latest is not None and now - latest.created_at < RESEND_AFTER:
        raise CodeError("cooldown", retry_after=math.ceil((latest.created_at + RESEND_AFTER - now).total_seconds()))
    recent = session.query(func.count(EmailCode.id)).filter(
        EmailCode.email == email, EmailCode.created_at > now - timedelta(hours=1)).scalar() or 0
    if recent >= MAX_PER_HOUR:
        raise CodeError("rate_limited", retry_after=3600)
    session.query(EmailCode).filter(EmailCode.email == email, EmailCode.consumed_at.is_(None)).update(
        {EmailCode.consumed_at: now}, synchronize_session=False)
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(EmailCode(email=email, purpose=purpose, code_hash=_digest(email, code), expires_at=now + CODE_TTL,
                          attempt_count=0, created_at=now))
    session.commit()
    return code


def withdraw(session: Session, email: str) -> None:
    """Retire the latest code (it couldn't be delivered), so it doesn't hold up a retry."""
    latest = session.query(EmailCode).filter(EmailCode.email == email).order_by(EmailCode.created_at.desc()).first()
    if latest is not None:
        session.delete(latest)
        session.commit()


def verify(session: Session, email: str, code: str) -> EmailCode:
    """The (now used) code row if `code` is right; otherwise raises `CodeError`."""
    now = utcnow()
    row = session.query(EmailCode).filter(EmailCode.email == email, EmailCode.consumed_at.is_(None)).order_by(
        EmailCode.created_at.desc()).first()
    if row is None:
        raise CodeError("expired")
    if row.attempt_count >= MAX_ATTEMPTS:
        raise CodeError("too_many_attempts")
    if row.expires_at <= now:
        raise CodeError("expired")
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()) or not hmac.compare_digest(row.code_hash, _digest(email, code)):
        row.attempt_count += 1
        session.commit()
        if row.attempt_count >= MAX_ATTEMPTS:
            raise CodeError("too_many_attempts")
        raise CodeError("invalid", attempts_left=MAX_ATTEMPTS - row.attempt_count)
    row.consumed_at = now
    session.commit()
    return row


def purge(session: Session) -> None:
    session.query(EmailCode).filter(EmailCode.created_at < utcnow() - timedelta(days=1)).delete(
        synchronize_session=False)
