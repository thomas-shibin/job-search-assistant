"""Reviews and Contact us messages.

The review popup asks each member once: after ACTIVE_MINUTES of use (counted
in the browser, across pages, only while the tab is in view and they're
using it). "Maybe later" asks again after LATER_DAYS; "Don't ask again" and
sending a review stop it. The account menu's "Rate Hirlumo" opens it any time.

A Contact us message is kept here and emailed to CONTACT_EMAIL with the
member's address as Reply-To, so answering it is just replying.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.integrations.email import APP_NAME, send_email
from app.modules.auth.models import User
from app.modules.auth.ratelimit import RateLimiter
from app.modules.feedback.models import ContactMessage, Review
from app.modules.usage.plans import CONTACT_EMAIL

logger = logging.getLogger(__name__)

ACTIVE_MINUTES = 5
LATER_DAYS = 7
MAX_COMMENT = 2000
MAX_TITLE = 150
MAX_MESSAGE = 5000

LATER, NEVER, SENT = "later", "never", "sent"

# field -> (question, what 1 star means, what 5 stars mean)
QUESTIONS: dict[str, tuple[str, str, str]] = {
    "jobs_rating": ("Are you enjoying the jobs you find here?", "Not at all", "Love them"),
    "enough_jobs_rating": ("Are you able to find enough jobs?", "Hardly any", "Plenty"),
    "resume_rating": ("Are you satisfied with the resume builder?", "Not at all", "Very"),
    "features_rating": ("And the other features: tracking, alerts, the resume check?", "Not at all", "Very"),
}

SHORT_LABELS: dict[str, str] = {"jobs_rating": "Enjoying the jobs", "enough_jobs_rating": "Enough jobs",
                                "resume_rating": "Resume builder", "features_rating": "Other features"}

TOPICS: dict[str, str] = {
    "question": "A question", "problem": "Something isn't working", "limits": "Higher limits",
    "idea": "An idea or feedback", "account": "My account or data", "other": "Something else",
}

# Contact us: a few messages an hour per account, so a stuck button can't flood the inbox.
contact_limiter = RateLimiter(limit=5, window_seconds=3600)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FeedbackError(ValueError):
    """What to tell the member when what they sent can't be saved."""


def review_due(user: User | None, now: datetime | None = None) -> bool:
    """Whether the popup should come up by itself for `user`."""
    if user is None or user.is_guest or not user.email_verified:
        return False
    if user.review_prompt in (NEVER, SENT):
        return False
    after = user.review_prompt_after
    return after is None or after <= (now or _utcnow())


def _rating(value) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 5 else None


def _account(session: Session, user: User) -> User:
    return session.get(User, user.id) or user


def save_review(session: Session, user: User, ratings: dict[str, object], comment: str | None,
                page: str | None = None) -> Review:
    """Store a review (any of the questions may be left out, but not all of
    them and the comment) and stop the popup asking."""
    values = {field: _rating(ratings.get(field)) for field in QUESTIONS}
    comment = (comment or "").strip()[:MAX_COMMENT] or None
    if not any(values.values()) and not comment:
        raise FeedbackError("Pick a few stars or write a comment first.")
    review = Review(**values, comment=comment, page=(page or "")[:200] or None)
    session.add(review)
    account = _account(session, user)
    account.review_prompt, account.review_prompt_after = SENT, None
    session.commit()
    return review


def postpone(session: Session, user: User, *, never: bool = False) -> None:
    """"Maybe later" (ask again in LATER_DAYS) or "Don't ask again"."""
    account = _account(session, user)
    if account.review_prompt == SENT and not never:
        return  # already reviewed: nothing to ask later
    account.review_prompt = NEVER if never else LATER
    account.review_prompt_after = None if never else _utcnow() + timedelta(days=LATER_DAYS)
    session.commit()


def send_contact(session: Session, user: User, topic: str, title: str, message: str) -> ContactMessage:
    """Keep the message and email it to us. Raises FeedbackError when it's
    incomplete or the member has sent too many this hour."""
    topic = topic if topic in TOPICS else "other"
    title = " ".join((title or "").split())[:MAX_TITLE]
    message = (message or "").strip()
    if not title:
        raise FeedbackError("Add a title, so we know what it's about.")
    if len(message) < 10:
        raise FeedbackError("Tell us a little more in the message.")
    if len(message) > MAX_MESSAGE:
        raise FeedbackError(f"Please keep the message under {MAX_MESSAGE:,} characters.")
    if not contact_limiter.allow(str(user.id)):
        raise FeedbackError(f"You've sent several messages this hour. Please wait a little, or email {CONTACT_EMAIL}.")
    saved = ContactMessage(topic=topic, title=title, message=message, first_name=user.first_name or "",
                           last_name=user.last_name or "", email=user.email)
    session.add(saved)
    session.commit()
    name = " ".join(part for part in (saved.first_name, saved.last_name) if part) or saved.email
    body = (f"{name} <{saved.email}> sent a message from {APP_NAME}'s Contact us page.\n\n"
            f"Topic: {TOPICS[topic]}\nTitle: {title}\nAccount id: {user.id}\n\n{message}\n\n"
            f"-- Reply to this email to answer {name} directly.")
    try:
        send_email(CONTACT_EMAIL, f"[{APP_NAME} contact] {title}", body,
                   headers={"Reply-To": f"{name} <{saved.email}>"})
        saved.emailed = True
        session.commit()
    except Exception:  # kept anyway: the admin page lists it
        session.rollback()
        logger.exception("Could not email contact message id=%s", saved.id)
    return saved


# ---- the admin page --------------------------------------------------------------

@dataclass
class ReviewSummary:
    count: int
    averages: dict[str, float | None]  # field -> average stars (None: nobody answered)
    answered: dict[str, int]


def review_summary(session: Session) -> ReviewSummary:
    count = session.query(func.count(Review.id)).scalar() or 0
    averages, answered = {}, {}
    for field in QUESTIONS:
        column = getattr(Review, field)
        avg, n = session.query(func.avg(column), func.count(column)).one()
        averages[field] = round(float(avg), 1) if avg is not None else None
        answered[field] = n or 0
    return ReviewSummary(count=count, averages=averages, answered=answered)


def recent_reviews(session: Session, limit: int = 200) -> list[tuple[Review, User | None]]:
    rows = session.query(Review).order_by(Review.created_at.desc()).limit(limit).all()
    users = {u.id: u for u in session.query(User).filter(User.id.in_({r.user_id for r in rows}))} if rows else {}
    return [(r, users.get(r.user_id)) for r in rows]


def messages(session: Session, *, handled: bool, limit: int = 200) -> list[ContactMessage]:
    query = session.query(ContactMessage)
    query = query.filter(ContactMessage.handled_at.is_not(None) if handled else ContactMessage.handled_at.is_(None))
    return query.order_by(ContactMessage.created_at.desc()).limit(limit).all()


def message_counts(session: Session) -> dict[str, int]:
    open_ = session.query(func.count(ContactMessage.id)).filter(ContactMessage.handled_at.is_(None)).scalar() or 0
    done = session.query(func.count(ContactMessage.id)).filter(ContactMessage.handled_at.is_not(None)).scalar() or 0
    return {"open": open_, "handled": done}


def mark_handled(session: Session, message_id: int, *, handled: bool = True) -> ContactMessage | None:
    saved = session.get(ContactMessage, message_id)
    if saved is None:
        return None
    saved.handled_at = _utcnow() if handled else None
    session.commit()
    return saved
