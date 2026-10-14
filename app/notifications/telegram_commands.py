"""The Telegram bot, for `python cli.py serve`: links each user's chat to their
account, and answers a few commands.

- `/start <code>`: the user tapped "Connect Telegram" on their Watchlist page,
  which opened `t.me/<bot>?start=<code>` -- a single-use code tied to their
  account. The chat is saved as *that user's* alert chat. Nobody can attach a
  chat to someone else's account without that code.
- `/stop`: stop alerts to this chat.
- `/health`: the platform health report -- only for a chat linked to an admin.
- `/help`: the list.

The app runs where Telegram can't reach it (no webhook), so a background thread
long-polls `getUpdates`.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

from app.notifications.telegram import TelegramClient, TelegramSettings

logger = logging.getLogger(__name__)

_POLL_SECONDS = 25
_IDLE_SECONDS = 30  # no bot token yet: re-check this often
_ERROR_BACKOFF_SECONDS = 30
_MAX_COMMAND_AGE_SECONDS = 300  # don't answer a backlog from while the app was off
LINK_TTL = timedelta(minutes=15)

HELP_TEXT = (
    "<b>Job Search Assistant</b>\n"
    "Connect this chat from the Watchlist page in the app to get job alerts here.\n\n"
    "/stop — stop alerts to this chat\n"
    "/help — this list"
)
ADMIN_HELP = "\n/health — platform status (admins)"


def _command(text: str) -> tuple[str, str]:
    """'/start@MyBot abc' -> ('/start', 'abc')."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].split("@", 1)[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


class BotActions:
    """What the bot does with the database -- injectable for tests."""

    def __init__(self, session_factory, config_path: Path) -> None:
        self._factory = session_factory
        self._config_path = config_path

    def link_chat(self, code: str, chat_id: str) -> str | None:
        """Bind `chat_id` to the account that issued `code`; returns their first name."""
        from app.auth.tokens import consume_token
        from app.core.config.store import load_app_config, save_section
        from app.models.user import TokenPurpose, User
        from app.core.tenancy import system_session, tenant_session

        with system_session(self._factory) as session:
            token = consume_token(session, TokenPurpose.TELEGRAM_LINK, code)
            session.commit()
            user = session.get(User, token.user_id) if token and token.user_id else None
            if user is None or not user.is_member:
                return None
            user_id, first_name = user.id, user.first_name
        self.unlink_chat(chat_id)  # a chat belongs to one account at a time
        with tenant_session(self._factory, user_id) as session:
            watchlist = load_app_config(session, self._config_path).watchlist.model_dump(exclude={"watches"})
            save_section(session, "watchlist", {**watchlist, "telegram_chat_id": str(chat_id), "enabled": True})
        return first_name

    def _users_for_chat(self, chat_id: str) -> list[int]:
        from app.models.settings import UserSetting
        from app.core.tenancy import system_session

        with system_session(self._factory) as session:
            rows = session.query(UserSetting.user_id, UserSetting.value).filter(UserSetting.key == "watchlist").all()
        return [uid for uid, value in rows if str((value or {}).get("telegram_chat_id") or "") == str(chat_id)]

    def unlink_chat(self, chat_id: str) -> int:
        from app.core.config.store import load_app_config, save_section
        from app.core.tenancy import tenant_session

        users = self._users_for_chat(chat_id)
        for user_id in users:
            with tenant_session(self._factory, user_id) as session:
                watchlist = load_app_config(session, self._config_path).watchlist.model_dump(exclude={"watches"})
                save_section(session, "watchlist", {**watchlist, "telegram_chat_id": None})
        return len(users)

    def is_admin_chat(self, chat_id: str) -> bool:
        from app.models.user import User
        from app.core.tenancy import system_session

        users = self._users_for_chat(chat_id)
        if not users:
            return False
        with system_session(self._factory) as session:
            return any(u.is_member and u.can("settings.view") for u in session.query(User).filter(User.id.in_(users)))

    def health_text(self) -> str:
        from app.services.health import check_health, format_health_message
        from app.core.tenancy import system_session

        with system_session(self._factory) as session:
            return format_health_message(check_health(session, self._config_path))


def reply_for(update: dict, actions: BotActions, *, now: float | None = None) -> tuple[str, str] | None:
    """(chat id, reply) for one update, or None to ignore it."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type", "private") != "private" or "id" not in chat:
        return None  # alerts are personal: private chats only
    chat_id = str(chat["id"])
    if (now if now is not None else time.time()) - message.get("date", 0) > _MAX_COMMAND_AGE_SECONDS:
        return None
    command, argument = _command(message.get("text") or "")
    if command == "/start" and argument:
        name = actions.link_chat(argument, chat_id)
        if name is None:
            return chat_id, "That link has expired. Open the Watchlist page and tap “Connect Telegram” again."
        return chat_id, f"✅ Connected, {name}! Your job alerts will arrive here. Send /stop to turn them off."
    if command == "/stop":
        count = actions.unlink_chat(chat_id)
        return chat_id, "Alerts to this chat are off." if count else "This chat isn't connected to an account."
    if command == "/health":
        if not actions.is_admin_chat(chat_id):
            return chat_id, "That command is only available to administrators."
        return chat_id, actions.health_text()
    if command in ("/help", "/start"):
        return chat_id, HELP_TEXT + (ADMIN_HELP if actions.is_admin_chat(chat_id) else "")
    if command.startswith("/"):
        return chat_id, f"Unknown command {command}.\n\n{HELP_TEXT}"
    return None


def poll_once(client: TelegramClient, offset: int | None, actions: BotActions, *,
              poll_seconds: int = _POLL_SECONDS) -> int | None:
    """Answer whatever arrived; returns the next offset."""
    for update in client.get_updates(offset, poll_seconds):
        offset = update["update_id"] + 1
        try:
            result = reply_for(update, actions)
            if result:
                client.send_message(*result)
        except Exception:
            logger.warning("Could not answer a Telegram message", exc_info=True)
    return offset


_bot_username: dict[str, str] = {}


def bot_username(settings: TelegramSettings | None = None) -> str | None:
    """The bot's @username, for "Connect Telegram" links (cached per token)."""
    settings = settings or TelegramSettings()
    if not settings.bot_token:
        return None
    if settings.bot_token not in _bot_username:
        _bot_username[settings.bot_token] = TelegramClient(settings.bot_token).get_me()["username"]
    return _bot_username[settings.bot_token]


class TelegramCommandListener:
    def __init__(self, config_path: Path, session_factory=None) -> None:
        from app.core.database.session import SessionLocal

        self._actions = BotActions(session_factory or SessionLocal, config_path)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="telegram-bot", daemon=True)

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
                if not settings.bot_token:
                    self._stop.wait(_IDLE_SECONDS)
                    continue
                if client is None or settings.bot_token != token:
                    token, client, offset = settings.bot_token, TelegramClient(settings.bot_token), None
                offset = poll_once(client, offset, self._actions)
            except Exception:
                # Never log the request URL -- it embeds the bot token (TelegramError omits it).
                logger.warning("Telegram polling failed; retrying in %ss", _ERROR_BACKOFF_SECONDS, exc_info=True)
                self._stop.wait(_ERROR_BACKOFF_SECONDS)


def start_telegram_command_listener(config_path: Path) -> TelegramCommandListener:
    listener = TelegramCommandListener(config_path)
    listener.start()
    return listener
