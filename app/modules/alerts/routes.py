"""The Watchlist page's actions: add, edit, pause and delete watches (saved
searches that the scheduled scans check), and connect, test or disconnect the
user's Telegram chat for alerts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.loader import ConfigError
from app.core.config.models import WatchlistEntry
from app.core.config.store import WATCH_LIST_FIELDS, save_watch
from app.modules.alerts.telegram_link import disconnect, send_test_message, telegram_link
from app.modules.alerts.views import watchlist_response
from app.modules.auth.deps import member_session
from app.modules.jobs.catalog.store import remember_user_terms
from app.web.responses import load_config_or_500, redirect

router = APIRouter()


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
    return watchlist_response(request, session, message=msg, edit=entry)


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
        return watchlist_response(request, session, error="; ".join(exc.errors),
                                   edit=entry if entry.id else None, status_code=400)
    remember_user_terms(
        session, company=fields["companies"] + fields["exclude_companies"], title=fields["titles"],
        location=fields["locations"],
        keyword=fields["keywords"] + fields["exclude_keywords"],
    )
    return redirect("/watchlist", msg=message)


@router.post("/watchlist/watches/{entry_id}/toggle")
def toggle_watch(entry_id: int, session: Session = Depends(member_session)):
    entry = _get_entry_or_404(session, entry_id)
    entry.enabled = not entry.enabled
    session.commit()
    return redirect("/watchlist", msg=f"{entry.name}: {'on' if entry.enabled else 'paused'}.")


@router.post("/watchlist/watches/{entry_id}/delete")
def delete_watch(entry_id: int, session: Session = Depends(member_session)):
    entry = _get_entry_or_404(session, entry_id)
    session.delete(entry)
    session.commit()
    return redirect("/watchlist", msg=f"Deleted {entry.name}.")


@router.post("/watchlist/telegram/connect")
def telegram_connect(session: Session = Depends(member_session)):
    """Open Telegram on a single-use link that connects *this* account's chat."""
    try:
        link = telegram_link(session)
    except Exception:  # TelegramError, or a network failure
        return redirect("/watchlist", msg="Telegram alerts aren't available right now. Please try again later.")
    return RedirectResponse(url=link, status_code=303)


@router.post("/watchlist/telegram/disconnect")
def telegram_disconnect(session: Session = Depends(member_session)):
    disconnect(session, load_config_or_500(session))
    return redirect("/watchlist", msg="Telegram disconnected.")


@router.post("/watchlist/telegram/test")
def telegram_test(session: Session = Depends(member_session)):
    try:
        send_test_message(load_config_or_500(session))
    except Exception:  # TelegramError, or a network failure
        return redirect("/watchlist", msg="Couldn't send a test message. Reconnect Telegram and try again.")
    return redirect("/watchlist", msg="Test message sent -- check Telegram.")
