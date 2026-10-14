"""What the account emails say: sign-in and verification codes, email
confirmation, password reset, welcome. Delivery is app/integrations/email.py."""
from __future__ import annotations

from app.integrations.email import APP_NAME, EmailDeliveryError, send_email

__all__ = ["EmailDeliveryError", "send_code_email", "send_welcome_email", "send_verification_email",
           "send_password_reset_email", "send_existing_account_email"]


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
