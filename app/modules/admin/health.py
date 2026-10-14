"""Platform health report for admins: the /admin/health page and the Telegram
`/health` command (admins only). Everything here is local and free: it reads the database
and settings and never calls a billed or rate-limited API (Adzuna, OpenAI), so
it is safe to ask as often as you like.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from sqlalchemy import func, text
from sqlalchemy.orm import Session

OK, WARN, FAIL = "ok", "warn", "fail"
_ICONS = {OK: "✅", WARN: "⚠️", FAIL: "❌"}

_STARTED = time.monotonic()
from app.core.paths import DEFAULT_CONFIG_PATH as _DEFAULT_CONFIG_PATH


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str


@dataclass
class HealthReport:
    checks: list[Check] = field(default_factory=list)
    uptime_seconds: int = 0

    @property
    def status(self) -> str:
        """down if the database is unreachable, degraded on any other problem."""
        if any(c.status == FAIL and c.name == "Database" for c in self.checks):
            return "down"
        return "degraded" if any(c.status != OK for c in self.checks) else "ok"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "uptime_seconds": self.uptime_seconds,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks],
        }


def _utcnow() -> datetime:
    # The DB stores naive UTC timestamps (server_default=func.now()).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ago(when: datetime) -> str:
    return _duration((_utcnow() - when).total_seconds()) + " ago"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400} days"


def _check(name: str, fn) -> Check:
    """One check; a crash in it is reported, never raised."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- a health report must always render
        return Check(name, FAIL, f"check failed: {exc.__class__.__name__}: {exc}")


def _database(session: Session) -> Check:
    from app.modules.jobs.models import Job
    from app.modules.auth.models import Role, User

    session.execute(text("SELECT 1"))
    members = session.query(func.count(User.id)).filter(User.role != Role.GUEST.value).scalar()
    jobs = session.query(func.count(Job.id)).scalar()
    return Check("Database", OK, f"reachable, {members} accounts, {jobs} jobs tracked")


def _scheduler(session: Session, config) -> Check:
    from app.workers.models import SchedulerRun, SourceRunResult
    from app.workers.background import is_running, next_scan_time

    last = session.query(SchedulerRun).order_by(SchedulerRun.id.desc()).first()
    if last is None:
        last_text, last_status = "no scan has run yet", WARN
    else:
        when = last.finished_at or last.started_at
        last_text = f"last scan {last.status.lower().replace('_', ' ')} {_ago(when)}"
        last_status = OK
        if last.status == "COMPLETED_WITH_ERRORS":
            failed = [r.source_name for r in session.query(SourceRunResult)
                      .filter_by(scheduler_run_id=last.id, status="FAILED")]
            last_text += f" (failed: {', '.join(failed) or 'some sources'})"
            last_status = WARN
        elif last.status == "RUNNING" and _utcnow() - last.started_at > timedelta(hours=1):
            last_text = f"a scan started {_ago(last.started_at)} never finished"
            last_status = WARN
        elif last.finished_at and config is not None and config.scheduler.enabled and (
            _utcnow() - last.finished_at > timedelta(minutes=config.scheduler.interval_minutes * 3)
        ):
            last_status = WARN  # scans are on but haven't run for three intervals

    if not is_running():
        return Check("Scheduler", WARN, f"not running in this process (start with `cli.py serve`); {last_text}")
    if config is not None and not config.scheduler.enabled:
        return Check("Scheduler", WARN, f"paused in Settings; {last_text}")
    detail = last_text
    next_at = next_scan_time()
    if next_at is not None:
        detail += f"; next in {_duration((next_at - datetime.now(next_at.tzinfo)).total_seconds())}"
    return Check("Scheduler", last_status, detail)


def _accounts(session: Session) -> Check:
    from app.modules.resumes.models import CandidateProfile
    from app.modules.auth.models import Role, User

    members = session.query(func.count(User.id)).filter(User.role != Role.GUEST.value).scalar()
    unverified = session.query(func.count(User.id)).filter(
        User.role != Role.GUEST.value, User.email_verified_at.is_(None)).scalar()
    guests = session.query(func.count(User.id)).filter(User.role == Role.GUEST.value).scalar()
    with_resume = session.query(func.count(func.distinct(CandidateProfile.user_id))).filter(
        CandidateProfile.is_active.is_(True)).scalar()
    return Check("Accounts", OK, f"{members} members ({unverified} unconfirmed), {with_resume} with a resume, "
                                 f"{guests} recent guests")


