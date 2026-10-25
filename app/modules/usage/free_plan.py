"""The free plan: lifetime allowances, counted per person rather than per account,
of the two things that cost us per use:
- resume actions (match analyses, tailored resumes, Quick Tailor, Canadian job
  titles), which call the paid analysis service;
- job-board searches, which spend the job boards' request budget. Once they're
  used up, searches still run, on company careers-site jobs only.

How one person's accounts are found
-----------------------------------
Each account collects *marks*: keyed one-way hashes (HMAC-SHA256, with a
random key made once in config/free_plan.key) of
- its email addresses (sign-up, social sign-ins, and any in its resumes), with
  Gmail dots and "+tags" removed, so a.b+x@gmail.com is ab@gmail.com;
- phone numbers (the account's and any in its resumes), as the last 10 digits;
- each resume's text (the same text, whatever the file);
- the person's name (account and resume), words in any order.

Two accounts with the same email, phone or resume text are the same person:
the link is `shared` at once and the accounts share one allowance. A name alone
is weak (many people share one), so it only flags the pair for an admin
(`review`). An admin can confirm a flag (shared) or dismiss any link
(different people: never shared again for that pair).

Marks are collected when an account uploads a resume or uses a resume action,
which is when the allowance matters. Deleting an account keeps its marks and
its count (see free_plan_models.py), so deleting and signing up again doesn't
start over.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.paths import DEFAULT_CONFIG_PATH
from app.modules.auth.models import OAuthIdentity, User
from app.modules.auth.settings import AuthSettings
from app.modules.usage.free_plan_models import AccountLink, FreeAccount, PersonMark
from app.modules.usage.models import ActivityEvent, ActivityKind

logger = logging.getLogger(__name__)

EMAIL, PHONE, RESUME, NAME = "email", "phone", "resume", "name"
AI, SEARCH = "ai", "search"  # the two allowances
_USED = {AI: "ai_used", SEARCH: "searches_used"}
_OVERRIDE = {AI: "ai_total_limit", SEARCH: "search_total_limit"}
STRONG = (EMAIL, PHONE, RESUME)
SHARED, REVIEW, CONFIRMED, DISMISSED = "shared", "review", "confirmed", "dismissed"
SHARING = (SHARED, CONFIRMED)
REASON_LABELS = {EMAIL: "same email address", PHONE: "same phone number", RESUME: "same resume",
                 NAME: "same name"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_GMAIL = {"gmail.com", "googlemail.com"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- marks ------------------------------------------------------------------------

# The key lives in config/ (backed up with it, encrypted), not in the database, so a
# copy of the database alone can't be used to test guesses against the marks. Made
# once; changing or losing it means old marks no longer match new ones.
KEY_FILE = DEFAULT_CONFIG_PATH.parent / "free_plan.key"
_cached_key: bytes | None = None


def _key() -> bytes:
    global _cached_key
    if _cached_key is None:
        _cached_key = _load_key(KEY_FILE)
    return _cached_key


def _load_key(path: Path) -> bytes:
    try:
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(secrets.token_hex(32) + "\n")
            logger.info("Made the free-plan key in %s", path)
    except FileExistsError:
        pass  # another worker made it first
    except OSError:
        logger.warning("Can't write %s; free-plan marks use a key from APP_SECRET_KEY instead", path)
    try:
        value = path.read_text().strip()
        if len(value) >= 32:
            return bytes.fromhex(value) if all(c in "0123456789abcdef" for c in value) else value.encode()
    except OSError:
        pass
    secret = AuthSettings().app_secret_key or "hirlumo-free-plan-marks"
    return hashlib.sha256(f"free-plan-marks:{secret}".encode()).digest()


def digest(kind: str, value: str) -> str:
    return hmac.new(_key(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def norm_email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}" if local and "." in domain else None


def norm_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else None


def norm_name(value: str | None) -> str | None:
    text = unicodedata.normalize("NFKD", value or "")
    words = re.findall(r"[a-z]+", "".join(c for c in text if not unicodedata.combining(c)).lower())
    words = [w for w in words if len(w) > 1]
    return " ".join(sorted(words)) if len(words) >= 2 else None  # one word is far too common


def account_marks(user: User, identity_emails: list[str] = ()) -> set[tuple[str, str]]:
    marks = {(EMAIL, e) for e in map(norm_email, [user.email, *identity_emails]) if e}
    if phone := norm_phone(user.phone):
        marks.add((PHONE, phone))
    if name := norm_name(user.full_name):
        marks.add((NAME, name))
    return marks


def resume_marks(text: str, text_hash: str | None = None, full_name: str | None = None) -> set[tuple[str, str]]:
    marks = {(EMAIL, e) for e in map(norm_email, _EMAIL_RE.findall(text or "")) if e}
    marks |= {(PHONE, p) for p in map(norm_phone, _PHONE_RE.findall(text or "")) if p}
    words = " ".join(re.findall(r"\w+", (text or "").lower()))
    if text_hash or words:
        marks.add((RESUME, text_hash or hashlib.sha256(words.encode()).hexdigest()))
    if name := norm_name(full_name):
        marks.add((NAME, name))
    return marks


# ---- accounts ---------------------------------------------------------------------

def account_for(session: Session, user: User) -> FreeAccount:
    """The account's row, made on first use. An account from before the free plan
    starts with the resume actions and job-board searches its activity log still holds."""
    row = session.query(FreeAccount).filter(FreeAccount.user_id == user.id).one_or_none()
    if row is not None:
        return row
    used = session.query(ActivityEvent).filter(ActivityEvent.user_id == user.id,
                                                ActivityEvent.kind.in_(ActivityKind.AI_KINDS)).count()
    searches = session.query(ActivityEvent).filter(ActivityEvent.user_id == user.id,
                                                    ActivityEvent.kind == ActivityKind.SEARCH,
                                                    ActivityEvent.provider_calls > 0).count()
    row = FreeAccount(user_id=user.id, ai_used=used, searches_used=searches)
    session.add(row)
    session.flush()
    emails = [e for (e,) in session.query(OAuthIdentity.email).filter(OAuthIdentity.user_id == user.id) if e]
    _add_marks(session, row, account_marks(user, emails))
    return row


def note_account(session: Session, user: User) -> None:
    """Collect the account's own marks (its emails, phone and name) again, after they change."""
    row = account_for(session, user)
    emails = [e for (e,) in session.query(OAuthIdentity.email).filter(OAuthIdentity.user_id == user.id) if e]
    _add_marks(session, row, account_marks(user, emails))
    session.commit()


