"""Job alerts by email. A scan queues each new watch match for the member
(`WatchlistAlert` rows: channel "email", status "pending", see watchlist.py);
`send_due` emails them in one message, as often as the member chose on the
Watchlist page:

- "instant": after every scan that found something;
- "daily": the first scan from DIGEST_HOUR local time (the server's TZ), once a day;
- "weekly": the same, on Mondays.

- One email lists the newest MAX_JOBS_PER_EMAIL matches and counts the rest,
  with a link to see them all. Matches older than STALE_AFTER are dropped rather
  than sent late (status "expired").
- Every email carries an unsubscribe link and the List-Unsubscribe headers mail
  apps show as a button (RFC 8058 one-click), as Canada's anti-spam law (CASL)
  and the large mailbox providers require. Unsubscribing turns email alerts off
  without signing in; the link holds a random per-account key
  (`watchlist.email_unsubscribe_key`), so it can't be guessed and keeps working.
  Set MAIL_POSTAL_ADDRESS so the footer carries the sender's mailing address.
- A failed send leaves the matches pending, so the next scan tries again.
"""
from __future__ import annotations

import hmac
import html
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config.schema import AppConfig
from app.core.config.store import save_section
from app.core.logging import log_event
from app.integrations.email import APP_NAME, EmailSettings, send_email
from app.modules.alerts.models import WatchlistAlert
from app.modules.alerts.watchlist import EMAIL
from app.modules.auth.models import User
from app.modules.auth.settings import AuthSettings
from app.modules.jobs.models import Job

logger = logging.getLogger(__name__)

DIGEST_HOUR = 8  # local time
MAX_JOBS_PER_EMAIL = 25
STALE_AFTER = timedelta(days=8)  # a weekly summary's span, plus a day of slack


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC


# ---- when ------------------------------------------------------------------------------

def period_start(frequency: str, now: datetime) -> datetime | None:
    """When the current daily or weekly period began (`now` is local, aware):
    the latest DIGEST_HOUR, on a Monday for weekly. None for "instant"."""
    if frequency not in ("daily", "weekly"):
        return None
    start = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    if frequency == "weekly":
        start -= timedelta(days=start.weekday())  # back to Monday
    return start


def is_due(frequency: str, last_sent: datetime | None, now: datetime) -> bool:
    """Whether an email may go out now. `last_sent` is naive UTC, `now` local and aware."""
    if frequency == "off":
        return False
    start = period_start(frequency, now)
    if start is None or last_sent is None:
        return True
    return last_sent < start.astimezone(timezone.utc).replace(tzinfo=None)


# ---- unsubscribe links -------------------------------------------------------------------

def unsubscribe_token(session: Session, user: User, config: AppConfig) -> str:
    """"<user id>.<key>", making the account's key the first time it's needed."""
    key = config.watchlist.email_unsubscribe_key
    if not key:
        key = secrets.token_urlsafe(24)
        save_section(session, "watchlist", {**config.watchlist.model_dump(exclude={"watches"}),
                                            "email_unsubscribe_key": key})
        config.watchlist.email_unsubscribe_key = key
    return f"{user.id}.{key}"


def parse_token(token: str) -> tuple[int, str] | None:
    """(user id, key) from an unsubscribe token, or None if it's malformed."""
    user_id, _, key = (token or "").partition(".")
    if not user_id.isdigit() or not 16 <= len(key) <= 64:
        return None
    return int(user_id), key


def key_matches(config: AppConfig, key: str) -> bool:
    expected = config.watchlist.email_unsubscribe_key
    return bool(expected) and hmac.compare_digest(expected, key)


def unsubscribe(session: Session, config: AppConfig) -> None:
    """Turn email alerts off for the session's member and drop what's queued."""
    save_section(session, "watchlist", {**config.watchlist.model_dump(exclude={"watches"}), "email_alerts": "off"})
    session.query(WatchlistAlert).filter(WatchlistAlert.channel == EMAIL, WatchlistAlert.status == "pending").update(
        {WatchlistAlert.status: "cancelled", WatchlistAlert.detail: "unsubscribed"})
    session.commit()


# ---- the email ---------------------------------------------------------------------------

@dataclass
class _Item:
    job: Job
    watches: list[str]
    score: int | None


def _salary(job: Job) -> str | None:
    low, high = job.salary_min, job.salary_max
    if not (low or high):
        return None
    amount = f"${low:,}" if low and (not high or high == low) else f"${(low or 0):,}–${high:,}"
    return f"{amount} {job.currency}" if job.currency and job.currency != "CAD" else amount


def _facts(item: _Item) -> list[str]:
    job = item.job
    facts = [job.company]
    if job.location:
        facts.append(job.location)
    if (salary := _salary(job)):
        facts.append(salary)
    if item.score is not None:
        facts.append(f"match {item.score}/100")
    return facts


def _subject(frequency: str, items: list[_Item], count: int) -> str:
    if count == 1:
        job = items[0].job
        return f"New job: {job.title} at {job.company}"
    prefix = {"daily": "Your daily job alert: ", "weekly": "Your weekly job alert: "}.get(frequency, "")
    return f"{prefix}{count} new jobs match your watches"