def _api_keys() -> Check:
    from app.integrations.ai.settings import AISettings
    from app.modules.auth.settings import AuthSettings
    from app.integrations.email import EmailSettings
    from app.integrations.telegram import TelegramSettings
    from app.integrations.job_boards.settings import AdzunaSettings

    adzuna = AdzunaSettings()
    auth = AuthSettings()
    keys = {
        "analysis provider": bool(AISettings().openai_api_key),
        "job listings provider": bool(adzuna.app_id and adzuna.app_key),
        "Telegram bot": bool(TelegramSettings().bot_token),
        "outgoing email": EmailSettings().configured,
    }
    missing = [name for name, present in keys.items() if not present]
    social = [p.title() for p in ("google", "facebook") if auth.provider_enabled(p)]
    detail = f"social sign-in: {', '.join(social) or 'none'}"
    if missing:
        return Check("Integrations", WARN, f"not configured: {', '.join(missing)}; {detail}")
    return Check("Integrations", OK, f"all configured; {detail}")


def _pdf() -> Check:
    from app.integrations.libreoffice import _find_soffice

    if _find_soffice():
        return Check("PDF export", OK, "LibreOffice found")
    return Check("PDF export", WARN, "LibreOffice not found -- tailored resumes are DOCX only")


def _activity(session: Session) -> Check:
    from app.modules.usage.ai_calls import AICallLog
    from app.modules.jobs.models import Job
    from app.core.config.models import WatchlistAlert

    since = _utcnow() - timedelta(hours=24)
    new_jobs = session.query(func.count(Job.id)).filter(Job.date_discovered >= since).scalar()
    alerts = session.query(func.count(WatchlistAlert.id)).filter(
        WatchlistAlert.created_at >= since, WatchlistAlert.status == "sent").scalar()
    failed = session.query(func.count(WatchlistAlert.id)).filter(
        WatchlistAlert.created_at >= since, WatchlistAlert.status == "failed").scalar()
    cost = session.query(func.coalesce(func.sum(AICallLog.estimated_cost), 0)).filter(
        AICallLog.created_at >= since).scalar()
    detail = f"{new_jobs} new jobs, {alerts} alerts sent, ${float(cost):.4f} analysis spend"
    if failed:
        return Check("Last 24h", WARN, f"{detail}, {failed} alerts failed to send")
    return Check("Last 24h", OK, detail)


def check_database(session_factory) -> bool:
    """Liveness for the public /health: can we reach the database at all?"""
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def check_health(session: Session, config_path: Path = _DEFAULT_CONFIG_PATH) -> HealthReport:
    """The admin report. `session` must be a system session (platform-wide counts)."""
    from app.core.config.store import load_app_config

    report = HealthReport(uptime_seconds=int(time.monotonic() - _STARTED))
    database = _check("Database", lambda: _database(session))
    report.checks.append(database)
    if database.status == FAIL:
        return report  # everything else reads the database too

    config = None
    try:
        config = load_app_config(session, config_path)
        report.checks.append(Check("Settings", OK, "valid"))
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        report.checks.append(Check("Settings", FAIL, f"invalid -- fix them on the Settings page ({exc})"))

    report.checks += [
        _check("Scheduler", lambda: _scheduler(session, config)),
        _check("Accounts", lambda: _accounts(session)),
        _check("Integrations", _api_keys),
        _check("PDF export", _pdf),
        _check("Last 24h", lambda: _activity(session)),
    ]
    return report


def format_health_message(report: HealthReport) -> str:
    """The report as a Telegram HTML message."""
    headline = {"ok": "✅ All systems OK", "degraded": "⚠️ Running with warnings", "down": "❌ App is down"}
    lines = [f"<b>Job Search Assistant — {headline[report.status]}</b>",
             f"Up {_duration(report.uptime_seconds)}", ""]
    lines += [f"{_ICONS[c.status]} <b>{escape(c.name)}</b>: {escape(c.detail)}" for c in report.checks]
    return "\n".join(lines)
