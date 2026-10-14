"""Telegram actions for one signed-in user's Watchlist page: start linking
their chat (a single-use code for `t.me/<bot>?start=<code>`, completed by the
bot -- see app/notifications/telegram_commands.py), send a test message to
their linked chat, or disconnect it."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.tokens import issue_token
from app.core.config.schema import AppConfig
from app.core.config.store import save_section
from app.models.user import TokenPurpose
from app.integrations.telegram import TelegramClient, TelegramError, TelegramSettings
from app.notifications.telegram_commands import LINK_TTL, bot_username
from app.core.tenancy import require_tenant


def _client(settings: TelegramSettings) -> TelegramClient:
    if not settings.bot_token:
        raise TelegramError("Telegram alerts aren't available right now.")
    return TelegramClient(settings.bot_token)


def telegram_link(session: Session, settings: TelegramSettings | None = None) -> str:
    """The deep link that connects the signed-in user's Telegram chat."""
    settings = settings or TelegramSettings()
    _client(settings)
    username = bot_username(settings)
    code = issue_token(session, TokenPurpose.TELEGRAM_LINK, user_id=require_tenant(session), ttl=LINK_TTL)
    session.commit()
    return f"https://t.me/{username}?start={code}"


def disconnect(session: Session, config: AppConfig) -> None:
    save_section(session, "watchlist", {**config.watchlist.model_dump(exclude={"watches"}), "telegram_chat_id": None})


def send_test_message(config: AppConfig, settings: TelegramSettings | None = None) -> None:
    settings = settings or TelegramSettings()
    chat_id = config.watchlist.telegram_chat_id
    if not chat_id:
        raise TelegramError("Connect Telegram first.")
    _client(settings).send_message(chat_id, "✅ Job Search Assistant is connected. Watchlist alerts will arrive here.")