def compose(user: User, frequency: str, items: list[_Item], more: int, links: dict[str, str]) -> tuple[str, str, str]:
    """(subject, plain text, HTML) for one alert email."""
    base = links["base"]
    count = len(items) + more
    intro = f"{count} new job{'s' if count != 1 else ''} match{'es' if count == 1 else ''} your watches."
    postal = EmailSettings().postal_address

    name = user.first_name or "there"
    text = [f"Hi {name},", "", intro, ""]
    for item in items:
        job = item.job
        text += [job.title, " · ".join(_facts(item)), f"Watch: {', '.join(item.watches)}",
                 f"Details: {base}/jobs/{job.id}"]
        if job.source_url:
            text.append(f"Apply: {job.source_url}")
        text.append("")
    if more:
        text += [f"And {more} more: {base}/watchlist#alerts", ""]
    text += ["--", f"You're getting this because you turned on job alerts in {APP_NAME}.",
             f"Change how often, or your watches: {base}/watchlist",
             f"Unsubscribe from job alert emails: {links['unsubscribe']}"]
    if postal:
        text.append(f"{APP_NAME}, {postal}")

    e = html.escape
    rows = []
    for item in items:
        job = item.job
        apply = (f' &middot; <a href="{e(job.source_url, quote=True)}" style="color:#2563eb">Apply</a>'
                 if job.source_url else "")
        rows.append(
            '<tr><td style="padding:14px 0;border-bottom:1px solid #e5e7eb">'
            f'<a href="{e(base)}/jobs/{job.id}" style="font-size:16px;font-weight:600;color:#111827;'
            f'text-decoration:none">{e(job.title)}</a>'
            f'<div style="color:#4b5563;font-size:14px;margin-top:4px">{e(" · ".join(_facts(item)))}</div>'
            f'<div style="color:#6b7280;font-size:13px;margin-top:4px">Watch: {e(", ".join(item.watches))}'
            f' &middot; <a href="{e(base)}/jobs/{job.id}" style="color:#2563eb">Details</a>{apply}</div>'
            '</td></tr>')
    if more:
        rows.append(f'<tr><td style="padding:14px 0"><a href="{e(base)}/watchlist#alerts" style="color:#2563eb">'
                    f'And {more} more</a></td></tr>')
    footer = (f'You\'re getting this because you turned on job alerts in {e(APP_NAME)}.<br>'
              f'<a href="{e(base)}/watchlist" style="color:#6b7280">Change how often, or your watches</a> &middot; '
              f'<a href="{e(links["unsubscribe"], quote=True)}" style="color:#6b7280">Unsubscribe</a>')
    if postal:
        footer += f"<br>{e(APP_NAME)}, {e(postal)}"
    body_html = (
        '<!doctype html><html><body style="margin:0;background:#f4f6fb">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;'
        'background:#ffffff;border-radius:12px;padding:24px;font-family:Arial,Helvetica,sans-serif">'
        f'<tr><td style="font-size:15px;color:#111827">Hi {e(name)},<p style="margin:8px 0 4px">'
        f'{e(intro)}</p></td></tr>'
        + "".join(rows) +
        f'<tr><td style="padding-top:20px;font-size:12px;color:#6b7280;line-height:1.6">{footer}</td></tr>'
        '</table></td></tr></table></body></html>')
    return _subject(frequency, items, count), "\n".join(text), body_html


# ---- sending -----------------------------------------------------------------------------

def last_sent(session: Session) -> datetime | None:
    return session.query(func.max(WatchlistAlert.sent_at)).filter(WatchlistAlert.channel == EMAIL).scalar()


def send_due(session: Session, user: User, config: AppConfig, *, now: datetime | None = None) -> int:
    """Email the member's pending matches if their email is due. Returns how many
    jobs were sent (0: nothing due, nothing pending, or the send failed)."""
    frequency = config.watchlist.email_alerts
    if not config.watchlist.enabled or frequency == "off" or not user.email or user.email_verified_at is None:
        return 0
    utc_now = _utcnow()
    pending = (session.query(WatchlistAlert)
               .filter(WatchlistAlert.channel == EMAIL, WatchlistAlert.status == "pending")
               .order_by(WatchlistAlert.created_at.desc(), WatchlistAlert.id.desc()).all())
    fresh = []
    for row in pending:
        job = session.get(Job, row.job_id) if row.job_id else None
        if job is None or row.created_at < utc_now - STALE_AFTER:
            row.status, row.detail = "expired", "job removed" if job is None else "too old to send"
            continue
        fresh.append((row, job))
    session.commit()
    if not fresh or not is_due(frequency, last_sent(session), now or datetime.now().astimezone()):
        return 0

    base = AuthSettings().public_url.rstrip("/")
    links = {"base": base, "unsubscribe": f"{base}/alerts/unsubscribe/{unsubscribe_token(session, user, config)}"}
    shown = fresh[:MAX_JOBS_PER_EMAIL]
    items = [_Item(job, list(row.watches or []), row.score) for row, job in shown]
    subject, text, body_html = compose(user, frequency, items, len(fresh) - len(shown), links)
    try:
        send_email(user.email, subject, text, html=body_html, wait=True, headers={
            "List-Unsubscribe": f"<{links['unsubscribe']}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        })
    except Exception:
        logger.warning("Job alert email to user_id=%s failed; the matches stay queued", user.id, exc_info=True)
        return 0
    listed = {row.id for row, _ in shown}
    for row, _ in fresh:  # those past MAX_JOBS_PER_EMAIL were counted in "And N more"
        row.status, row.sent_at = "sent", utc_now
        if row.id not in listed:
            row.detail = "counted in the email (not listed)"
    session.commit()
    log_event(logger, "ALERT_EMAIL_SENT", user_id=user.id, jobs=len(fresh), frequency=frequency)
    return len(fresh)
