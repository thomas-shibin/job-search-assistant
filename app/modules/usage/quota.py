"""Hourly limits per user, so one person (or a bot behind an account) can't
spend the platform's job-listing quota or analysis budget.

Two limits, each a sliding one-hour window counted from `ActivityEvent` rows
(in the database, so they survive restarts):

- **New searches** (default 1/hour for members and guests). A search is
  identified by its fingerprint (titles, locations, keywords, country): paging
  through it, or running it again within the hour, doesn't count again. Paging
  is capped at `platform.max_search_pages` pages.
- **Resume actions** (default 5/hour): match analyses, analyze & tailor,
  re-tailor and Quick Tailor. An action counts only when it actually made a
  paid analysis call -- answers served from cache are free.

Admins are unlimited. An admin can override either limit for one user
(`User.search_limit_per_hour` / `User.ai_limit_per_hour`), including 0 to
block it. Each user's checks run under a per-user lock, so a burst of
parallel requests can't slip past a limit (one server process -- see
docs/MULTI_TENANCY.md).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config.models import AppSetting
from app.core.config.schema import PlatformConfig
from app.core.exceptions import AppError
from app.modules.auth.models import User
from app.modules.usage.ai_calls import AICallLog
from app.modules.usage.models import ActivityEvent, ActivityKind

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=1)
SEARCH = "search"
AI = "ai"
_COUNTED = {SEARCH: (ActivityKind.SEARCH,), AI: ActivityKind.AI_KINDS}
_LOCK_WAIT_SECONDS = 120


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Allowance:
    kind: str
    limit: int | None  # None: unlimited
    used: int
    next_free_at: datetime | None = None  # when the next one frees up (None: never, or not exhausted)

    @property
    def unlimited(self) -> bool:
        return self.limit is None

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def minutes_until_free(self, now: datetime | None = None) -> int | None:
        if self.next_free_at is None:
            return None
        return max(1, math.ceil((self.next_free_at - (now or utcnow())).total_seconds() / 60))


class LimitReached(AppError):
    """An hourly limit stopped this action; `str(exc)` is the message to show."""

    def __init__(self, message: str, allowance: Allowance | None = None) -> None:
        super().__init__(message)
        self.allowance = allowance


def platform_limits(session: Session) -> PlatformConfig:
    row = session.get(AppSetting, "platform")
    try:
        return PlatformConfig.model_validate(row.value if row is not None and isinstance(row.value, dict) else {})
    except ValidationError:
        logger.warning("Platform limits are invalid; using the defaults")
        return PlatformConfig()


def limit_for(user: User, kind: str, platform: PlatformConfig) -> int | None:
    if user.is_admin:
        return None
    if kind == SEARCH:
        if user.search_limit_per_hour is not None:
            return user.search_limit_per_hour
        return platform.guest_searches_per_hour if user.is_guest else platform.member_searches_per_hour
    if user.ai_limit_per_hour is not None:
        return user.ai_limit_per_hour
    return platform.member_ai_actions_per_hour


def allowance(session: Session, user: User, kind: str, platform: PlatformConfig | None = None,
              *, now: datetime | None = None) -> Allowance:
    platform = platform or platform_limits(session)
    limit = limit_for(user, kind, platform)
    now = now or utcnow()
    times = [t for (t,) in session.query(ActivityEvent.created_at).filter(
        ActivityEvent.user_id == user.id, ActivityEvent.kind.in_(_COUNTED[kind]),
        ActivityEvent.created_at >= now - WINDOW,
    ).order_by(ActivityEvent.created_at)]
    result = Allowance(kind=kind, limit=limit, used=len(times))
    if result.exhausted and limit:
        result.next_free_at = times[len(times) - limit] + WINDOW
    return result


def ai_allowance_for(session: Session, user: User | None) -> Allowance | None:
    """The signed-in user's resume-action allowance for display (None when signed out)."""
    return allowance(session, user, AI) if user is not None else None


def record(session: Session, user_id: int, kind: str, summary: str | None = None, *, ref: str | None = None,
           meta: dict | None = None, provider_calls: int = 0, ai_calls: int = 0) -> ActivityEvent | None:
    """Log one activity. Never lets a logging failure break what the user is doing."""
    try:
        event = ActivityEvent(user_id=user_id, kind=kind, summary=(summary or None) and summary[:300], ref=ref,
                              meta=meta, provider_calls=provider_calls, ai_calls=ai_calls)
        session.add(event)
        session.commit()
        return event
    except Exception:
        session.rollback()
        logger.exception("Couldn't record activity %s for user_id=%s", kind, user_id)
        return None


_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextmanager
def _user_lock(user_id: int) -> Iterator[None]:
    with _locks_guard:
        lock = _locks.setdefault(user_id, threading.Lock())
    if not lock.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise LimitReached("Your previous request is still running. Please wait a moment and try again.")
    try:
        yield
    finally:
        lock.release()


def _in(minutes: int | None) -> str:
    if minutes is None:
        return ""
    return f"in {minutes} minute{'s' if minutes != 1 else ''}"


