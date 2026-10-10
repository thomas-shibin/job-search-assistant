"""Telegram setup actions shared by the dashboard's Watchlist page and the CLI:
detect the chat id from the bot's recent messages (saving it as the
`watchlist.telegram_chat_id` setting), and send a test message.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config.schema import AppConfig
from app.config.store import save_section
from app.notifications.telegram import TelegramClient, TelegramError, TelegramSettings


def _client(settings: TelegramSettings) -> TelegramClient:
    if not settings.bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set in .env")
    return TelegramClient(settings.bot_token)


def detect_and_save_chat_id(session: Session, config: AppConfig, settings: TelegramSettings | None = None) -> dict:
    """Saves the most recent chat that messaged the bot; returns that chat."""
    chats = _client(settings or TelegramSettings()).recent_chats()
    if not chats:
        raise TelegramError("No messages found -- open your bot in Telegram, press Start, then try again.")
    chat = chats[-1]
    save_section(session, "watchlist", {**config.watchlist.model_dump(), "telegram_chat_id": str(chat["id"])})
    return chat


def send_test_message(config: AppConfig, settings: TelegramSettings | None = None) -> None:
    settings = settings or TelegramSettings()
    chat_id = config.watchlist.telegram_chat_id or settings.chat_id
    if not chat_id:
        raise TelegramError("No Telegram chat id yet -- use Detect chat ID first.")
    _client(settings).send_message(chat_id, "✅ Job Search Assistant is connected. Watchlist alerts will arrive here.")
