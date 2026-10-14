"""Random secrets handed to a browser or an email, stored only as SHA-256
hashes: a leaked database can't be turned into working links or sessions."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.modules.auth.models import AuthToken, TokenPurpose


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_token(session: Session, purpose: TokenPurpose, *, user_id: int | None, ttl: timedelta,
                payload: dict | None = None) -> str:
    """A new single-use token; returns the raw value (shown once, never stored)."""
    raw = new_secret()
    session.add(AuthToken(user_id=user_id, purpose=purpose.value, token_hash=hash_secret(raw),
                          payload=payload, expires_at=utcnow() + ttl))
    session.flush()
    return raw


def consume_token(session: Session, purpose: TokenPurpose, raw: str | None) -> AuthToken | None:
    """The token, marked used -- or None if unknown, expired, used or for another purpose."""
    if not raw or len(raw) > 200:
        return None
    token = session.query(AuthToken).filter(AuthToken.token_hash == hash_secret(raw)).one_or_none()
    if token is None or token.purpose != purpose.value or token.used_at is not None or token.expires_at < utcnow():
        return None
    token.used_at = utcnow()
    session.flush()
    return token


def revoke_tokens(session: Session, user_id: int, purpose: TokenPurpose) -> None:
    """Invalidate a user's outstanding tokens of one kind (e.g. older reset links)."""
    session.query(AuthToken).filter(
        AuthToken.user_id == user_id, AuthToken.purpose == purpose.value, AuthToken.used_at.is_(None)
    ).update({AuthToken.used_at: utcnow()})
