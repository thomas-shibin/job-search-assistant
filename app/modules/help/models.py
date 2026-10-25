"""Questions asked in the help chat and the Help centre search, without who
asked them, kept for QUESTION_DAYS (app/modules/help/routes.py) to see what
people look for and what the articles don't answer."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


class HelpQuestion(Base):
    __tablename__ = "help_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(300))
    channel: Mapped[str] = mapped_column(String(16))  # chat | search
    answered: Mapped[bool] = mapped_column(Boolean, default=False)
    article: Mapped[str | None] = mapped_column(String(80), nullable=True)  # the answer's article
    section: Mapped[str | None] = mapped_column(String(200), nullable=True)
    page: Mapped[str | None] = mapped_column(String(200), nullable=True)  # where it was asked
    signed_in: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


def recent_questions(session, *, unanswered: bool = False, limit: int = 200) -> list[HelpQuestion]:
    query = session.query(HelpQuestion)
    if unanswered:
        query = query.filter(HelpQuestion.answered.is_(False))
    return query.order_by(HelpQuestion.created_at.desc()).limit(limit).all()


def question_counts(session) -> dict[str, int]:
    from sqlalchemy import func

    total = session.query(func.count(HelpQuestion.id)).scalar() or 0
    unanswered = session.query(func.count(HelpQuestion.id)).filter(HelpQuestion.answered.is_(False)).scalar() or 0
    return {"total": total, "unanswered": unanswered}