def note_resume(session: Session, user: User, text: str, *, text_hash: str | None = None,
                full_name: str | None = None) -> None:
    """Collect a resume's marks. Never lets a failure here stop the upload."""
    try:
        row = account_for(session, user)
        _add_marks(session, row, resume_marks(text, text_hash, full_name))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Couldn't note resume marks for user_id=%s", user.id)


def _add_marks(session: Session, row: FreeAccount, marks: set[tuple[str, str]]) -> None:
    have = {(m.kind, m.digest) for m in session.query(PersonMark).filter(PersonMark.account_id == row.id)}
    for kind, value in marks:
        d = digest(kind, value)
        if (kind, d) in have:
            continue
        session.add(PersonMark(account_id=row.id, kind=kind, digest=d))
        have.add((kind, d))
        others = {a for (a,) in session.query(PersonMark.account_id).filter(
            PersonMark.kind == kind, PersonMark.digest == d, PersonMark.account_id != row.id)}
        for other in others:
            _link(session, row.id, other, kind)
    session.flush()


def _link(session: Session, one: int, two: int, kind: str) -> None:
    a, b = sorted((one, two))
    link = session.query(AccountLink).filter(AccountLink.account_a == a, AccountLink.account_b == b).one_or_none()
    if link is None:
        link = AccountLink(account_a=a, account_b=b, reasons=[], status=REVIEW)
        session.add(link)
        session.flush()  # a second shared mark in this upload must find it, not make another
    if kind not in (link.reasons or []):
        link.reasons = [*(link.reasons or []), kind]
        link.updated_at = _utcnow()
    if kind in STRONG and link.status == REVIEW:
        link.status = SHARED
    if link.status != DISMISSED:
        logger.info("Free plan: accounts %s and %s linked (%s, %s)", a, b, kind, link.status)


