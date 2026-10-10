"""Linux desktop notification provider. Implements the `NotificationProvider`
protocol in `app/notifications/base.py` by shelling out to `notify-send`
(libnotify-bin, preinstalled on Ubuntu desktop) -- the Linux counterpart of
`windows_toast.py`, with no extra Python dependency.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from app.notifications.base import JobNotification
from app.notifications.windows_toast import format_notification
from app.utils.logging import log_event

logger = logging.getLogger(__name__)

_TOAST_DURATION_MS = 10_000
_SEND_TIMEOUT_SECONDS = 10


class LinuxNotifySendProvider:
    """Structurally satisfies `app.notifications.base.NotificationProvider`."""

    def __init__(self, notify_send_path: str | None = None) -> None:
        path = notify_send_path or shutil.which("notify-send")
        if not path:
            raise RuntimeError("notify-send not found on PATH (Ubuntu: sudo apt install libnotify-bin)")
        self._notify_send = path

    def notify(self, notification: JobNotification) -> None:
        title, message = format_notification(notification)
        try:
            subprocess.run(
                [self._notify_send, "--app-name=Job Assistant", f"--expire-time={_TOAST_DURATION_MS}", title, message],
                check=True,
                capture_output=True,
                timeout=_SEND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            # No notification daemon (headless session, SSH, etc.) must never
            # break the pipeline that triggered it -- same rule as the Windows toast.
            logger.warning("notify-send notification failed for job_id=%s", notification.job_id, exc_info=True)
            return

        log_event(logger, "NOTIFICATION_SENT", job_id=notification.job_id, overall_score=notification.overall_score)
