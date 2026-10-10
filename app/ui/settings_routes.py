"""Settings + Watchlist dashboard pages: every `AppConfig` section is edited
here and saved to the database (`app/config/store.py`) -- config.yaml is only
a one-time import source. Only secrets (API keys, the Telegram bot token)
stay in .env, and those are shown as set/missing, never displayed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai.settings import AISettings
from app.catalog.scan import scan_status, start_background_scan
from app.catalog.store import counts as catalog_counts, remember_user_terms
from app.config.loader import ConfigError, validate_config
from app.config.schema import AppConfig
from app.config.store import WATCH_LIST_FIELDS, load_app_config, save_section, save_watch
from app.database.session import SessionLocal, get_session
from app.models.job import Job
from app.models.settings import WatchlistAlert, WatchlistEntry
from app.notifications.telegram import TelegramSettings
from app.scheduler import background
from app.services.search import queries_per_scan, scan_specs
from app.services.telegram import detect_and_save_chat_id, send_test_message
from app.sources.adzuna import AdzunaSource
from app.sources.settings import AdzunaSettings
from app.ui.forms import parse_form, section_form
from app.ui.routes import DEFAULT_CONFIG_PATH, PROJECT_ROOT, templates

router = APIRouter()

# Settings page sections, in display order, with a one-line description each.
# `watchlist` has its own page.
SETTINGS_SECTIONS: list[tuple[str, str]] = [
    ("search", "Job search conditions -- applied to the Jobs page search and to scheduled scans"),
    ("scheduler", "Automatic scans"),
    ("search_profiles", "Company job boards (Greenhouse/Lever) to watch, and the address used on tailored resumes"),
    ("matching", "How jobs are scored and filtered"),
    ("notifications", "Desktop notifications for qualified jobs"),
    ("candidate", "You and your base resume"),
    ("address_profiles", "Addresses used on tailored resumes"),
    ("ai", "AI provider"),
    ("resume", "Resume tailoring options"),
]


def _redirect(path: str, **params: str) -> RedirectResponse:
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{path}{query}", status_code=303)


def _load(session: Session) -> AppConfig:
    try:
        return load_app_config(session, DEFAULT_CONFIG_PATH)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Stored settings are invalid: {'; '.join(exc.errors)}")


def _settings_response(request: Request, session: Session, config: AppConfig, *, error: str | None = None,
                       error_section: str | None = None, message: str | None = None, status_code: int = 200):
    dumped = config.model_dump(mode="json")
    sections = [
        {"key": key, "description": description,
         "form": section_form(AppConfig.model_fields[key].annotation, key, dumped[key])}
        for key, description in SETTINGS_SECTIONS
    ]
    secrets = {
        "OPENAI_API_KEY": bool(AISettings().openai_api_key),
        "ADZUNA_APP_ID / ADZUNA_APP_KEY": bool(AdzunaSettings().app_id and AdzunaSettings().app_key),
        "TELEGRAM_BOT_TOKEN": bool(TelegramSettings().bot_token),
    }
    warnings = validate_config(config, base_dir=PROJECT_ROOT)
    pairs = len(scan_specs(config))
    if pairs:
        per_scan = queries_per_scan(pairs, config.scheduler.interval_minutes, config.search.adzuna_daily_limit)
        if per_scan < pairs:
            hours = config.scheduler.interval_minutes * -(-pairs // per_scan) / 60
            warnings.append(
                f"Scheduled scans have {pairs} searches to run (your job search's title × location combinations "
                f"plus your watches') but the Adzuna daily limit allows {per_scan} per scan, so they rotate -- each "
                f"is checked about every {hours:g} hours. Fewer titles/locations/watches or a longer interval "
                f"checks each more often."
            )
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "sections": sections, "error": error, "error_section": error_section, "message": message,
            "warnings": warnings, "secrets": secrets,
            "scheduler_running": background.is_running(),
            "next_scan": background.next_scan_time(),
            "catalog": {"counts": catalog_counts(session), **scan_status()},
        },
        status_code=status_code,
    )


@router.get("/settings")
def settings_page(request: Request, msg: str | None = None, session: Session = Depends(get_session)):
    return _settings_response(request, session, _load(session), message=msg)


@router.post("/settings/scan-now")
def scan_now():
    if not background.trigger_scan_now():
        return _redirect("/settings", msg="Scans only run under `python cli.py serve` (or Docker).")
    return _redirect("/settings", msg="Scan started in the background -- new jobs will appear on the Jobs page.")


@router.post("/settings/catalog-scan")
def catalog_scan(session: Session = Depends(get_session)):
    """Refresh the search-term catalog from a quick scan of the job market (~50 Adzuna requests)."""
    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        return _redirect("/settings", msg="Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env first.")
    country = _load(session).search.country
    adzuna = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
    if not start_background_scan(SessionLocal, adzuna, country):
        return _redirect("/settings", msg="A catalog scan is already running.")
    return RedirectResponse(
        url=f"/settings?{urlencode({'msg': 'Catalog scan started -- it takes about 2 minutes.'})}#catalog",
        status_code=303,
    )


@router.post("/settings/{section}")
async def save_settings_section(request: Request, section: str, session: Session = Depends(get_session)):
    if section not in AppConfig.model_fields:
        raise HTTPException(status_code=404, detail=f"Unknown settings section {section!r}")
    form = await request.form()
    parsed = parse_form((k, v) for k, v in form.multi_items() if isinstance(v, str))
    value = parsed.get(section)
    if section == "watchlist":
        # The watchlist form doesn't carry the watches (they're separate rows) or the detected chat id.
        current = _load(session).watchlist.model_dump(exclude={"watches"})
        value = {**current, **(value or {})}
    try:
        config = save_section(session, section, value)
    except ConfigError as exc:
        error = "; ".join(exc.errors)
        if section == "watchlist":
            return _watchlist_response(request, session, error=error, status_code=400)
        return _settings_response(request, session, _load(session), error=error, error_section=section, status_code=400)

    background.apply_schedule(config)
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
    chat_id = config.watchlist.telegram_chat_id or telegram.chat_id
    return templates.TemplateResponse(
        request, "watchlist.html",
        {
            "entries": session.query(WatchlistEntry).order_by(WatchlistEntry.id).all(),
            "alerts": _recent_alerts(session),
            "alert_counts": _alert_counts(session),
            "list_fields": WATCH_LIST_FIELDS,
            "settings_form": section_form(
                AppConfig.model_fields["watchlist"].annotation, "watchlist",
                config.watchlist.model_dump(mode="json"), skip=("watches",),
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
        .join(Job, Job.id == WatchlistAlert.job_id)
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
                   session: Session = Depends(get_session)):
    entry = _get_entry_or_404(session, edit) if edit is not None else None
    return _watchlist_response(request, session, message=msg, edit=entry)


@router.post("/watchlist/watches")
async def create_watch(request: Request, session: Session = Depends(get_session)):
    return await _save_watch(request, session, WatchlistEntry(), "Watch added.")


@router.post("/watchlist/watches/{entry_id}")
async def update_watch(request: Request, entry_id: int, session: Session = Depends(get_session)):
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
def toggle_watch(entry_id: int, session: Session = Depends(get_session)):
    entry = _get_entry_or_404(session, entry_id)
    entry.enabled = not entry.enabled
    session.commit()
    return _redirect("/watchlist", msg=f"{entry.name}: {'on' if entry.enabled else 'paused'}.")


@router.post("/watchlist/watches/{entry_id}/delete")
def delete_watch(entry_id: int, session: Session = Depends(get_session)):
    entry = _get_entry_or_404(session, entry_id)
    session.delete(entry)
    session.commit()
    return _redirect("/watchlist", msg=f"Deleted {entry.name}.")


@router.post("/watchlist/telegram/detect")
def telegram_detect(session: Session = Depends(get_session)):
    try:
        chat = detect_and_save_chat_id(session, _load(session))
    except Exception as exc:  # TelegramError, ConfigError, or a network failure
        return _redirect("/watchlist", msg=f"Could not detect chat id: {exc}")
    who = chat.get("username") or chat.get("first_name") or chat.get("title") or chat["id"]
    return _redirect("/watchlist", msg=f"Connected to Telegram chat {chat['id']} ({who}).")


@router.post("/watchlist/telegram/test")
def telegram_test(session: Session = Depends(get_session)):
    try:
        send_test_message(_load(session))
    except Exception as exc:  # TelegramError, or a network failure
        return _redirect("/watchlist", msg=f"Test message failed: {exc}")
    return _redirect("/watchlist", msg="Test message sent -- check Telegram.")
