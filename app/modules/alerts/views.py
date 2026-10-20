"""Rendering the Watchlist page (/watchlist): the user's watches, the alerts
sent recently, and the job-alert settings form. Shared by the alerts routes
and the Settings page, which saves the same "watchlist" section."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config.models import WatchlistEntry
from app.core.config.schema import AppConfig
from app.core.config.store import WATCH_LIST_FIELDS
from app.integrations.telegram import TelegramSettings
from app.modules.alerts.models import WatchlistAlert
from app.modules.jobs.models import Job
from app.web.forms import CHOICE_LABELS, section_form
from app.web.responses import load_config_or_500
from app.web.templating import templates

# Fields a member doesn't set: platform-controlled, or filled in by the app.
HIDDEN_FIELDS = {
    "search": ("adzuna_daily_limit",),
    "candidate": ("base_resume",),
    "watchlist": ("watches", "dashboard_url", "telegram_chat_id", "email_unsubscribe_key"),
}



def watchlist_response(request: Request, session: Session, *, error: str | None = None,
                        message: str | None = None, edit: WatchlistEntry | None = None, status_code: int = 200):
    config = load_config_or_500(session)
    telegram = TelegramSettings()
    chat_id = config.watchlist.telegram_chat_id
    return templates.TemplateResponse(
        request, "watchlist.html",
        {
            "entries": session.query(WatchlistEntry).order_by(WatchlistEntry.id).all(),
            "alerts": _recent_alerts(session),
            "alert_counts": _alert_counts(session),
            "list_fields": WATCH_LIST_FIELDS,
            "settings_form": section_form(
                AppConfig.model_fields["watchlist"].annotation, "watchlist",
                config.watchlist.model_dump(mode="json"), skip=HIDDEN_FIELDS["watchlist"],
            ),
            "token_set": bool(telegram.bot_token), "chat_id": chat_id,
            "watchlist_enabled": config.watchlist.enabled,
            "email_alerts": config.watchlist.email_alerts,
            "email_alerts_label": CHOICE_LABELS["watchlist.email_alerts"][config.watchlist.email_alerts],
            "alert_email": getattr(getattr(request.state, "user", None), "email", None),
            "error": error, "message": message, "edit": edit,
        },
        status_code=status_code,
    )


def _recent_alerts(session: Session, limit: int = 50) -> list[dict]:
    rows = (
        session.query(WatchlistAlert, Job)
        .outerjoin(Job, Job.id == WatchlistAlert.job_id)  # the job may since have been cleaned up
        .order_by(WatchlistAlert.created_at.desc(), WatchlistAlert.id.desc())
        .limit(limit)
        .all()
    )
    return [{"alert": alert, "job": job} for alert, job in rows]


def _alert_counts(session: Session) -> dict[str, int]:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    rows = (
        session.query(WatchlistAlert.status, func.count())
        .filter(WatchlistAlert.created_at >= since)
        .group_by(WatchlistAlert.status)
        .all()
    )
    return dict(rows)
