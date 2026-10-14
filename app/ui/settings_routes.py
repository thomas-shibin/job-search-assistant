"""Settings + Watchlist pages for the signed-in member: their own settings
sections and watches (`app/config/store.py` keeps each user's in their own
rows). Platform settings -- provider, schedule, retention, limits -- are on the
admin pages (app/admin/routes.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import member_session
from app.catalog.store import remember_user_terms
from app.core.config.loader import ConfigError
from app.core.config.schema import AppConfig
from app.core.config.store import USER_SECTIONS, WATCH_LIST_FIELDS, load_app_config, save_section, save_watch
from app.models.job import Job
from app.models.settings import WatchlistAlert, WatchlistEntry
from app.notifications.telegram import TelegramSettings
from app.services.search import queries_per_scan, scan_specs
from app.services.telegram import disconnect, send_test_message, telegram_link
from app.ui.forms import parse_form, section_form
from app.ui.routes import DEFAULT_CONFIG_PATH, templates

router = APIRouter()

# Settings page sections, in display order, with a one-line description each.
# `watchlist` has its own page.
SETTINGS_SECTIONS: list[tuple[str, str]] = [
    ("search", "Your default job search -- prefilled on Find jobs and used for your automatic scans"),
    ("matching", "How jobs are scored against your resume"),
    ("resume", "How tailored resumes are written"),
    ("address_profiles", "Addresses shown on tailored resumes"),
    ("search_profiles", "Company career boards to watch, and the address used on resumes for them"),
    ("candidate", "Your name on tailored resumes"),
]
# Fields a member doesn't set: platform-controlled, or filled in by the app.
_HIDDEN_FIELDS = {
    "search": ("adzuna_daily_limit",),
    "candidate": ("base_resume",),
    "watchlist": ("watches", "dashboard_url", "telegram_chat_id"),
}


def _redirect(path: str, **params: str) -> RedirectResponse:
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{path}{query}", status_code=303)


def _load(session: Session) -> AppConfig:
    try:
        return load_app_config(session, DEFAULT_CONFIG_PATH)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Your settings are invalid: {'; '.join(exc.errors)}")


def _settings_response(request: Request, session: Session, config: AppConfig, *, error: str | None = None,
                       error_section: str | None = None, message: str | None = None, status_code: int = 200):
    dumped = config.model_dump(mode="json")
    sections = [
        {"key": key, "description": description,
         "form": section_form(AppConfig.model_fields[key].annotation, key, dumped[key],
                              skip=_HIDDEN_FIELDS.get(key, ()))}
        for key, description in SETTINGS_SECTIONS
    ]
    warnings = []
    pairs = len(scan_specs(config))
    if pairs:
        per_scan = queries_per_scan(pairs, config.scheduler.interval_minutes, config.search.adzuna_daily_limit)
        if per_scan < pairs:
            hours = config.scheduler.interval_minutes * -(-pairs // per_scan) / 60
            warnings.append(
                f"Your automatic scans have {pairs} searches (each title × location in your job search, plus "
                f"your watches'), more than fit in one scan, so they take turns -- each is checked about every "
                f"{hours:g} hours. Fewer titles, locations or watches checks each more often."
            )
    return templates.TemplateResponse(
        request, "settings.html",
        {"sections": sections, "error": error, "error_section": error_section, "message": message,
         "warnings": warnings},
        status_code=status_code,
    )


@router.get("/settings")
def settings_page(request: Request, msg: str | None = None, session: Session = Depends(member_session)):
    return _settings_response(request, session, _load(session), message=msg)


@router.post("/settings/{section}")
async def save_settings_section(request: Request, section: str, session: Session = Depends(member_session)):
    if section not in USER_SECTIONS:
        raise HTTPException(status_code=404, detail="Unknown settings section")
    form = await request.form()
    parsed = parse_form((k, v) for k, v in form.multi_items() if isinstance(v, str))
    value = parsed.get(section)
    current = _load(session).model_dump(mode="json")[section]
    if isinstance(current, dict) and isinstance(value or {}, dict):  # keep fields the form doesn't show
        kept = {k: current[k] for k in _HIDDEN_FIELDS.get(section, ()) if k in current and k != "watches"}
        value = {**kept, **(value or {})}
    try:
        config = save_section(session, section, value)
    except ConfigError as exc:
        error = "; ".join(exc.errors)
        if section == "watchlist":
            return _watchlist_response(request, session, error=error, status_code=400)
        return _settings_response(request, session, _load(session), error=error, error_section=section, status_code=400)

    if section == "search":
        remember_user_terms(session, title=config.search.titles, location=config.search.locations,
                            keyword=config.search.keywords)
    if section == "watchlist":
        return _redirect("/watchlist", msg="Saved.")
    return RedirectResponse(url=f"/settings?{urlencode({'msg': 'Saved.'})}#{section}", status_code=303)


# ---- Watchlist -------------------------------------------------------------

def _watchlist_response(request: Request, session: Session, *, error: str | None = None,
                        message: str | None = None, edit: WatchlistEntry | None = None, status_code: int = 200):
    config = _load(session)
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
                config.watchlist.model_dump(mode="json"), skip=_HIDDEN_FIELDS["watchlist"],
            ),
            "token_set": bool(telegram.bot_token), "chat_id": chat_id,
            "watchlist_enabled": config.watchlist.enabled,
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


def _watch_fields(form) -> dict:
    def values(name: str) -> list[str]:  # picker tags (one field each) or one-per-line text
        items = (line.strip() for value in form.getlist(name) for line in str(value).splitlines())
        return list(dict.fromkeys(item for item in items if item))

    def number(name: str) -> int | str | None:
        raw = str(form.get(name, "")).replace(",", "").strip()
        if not raw:
            return None
        return int(raw) if raw.isdigit() else raw  # non-numbers are left for validation to reject

    return {
        "name": str(form.get("name", "")).strip(),
        "enabled": form.get("enabled") == "true",
        **{field: values(field) for field in WATCH_LIST_FIELDS},
        "min_score": number("min_score"),
        "min_salary": number("min_salary"),
        "max_salary": number("max_salary"),
        "posted_within_days": number("posted_within_days"),
        # hidden "false" + checkbox "true": ticked when "true" was sent
        "search": "true" in form.getlist("search") if "search" in form else True,
    }


def _get_entry_or_404(session: Session, entry_id: int) -> WatchlistEntry:
    entry = session.get(WatchlistEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No watch with id={entry_id}")
    return entry


@router.get("/watchlist")
def watchlist_page(request: Request, msg: str | None = None, edit: int | None = None,
                   session: Session = Depends(member_session)):
    entry = _get_entry_or_404(session, edit) if edit is not None else None
    return _watchlist_response(request, session, message=msg, edit=entry)


@router.post("/watchlist/watches")
async def create_watch(request: Request, session: Session = Depends(member_session)):
    return await _save_watch(request, session, WatchlistEntry(), "Watch added.")


@router.post("/watchlist/watches/{entry_id}")
async def update_watch(request: Request, entry_id: int, session: Session = Depends(member_session)):
    return await _save_watch(request, session, _get_entry_or_404(session, entry_id), "Watch updated.")


async def _save_watch(request: Request, session: Session, entry: WatchlistEntry, message: str):
    try:
        fields = _watch_fields(await request.form())
        save_watch(session, entry, **fields)
    except ConfigError as exc:
        session.rollback()
        return _watchlist_response(request, session, error="; ".join(exc.errors),
                                   edit=entry if entry.id else None, status_code=400)
    remember_user_terms(
        session, company=fields["companies"] + fields["exclude_companies"], title=fields["titles"],
        location=fields["locations"],
        keyword=fields["keywords"] + fields["exclude_keywords"],
    )
    return _redirect("/watchlist", msg=message)


@router.post("/watchlist/watches/{entry_id}/toggle")
def toggle_watch(entry_id: int, session: Session = Depends(member_session)):
    entry = _get_entry_or_404(session, entry_id)
    entry.enabled = not entry.enabled
    session.commit()
    return _redirect("/watchlist", msg=f"{entry.name}: {'on' if entry.enabled else 'paused'}.")


@router.post("/watchlist/watches/{entry_id}/delete")
def delete_watch(entry_id: int, session: Session = Depends(member_session)):
    entry = _get_entry_or_404(session, entry_id)
    session.delete(entry)
    session.commit()
    return _redirect("/watchlist", msg=f"Deleted {entry.name}.")


@router.post("/watchlist/telegram/connect")
def telegram_connect(session: Session = Depends(member_session)):
    """Open Telegram on a single-use link that connects *this* account's chat."""
    try:
        link = telegram_link(session)
    except Exception:  # TelegramError, or a network failure
        return _redirect("/watchlist", msg="Telegram alerts aren't available right now. Please try again later.")
    return RedirectResponse(url=link, status_code=303)


@router.post("/watchlist/telegram/disconnect")
def telegram_disconnect(session: Session = Depends(member_session)):
    disconnect(session, _load(session))
    return _redirect("/watchlist", msg="Telegram disconnected.")


@router.post("/watchlist/telegram/test")
def telegram_test(session: Session = Depends(member_session)):
    try:
        send_test_message(_load(session))
    except Exception:  # TelegramError, or a network failure
        return _redirect("/watchlist", msg="Couldn't send a test message. Reconnect Telegram and try again.")
    return _redirect("/watchlist", msg="Test message sent -- check Telegram.")
