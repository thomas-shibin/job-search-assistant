"""Windows toast notification provider (Milestone 10). Implements the
`NotificationProvider` protocol in `app/notifications/base.py` using
`win10toast`, a thin ctypes wrapper around the native Windows notification
API -- no extra background service or account needed, consistent with this
being a local, single-user tool.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.logging import log_event
from app.modules.alerts.base import JobNotification

logger = logging.getLogger(__name__)

_TOAST_DURATION_SECONDS = 10


def format_notification(notification: JobNotification) -> tuple[str, str]:
    """(title, body) shared by every OS notification provider."""
    title = f"Qualified job: {notification.overall_score}/100"
    lines = [f"{notification.title} @ {notification.company}"]
    if notification.location:
        lines.append(notification.location)
    if notification.strong_matches:
        lines.append("Strong: " + ", ".join(notification.strong_matches[:5]))
    return title, "\n".join(lines)


class WindowsToastNotificationProvider:
    """Structurally satisfies `app.modules.alerts.base.NotificationProvider`."""

    def __init__(self, toaster: Any | None = None) -> None:
        if toaster is None:
            # Imported here, not at module level: win10toast only installs on
            # Windows, and this module must stay importable elsewhere (tests,
            # the Linux provider sharing `format_notification`).
            from win10toast import ToastNotifier

            toaster = ToastNotifier()
        self._toaster = toaster

    def notify(self, notification: JobNotification) -> None:
        title, message = format_notification(notification)

        try:
            # threaded=False (blocking): win10toast's threaded mode raises inside a
            # background thread that this try/except can't observe, so a failure
            # there would go uncaught. This call already runs on the scheduler's own
            # background thread (see app/scheduler/background.py), so blocking for
            # the toast's ~10s duration doesn't affect the web server.
            self._toaster.show_toast(title, message, duration=_TOAST_DURATION_SECONDS, threaded=False)
        except Exception:
            # A failed OS-level toast (no notification service available, headless
            # session, etc.) should never break the pipeline that triggered it.
            logger.warning("Windows toast notification failed for job_id=%s", notification.job_id, exc_info=True)
            return

        log_event(logger, "NOTIFICATION_SENT", job_id=notification.job_id, overall_score=notification.overall_score)
