"""Outgoing email over SMTP (any provider: Gmail, Amazon SES, Resend, ...).

Without SMTP settings (local development) each email is saved as a file in
MAIL_OUTBOX_DIR (default `data/mail_outbox/`) instead -- never written to the
log, since emails carry codes and links that sign people in. What the emails
say lives with the feature that sends them (e.g. app/modules/auth/emails.py).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import IntegrationError

logger = logging.getLogger(__name__)

APP_NAME = "Job Assistant"


class EmailSettings(BaseSettings):
    """SMTP connection settings, from the environment (.env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_from: str | None = Field(default=None, alias="SMTP_FROM")
    smtp_starttls: bool = Field(default=True, alias="SMTP_STARTTLS")
    outbox_dir: Path = Field(default=Path("data/mail_outbox"), alias="MAIL_OUTBOX_DIR")

    @property
    def configured(self) -> bool:
        return bool(self.smtp_host)


# Tests replace this to capture outgoing mail.
outbox: list[EmailMessage] | None = None


class EmailDeliveryError(IntegrationError, RuntimeError):
    """The mail server refused or couldn't be reached."""


def _smtp_send(message: EmailMessage, settings: EmailSettings) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(message)


def _deliver_in_background(message: EmailMessage, settings: EmailSettings) -> None:
    try:
        _smtp_send(message, settings)
    except Exception:
        logger.exception("Could not send email to %s", message["To"])


def _save_to_outbox(message: EmailMessage, settings: EmailSettings) -> None:
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = settings.outbox_dir / f"{stamp}.eml"
    path.write_bytes(bytes(message))
    logger.warning("Email is not configured (SMTP_HOST): message to %s saved to %s", message["To"], path)


def _message(to: str, subject: str, body: str, settings: EmailSettings) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.smtp_from or f"{APP_NAME} <no-reply@localhost>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def send_email(to: str, subject: str, body: str, *, wait: bool = False) -> None:
    """Send a plain-text email.

    Args:
        to: Recipient address.
        subject: Subject line.
        body: Plain-text body.
        wait: Send now and report failure (for codes the user is waiting for).
            By default the email goes out on a background thread, so a
            response's timing doesn't reveal whether an account exists.

    Raises:
        EmailDeliveryError: Only with `wait=True`, when the mail server fails.
    """
    settings = EmailSettings()
    message = _message(to, subject, body, settings)
    if outbox is not None:
        outbox.append(message)
        return
    if not settings.smtp_host:
        _save_to_outbox(message, settings)
        return
    if not wait:
        threading.Thread(target=_deliver_in_background, args=(message, settings), daemon=True).start()
        return
    try:
        _smtp_send(message, settings)
    except Exception as exc:
        logger.exception("Could not send email to %s", to)
        raise EmailDeliveryError(str(exc)) from exc
