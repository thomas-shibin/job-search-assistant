"""Telegram bot commands for `python cli.py serve`: send the bot `/health` and
it replies with the app's health report (`app/services/health.py`).

The dashboard runs on your own machine, so Telegram can't call it (no
webhook); instead a background thread long-polls `getUpdates`. It only polls
once a chat id is saved (Watchlist -> Detect chat ID) -- until then "Detect
chat ID" needs those messages left unread -- and it only answers that chat, so
nobody else who finds the bot can query your app.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from app.notifications.telegram import TelegramClient, TelegramSettings

logger = logging.getLogger(__name__)

_POLL_SECONDS = 25
_IDLE_SECONDS = 30  # no token / chat id yet: re-check settings this often
_ERROR_BACKOFF_SECONDS = 30
_MAX_COMMAND_AGE_SECONDS = 300  # don't answer a backlog from while the app was off

HELP_TEXT = (
    "<b>Job Search Assistant</b>\n"
    "/health — check the app and get a status update\n"
    "/help — this list"
)


def _command(text: str) -> str:
    """'/health@MyBot extra' -> '/health'."""
    return text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""


def reply_for(update: dict, chat_id: str, health_text: Callable[[], str], *, now: float | None = None) -> str | None:
    """The reply to one update, or None to ignore it (another chat, too old,
    not a command)."""
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != str(chat_id):
        return None
    if (now if now is not None else time.time()) - message.get("date", 0) > _MAX_COMMAND_AGE_SECONDS:
        return None
    command = _command(message.get("text") or "")
    if command == "/health":
        return health_text()
    if command in ("/help", "/start"):
        return HELP_TEXT
    if command.startswith("/"):
        return f"Unknown command {command}.\n\n{HELP_TEXT}"
    return None


def poll_once(client: TelegramClient, offset: int | None, chat_id: str,
              health_text: Callable[[], str], *, poll_seconds: int = _POLL_SECONDS) -> int | None:
    """Answer whatever arrived; returns the next offset."""
    for update in client.get_updates(offset, poll_seconds):
        offset = update["update_id"] + 1
        try:
            reply = reply_for(update, chat_id, health_text)
            if reply:
                client.send_message(chat_id, reply)
        except Exception:
            logger.warning("Could not answer Telegram command", exc_info=True)
    return offset


def _health_text(config_path: Path) -> str:
    from app.database.session import SessionLocal
    from app.services.health import check_health, format_health_message

    with SessionLocal() as session:
        return format_health_message(check_health(session, config_path))


def _chat_id(config_path: Path, settings: TelegramSettings) -> str | None:
    from app.config.store import load_app_config
    from app.database.session import SessionLocal

    with SessionLocal() as session:
        return load_app_config(session, config_path).watchlist.telegram_chat_id or settings.chat_id


class TelegramCommandListener:
    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="telegram-commands", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        offset: int | None = None
        client: TelegramClient | None = None
        token: str | None = None
        while not self._stop.is_set():
            try:
                settings = TelegramSettings()
                chat_id = _chat_id(self._config_path, settings) if settings.bot_token else None
                if not (settings.bot_token and chat_id):
                    self._stop.wait(_IDLE_SECONDS)
                    continue
                if client is None or settings.bot_token != token:
                    token, client, offset = settings.bot_token, TelegramClient(settings.bot_token), None
                offset = poll_once(client, offset, chat_id, lambda: _health_text(self._config_path))
            except Exception:
                # Never log the request URL -- it embeds the bot token (TelegramError omits it).
                logger.warning("Telegram command polling failed; retrying in %ss", _ERROR_BACKOFF_SECONDS,
                               exc_info=True)
                self._stop.wait(_ERROR_BACKOFF_SECONDS)


def start_telegram_command_listener(config_path: Path) -> TelegramCommandListener:
    listener = TelegramCommandListener(config_path)
    listener.start()
    return listener