def _search_message(user: User, a: Allowance) -> str:
    if a.limit == 0:
        return "New job searches aren't available on your account right now."
    plural = "search" if a.limit == 1 else "searches"
    message = (f"You've used your {a.limit} new {plural} for this hour. You can start a new search "
               f"{_in(a.minutes_until_free())} -- paging through your last search still works.")
    if user.is_guest:
        message += " Create a free account to tailor your resume to these jobs."
    return message


def _ai_message(a: Allowance) -> str:
    if a.limit == 0:
        return "Match analyses and tailored resumes aren't available on your account right now."
    return (f"You've used your {a.limit} resume actions (match analyses and tailored resumes) for this hour. "
            f"More are available {_in(a.minutes_until_free())}.")


# ---- searches --------------------------------------------------------------------

def search_fingerprint(titles: list[str], locations: list[str], keywords: list[str], country: str) -> str:
    def norm(values: list[str]) -> list[str]:
        return sorted({v.strip().lower() for v in values if v and v.strip()})

    payload = json.dumps([norm(titles), norm(locations), norm(keywords), (country or "").strip().lower()])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


@dataclass
class SearchCharge:
    event_id: int | None  # the new SEARCH event (None: a repeat/next page of one already counted)
    allowance: Allowance


def charge_search(session: Session, user: User, *, fingerprint: str, page: int, summary: str, meta: dict,
                  platform: PlatformConfig | None = None) -> SearchCharge:
    """Count a search against the hourly limit before it runs, or raise
    `LimitReached`. Paging through / repeating a search counted this hour is free."""
    platform = platform or platform_limits(session)
    if page > platform.max_search_pages:
        raise LimitReached(f"Results stop at page {platform.max_search_pages} -- refine your search to see "
                           "other jobs.")
    with _user_lock(user.id):
        repeat = session.query(ActivityEvent.id).filter(
            ActivityEvent.user_id == user.id, ActivityEvent.kind == ActivityKind.SEARCH,
            ActivityEvent.ref == fingerprint, ActivityEvent.created_at >= utcnow() - WINDOW,
        ).first() is not None
        current = allowance(session, user, SEARCH, platform)
        if repeat:
            return SearchCharge(event_id=None, allowance=current)
        if current.exhausted:
            record(session, user.id, ActivityKind.LIMIT_HIT, f"Search limit: {summary}",
                   meta={"limit": SEARCH, "allowed": current.limit})
            raise LimitReached(_search_message(user, current), current)
        event = record(session, user.id, ActivityKind.SEARCH, summary, ref=fingerprint, meta=meta)
        current.used += 1
        return SearchCharge(event_id=event.id if event else None, allowance=current)


def settle_search(session: Session, user: User, charge: SearchCharge, *, provider_calls: int, failed: bool,
                  summary: str, fingerprint: str, page: int) -> None:
    """After the search ran: note what it cost; give the search back if it failed outright."""
    try:
        if charge.event_id is not None:
            event = session.get(ActivityEvent, charge.event_id)
            if event is not None:
                if failed:
                    session.delete(event)
                    charge.allowance.used = max(0, charge.allowance.used - 1)
                else:
                    event.provider_calls = provider_calls
                session.commit()
        elif provider_calls:
            record(session, user.id, ActivityKind.SEARCH_MORE, summary, ref=fingerprint,
                   meta={"page": page}, provider_calls=provider_calls)
    except Exception:
        session.rollback()
        logger.exception("Couldn't settle search activity for user_id=%s", user.id)


# ---- resume actions (paid analysis calls) ------------------------------------------

def _paid_calls(session: Session, user_id: int) -> int:
    return session.query(func.count(AICallLog.id)).filter(
        AICallLog.user_id == user_id, AICallLog.cache_hit.is_(False)
    ).scalar() or 0


def check_ai(session: Session, user: User, platform: PlatformConfig | None = None) -> Allowance:
    """The current resume-action allowance; raises `LimitReached` when it's used up."""
    current = allowance(session, user, AI, platform)
    if current.exhausted:
        raise LimitReached(_ai_message(current), current)
    return current


@contextmanager
def ai_action(session: Session, user: User, kind: str, summary: str | None = None, *, job_id: int | None = None,
              platform: PlatformConfig | None = None) -> Iterator[None]:
    """Wrap one resume action: refuse it when the hourly limit is used up, and
    afterwards count it if it made at least one paid analysis call."""
    with _user_lock(user.id):
        try:
            check_ai(session, user, platform)
        except LimitReached:
            record(session, user.id, ActivityKind.LIMIT_HIT, f"Resume-action limit: {summary or ''}".strip(),
                   meta={"limit": AI, "job_id": job_id})
            raise
        before = _paid_calls(session, user.id)
        try:
            yield
        finally:
            try:
                made = _paid_calls(session, user.id) - before
            except Exception:  # the action left the session failed: start clean
                session.rollback()
                try:
                    made = _paid_calls(session, user.id) - before
                except Exception:
                    made = 0
            if made > 0:
                record(session, user.id, kind, summary, meta={"job_id": job_id} if job_id else None, ai_calls=made)


def job_label(job) -> str:
    return " at ".join(part for part in (getattr(job, "title", None), getattr(job, "company", None)) if part)
