"""Account emails (sign-in / verification codes, password reset, welcome)
over SMTP. Without SMTP settings (local development) each email is saved as a
file in `data/mail_outbox/` instead -- never written to the log, since emails
carry codes and links that sign people in."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.auth.settings import AuthSettings

logger = logging.getLogger(__name__)

APP_NAME = "Job Assistant"
OUTBOX_DIR = Path(os.environ.get("MAIL_OUTBOX_DIR", "data/mail_outbox"))

# Tests replace this to capture outgoing mail.
outbox: list[EmailMessage] | None = None


class EmailDeliveryError(RuntimeError):
    """The mail server refused or couldn't be reached."""


def _smtp_send(message: EmailMessage, settings: AuthSettings) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(message)


def _deliver_in_background(message: EmailMessage, settings: AuthSettings) -> None:
    try:
        _smtp_send(message, settings)
    except Exception:
        logger.exception("Could not send email to %s", message["To"])


def _save_to_outbox(message: EmailMessage) -> None:
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = OUTBOX_DIR / f"{stamp}.eml"
    path.write_bytes(bytes(message))
    logger.warning("Email is not configured (SMTP_HOST): message to %s saved to %s", message["To"], path)


def _message(to: str, subject: str, body: str, settings: AuthSettings) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.smtp_from or f"{APP_NAME} <no-reply@localhost>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def send_email(to: str, subject: str, body: str, *, wait: bool = False) -> None:
    """Send an email. By default in the background, so the response time doesn't
    reveal anything; with `wait=True` (codes the user is waiting for), now --
    raising `EmailDeliveryError` if the mail server fails."""
    settings = AuthSettings()
    message = _message(to, subject, body, settings)
    if outbox is not None:
        outbox.append(message)
        return
    if not settings.smtp_host:
        _save_to_outbox(message)
        return
    if not wait:
        threading.Thread(target=_deliver_in_background, args=(message, settings), daemon=True).start()
        return
    try:
        _smtp_send(message, settings)
    except Exception as exc:
        logger.exception("Could not send email to %s", to)
        raise EmailDeliveryError(str(exc)) from exc


def send_code_email(to: str, code: str, *, new_account: bool, minutes_valid: int = 1) -> None:
    validity = "60 seconds" if minutes_valid == 1 else f"{minutes_valid} minutes"
    if new_account:
        subject = "Verify your email address"
        intro = f"Welcome to {APP_NAME}!\n\nUse the following verification code to continue:"
    else:
        subject = f"Your {APP_NAME} sign-in code"
        intro = f"Use the following code to sign in to {APP_NAME}:"
    send_email(to, subject, (
        f"{intro}\n\n    {code}\n\nThis code expires in {validity}.\n\n"
        "If you did not request this code, you can ignore this email.\n"
    ), wait=True)


def send_welcome_email(to: str, first_name: str, link: str) -> None:
    send_email(to, f"Welcome to {APP_NAME}", (
        f"Hi {first_name},\n\nYour account is ready. Search jobs from job boards and company career sites "
        f"across Canada, see how well each one fits you, and tailor your resume in a click:\n\n{link}\n\n"
        "Tip: complete your profile so we can suggest more relevant jobs.\n"
    ))


def send_verification_email(to: str, first_name: str, link: str) -> None:
    send_email(to, f"Confirm your email for {APP_NAME}", (
        f"Hi {first_name},\n\nConfirm your email address to finish setting up your account:\n\n{link}\n\n"
        "The link works once and expires in 24 hours. If you didn't sign up, ignore this email.\n"
    ))


def send_password_reset_email(to: str, first_name: str, link: str) -> None:
    send_email(to, f"Reset your {APP_NAME} password", (
        f"Hi {first_name},\n\nSomeone (hopefully you) asked to reset your password. Choose a new one here:\n\n"
        f"{link}\n\nThe link works once and expires in 1 hour. If you didn't ask, ignore this email -- "
        "your password hasn't changed.\n"
    ))


def send_existing_account_email(to: str, first_name: str, link: str) -> None:
    """Sign-up with an address that already has an account: tell its owner, not the visitor."""
    send_email(to, f"Your {APP_NAME} account", (
        f"Hi {first_name},\n\nSomeone tried to sign up with this email address, but you already have an "
        f"account. Sign in here (or reset your password from that page):\n\n{link}\n\n"
        "If this wasn't you, you can ignore this email.\n"
    ))
