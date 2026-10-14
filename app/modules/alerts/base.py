"""The notification interface (`NotificationProvider`) and its message (`JobNotification`)."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class JobNotification(BaseModel):
    job_id: int
    title: str
    company: str
    location: str | None = None
    overall_score: int
    strong_matches: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    review_url: str | None = None


class NotificationProvider(Protocol):
    """Kept generic so Windows toast (V1), email, Telegram, Slack, or mobile push can
    all implement the same call site.
    """

    def notify(self, notification: JobNotification) -> None:
        """Implemented in Milestone 10 (Windows toast)."""
        ...