def retire(session: Session, user_id: int) -> None:
    """The account is being deleted: keep its count and marks, without the user id."""
    session.query(FreeAccount).filter(FreeAccount.user_id == user_id).update(
        {FreeAccount.user_id: None, FreeAccount.deleted_at: _utcnow()}, synchronize_session=False)


# ---- the allowance ----------------------------------------------------------------

def group(session: Session, row: FreeAccount) -> list[FreeAccount]:
    """This account and every account linked to it, directly or through others."""
    seen, queue = {row.id}, deque([row.id])
    while queue:
        current = queue.popleft()
        for link in session.query(AccountLink).filter(
                AccountLink.status.in_(SHARING),
                or_(AccountLink.account_a == current, AccountLink.account_b == current)):
            other = link.account_b if link.account_a == current else link.account_a
            if other not in seen:
                seen.add(other)
                queue.append(other)
    return session.query(FreeAccount).filter(FreeAccount.id.in_(seen)).order_by(FreeAccount.id).all()


@dataclass
class Standing:
    limit: int | None  # None: unlimited (admins)
    used: int
    accounts: int = 1  # accounts sharing this allowance, deleted ones included
    shared_with: list[FreeAccount] = field(default_factory=list)

    @property
    def unlimited(self) -> bool:
        return self.limit is None

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit


def limit_for(user: User, default: int, kind: str = AI) -> int | None:
    if user.is_admin:
        return None
    override = getattr(user, _OVERRIDE[kind])
    return override if override is not None else default


def standing(session: Session, user: User, default: int, kind: str = AI) -> Standing:
    row = account_for(session, user)
    members = group(session, row)
    return Standing(limit=limit_for(user, default, kind), used=sum(getattr(m, _USED[kind]) for m in members),
                    accounts=len(members), shared_with=[m for m in members if m.id != row.id])


def spend(session: Session, user: User, kind: str = AI) -> None:
    row = account_for(session, user)
    setattr(row, _USED[kind], getattr(row, _USED[kind]) + 1)
    session.commit()


# ---- for admins -------------------------------------------------------------------

@dataclass
class LinkView:
    link: AccountLink
    other: FreeAccount
    other_user: User | None  # None: the other account was deleted

    @property
    def reasons(self) -> list[str]:
        return [REASON_LABELS.get(r, r) for r in self.link.reasons or []]


def links_of(session: Session, user_id: int) -> list[LinkView]:
    row = session.query(FreeAccount).filter(FreeAccount.user_id == user_id).one_or_none()
    if row is None:
        return []
    out = []
    for link in session.query(AccountLink).filter(
            or_(AccountLink.account_a == row.id, AccountLink.account_b == row.id)).order_by(AccountLink.id):
        other = session.get(FreeAccount, link.account_b if link.account_a == row.id else link.account_a)
        out.append(LinkView(link, other, session.get(User, other.user_id) if other.user_id else None))
    return out


@dataclass
class Flag:
    link: AccountLink
    a: FreeAccount
    b: FreeAccount
    user_a: User | None
    user_b: User | None

    @property
    def reasons(self) -> list[str]:
        return [REASON_LABELS.get(r, r) for r in self.link.reasons or []]


def flags(session: Session, statuses: tuple[str, ...], limit: int = 200) -> list[Flag]:
    out = []
    for link in (session.query(AccountLink).filter(AccountLink.status.in_(statuses))
                 .order_by(AccountLink.updated_at.desc(), AccountLink.id.desc()).limit(limit)):
        a, b = session.get(FreeAccount, link.account_a), session.get(FreeAccount, link.account_b)
        out.append(Flag(link, a, b, session.get(User, a.user_id) if a.user_id else None,
                        session.get(User, b.user_id) if b.user_id else None))
    return out


def review(session: Session, link_id: int, *, same_person: bool, admin: User) -> AccountLink | None:
    link = session.get(AccountLink, link_id)
    if link is None:
        return None
    link.status = CONFIRMED if same_person else DISMISSED
    link.reviewed_by = admin.id
    link.updated_at = _utcnow()
    return link
