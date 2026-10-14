"""Watchlist alerts: every newly discovered job is checked against your
watches (Watchlist page), and matches are pushed to Telegram. Separate from
`service.maybe_notify_qualified_job`: a watch is an explicit "tell me about
anything like this", optionally gated on a minimum match score.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.modules.jobs.catalog.geo import province_code, province_of
from app.core.config.schema import AppConfig, Watch, WatchlistConfig
from app.modules.jobs.models import Job
from app.core.config.models import WatchlistAlert
from app.integrations.telegram import TelegramClient, TelegramSettings
from app.core.logging import log_event
from app.shared.terms import normalize_term

logger = logging.getLogger(__name__)


def _contains_word(text: str, term: str) -> bool:
    # Alphanumeric lookarounds instead of \b, so terms like "C++" or ".NET" still match.
    pattern = rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _any_substring(value: str | None, terms: list[str]) -> bool:
    value = (value or "").lower()
    return any(term.lower() in value for term in terms)


def _place_name(location: str) -> str:
    return location.split(",")[0].strip().lower()


def _location_matches(job: Job, locations: list[str], job_province: str | None) -> bool:
    """A watch location matches by city ("Victoria, BC" ~ Adzuna's "Victoria,
    Capital"), by province ("Ontario" / "ON" ~ any job in Ontario), or "Remote"."""
    job_location = (job.location or "").lower()
    for location in locations:
        if location.strip().lower() == "remote":
            if job.remote_type == "remote" or "remote" in job_location:
                return True
            continue
        if (code := province_code(location)) and code == job_province:
            return True
        name = _place_name(location)
        if name and re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", job_location):
            return True
    return False


def _salary_matches(watch: Watch, job: Job) -> bool:
    """Jobs without a listed salary pass -- most postings don't state one."""
    low, high = job.salary_min, job.salary_max
    if low is None and high is None:
        return True
    if watch.min_salary is not None and (high or low) < watch.min_salary:
        return False
    if watch.max_salary is not None and (low or high) > watch.max_salary:
        return False
    return True


def _job_type_matches(watch: Watch, job: Job) -> bool:
    if not watch.job_types or not job.employment_type:
        return True  # unknown type passes
    return any(kind in job.employment_type.split() for kind in watch.job_types)


def _recent_enough(watch: Watch, job: Job) -> bool:
    if not watch.posted_within_days or job.date_posted is None:
        return True
    posted = job.date_posted if job.date_posted.tzinfo else job.date_posted.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - posted <= timedelta(days=watch.posted_within_days)


def watch_matches(watch: Watch, job: Job, score: int | None = None, job_province: str | None = None) -> bool:
    """Does `job` satisfy every filter `watch` sets? `score` is the job's match
    score (needed for `min_score`); `job_province` its province code, if known."""
    text = f"{job.title}\n{job.description}".lower()
    if watch.exclude_keywords and any(_contains_word(text, kw) for kw in watch.exclude_keywords):
        return False
    if watch.exclude_companies and _any_substring(job.company, watch.exclude_companies):
        return False
    if watch.companies and not _any_substring(job.company, watch.companies):
        return False
    if watch.titles and not _any_substring(job.title, watch.titles):
        return False
    if watch.keywords and not any(_contains_word(text, kw) for kw in watch.keywords):
        return False
    if watch.locations and not _location_matches(job, watch.locations, job_province or province_code_of(job)):
        return False
    if not (_salary_matches(watch, job) and _job_type_matches(watch, job) and _recent_enough(watch, job)):
        return False
    if watch.min_score is not None and (score is None or score < watch.min_score):
        return False
    return True


def province_code_of(job: Job) -> str | None:
    return province_of(None, job.location)


def format_alert(job: Job, watch_names: list[str], score: int | None, dashboard_url: str | None) -> str:
    e = html.escape
    lines = [f"🔔 <b>{e(job.title)}</b>", f"🏢 {e(job.company)}"]
    if job.location:
        lines.append(f"📍 {e(job.location)}")
    if job.salary_min or job.salary_max:
        low, high = job.salary_min, job.salary_max
        amount = f"{low:,}" if low and (not high or high == low) else f"{(low or 0):,} – {high:,}"
        lines.append(f"💰 {amount} {job.currency or ''}".rstrip())
    if job.employment_type:
        lines.append(f"🕒 {e(job.employment_type.replace('_', '-').replace(' ', ', '))}")
    if score is not None:
        lines.append(f"📊 Match score: {score}/100")
    lines.append(f"👀 Watch: {e(', '.join(watch_names))}")
    lines.append(f'<a href="{e(job.source_url, quote=True)}">View posting</a>')
    if dashboard_url:
        lines.append(f'<a href="{e(dashboard_url.rstrip("/"), quote=True)}/jobs/{job.id}">Open in dashboard</a>')
    return "\n".join(lines)


# The same role (company + title) alerts at most once in this window, even if it's
# reposted or listed in several cities.
DUPLICATE_WINDOW = timedelta(days=30)
RETRY_WINDOW = timedelta(hours=24)


def alert_key(job: Job) -> str:
    """Company + title, normalized: "Senior Java Developer - Vice President" and
    "Senior Java Developer, Vice President" at Citigroup are one role."""
    return f"{normalize_term(job.company)}|{normalize_term(job.title)}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # matches the DB's naive UTC timestamps


class WatchlistAlerter:
    """One instance per scheduler run, so `max_alerts_per_run` caps a single run.

    With a `session`, every decision is written to `watchlist_alerts` -- sent,
    failed, rolled into a summary, or skipped as a duplicate -- and that log is
    what stops the same role being alerted twice (see `alert_key`).
    """

    def __init__(
        self, config: WatchlistConfig, client: TelegramClient, chat_id: str,
        province_resolver: Callable[[Job], str | None] | None = None, session: Session | None = None,
    ) -> None:
        self._config = config
        self._province = province_resolver or province_code_of
        self._client = client
        self._chat_id = chat_id
        self._session = session
        self._sent = 0
        self._overflow: list[tuple[Job, str, list[str], int | None]] = []
        self._this_run: dict[str, int] = {}  # alert_key -> job id alerted in this run

    # ---- log ----------------------------------------------------------------
    def _record(self, job: Job, key: str, watches: list[str], score: int | None, status: str,
                detail: str | None = None) -> None:
        if self._session is None:
            return
        self._session.add(WatchlistAlert(job_id=job.id, job_title=job.title, job_company=job.company, alert_key=key,
                                         watches=watches, score=score, status=status, detail=detail))
        self._session.commit()

    def _already_alerted(self, key: str, job: Job) -> WatchlistAlert | None:
        if self._session is None:
            return None
        return (
            self._session.query(WatchlistAlert)
            .filter(WatchlistAlert.alert_key == key, WatchlistAlert.status.in_(("sent", "summarized")),
                    WatchlistAlert.created_at >= _utcnow() - DUPLICATE_WINDOW)
            .order_by(WatchlistAlert.created_at.desc())
            .first()
        )

    # ---- alerts ---------------------------------------------------------------
    def consider(self, job: Job, score: int | None = None) -> None:
        province = self._province(job)
        matched = [watch.name for watch in self._config.watches if watch_matches(watch, job, score, province)]
        if not matched:
            return
        key = alert_key(job)
        if key in self._this_run:
            self._record(job, key, matched, score, "duplicate", f"same role as job #{self._this_run[key]} in this scan")
            return
        previous = self._already_alerted(key, job)
        if previous is not None:
            self._record(job, key, matched, score, "duplicate",
                         f"already alerted for job #{previous.job_id} on {previous.created_at:%b %d}")
            return
        self._this_run[key] = job.id
        if self._sent >= self._config.max_alerts_per_run:
            self._overflow.append((job, key, matched, score))
            return
        try:
            self._client.send_message(self._chat_id, format_alert(job, matched, score, self._config.dashboard_url))
        except Exception as exc:
            # A Telegram outage must never break the discovery run that triggered it;
            # the failed alert is retried at the start of the next scan.
            logger.warning("Telegram watchlist alert failed for job_id=%s", job.id, exc_info=True)
            self._record(job, key, matched, score, "failed", type(exc).__name__)
            return
        self._sent += 1
        self._record(job, key, matched, score, "sent")
        log_event(logger, "WATCHLIST_ALERT_SENT", job_id=job.id, watches=matched)

    def flush(self) -> None:
        """Send one roll-up message for alerts skipped by `max_alerts_per_run`."""
        if not self._overflow:
            return
        e = html.escape
        lines = [f"➕ <b>{len(self._overflow)} more watchlist matches</b> this scan:"]
        lines += [f"• {e(job.title)} @ {e(job.company)}" for job, *_ in self._overflow[:30]]
        if len(self._overflow) > 30:
            lines.append("…see the dashboard for the rest.")
        try:
            self._client.send_message(self._chat_id, "\n".join(lines))
            status, detail = "summarized", "in a roll-up message"
        except Exception as exc:
            logger.warning("Telegram watchlist summary failed", exc_info=True)
            status, detail = "failed", type(exc).__name__
        for job, key, matched, score in self._overflow:
            self._record(job, key, matched, score, status, detail)
        self._overflow = []

    def retry_failed(self) -> int:
        """Re-send alerts that failed in the last day (e.g. Telegram was down). Returns how many went out."""
        if self._session is None:
            return 0
        failed = (
            self._session.query(WatchlistAlert)
            .filter(WatchlistAlert.status == "failed", WatchlistAlert.created_at >= _utcnow() - RETRY_WINDOW)
            .order_by(WatchlistAlert.created_at)
            .limit(self._config.max_alerts_per_run)
            .all()
        )
        resent = 0
        for row in failed:
            job = self._session.get(Job, row.job_id)
            if job is None or self._already_alerted(row.alert_key, job) is not None:
                row.status, row.detail = "duplicate", "already alerted meanwhile"
                continue
            try:
                self._client.send_message(self._chat_id, format_alert(job, row.watches, row.score, self._config.dashboard_url))
            except Exception:
                logger.warning("Retrying watchlist alert for job_id=%s failed again", row.job_id, exc_info=True)
                break  # Telegram still down: try again next scan
            row.status, row.detail = "sent", "sent on retry"
            resent += 1
        self._session.commit()
        return resent


def build_watchlist_alerter(
    config: AppConfig, settings: TelegramSettings | None = None,
    province_resolver: Callable[[Job], str | None] | None = None, session: Session | None = None,
) -> WatchlistAlerter | None:
    watchlist = config.watchlist
    if not watchlist.enabled or not watchlist.watches:
        return None
    settings = settings or TelegramSettings()
    # Only the user's own linked chat (Watchlist -> Connect Telegram) -- never a
    # shared fallback, which would send one user's alerts to someone else.
    chat_id = watchlist.telegram_chat_id
    if not (settings.bot_token and chat_id):
        logger.info("Watchlist alerts are on but Telegram isn't connected for this user -- skipping")
        return None
    return WatchlistAlerter(watchlist, TelegramClient(settings.bot_token), chat_id, province_resolver, session)
