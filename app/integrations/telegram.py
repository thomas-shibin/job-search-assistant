"""Telegram Bot API client for watchlist alerts. Talks to the documented HTTP
API (https://core.telegram.org/bots/api) over httpx -- no extra dependency.
The bot token and target chat id are secrets, so they live in `.env`
(see `app/ai/settings.py`), never in config.yaml.
"""
from __future__ import annotations

import logging

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import IntegrationError
from app.shared.retry import with_retry

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/{method}"
_REQUEST_TIMEOUT_SECONDS = 15.0
_RETRYABLE_ERRORS = (httpx.TransportError,)


class TelegramSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")


class TelegramError(IntegrationError, RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, client: httpx.Client | None = None) -> None:
        self._bot_token = bot_token
        self._client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS)

    def _call(self, method: str, payload: dict, *, timeout: float | None = None) -> object:
        url = _API_URL.format(token=self._bot_token, method=method)
        extra = {"timeout": timeout} if timeout is not None else {}
        response = with_retry(lambda: self._client.post(url, json=payload, **extra), retry_on=_RETRYABLE_ERRORS)
        body = response.json()
        if not body.get("ok"):
            # Never include `url` here: it embeds the bot token.
            raise TelegramError(f"Telegram {method} failed: {body.get('error_code')} {body.get('description')}")
        return body["result"]

    def send_message(self, chat_id: str, text: str) -> None:
        self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )

    def get_me(self) -> dict:
        return self._call("getMe", {})  # type: ignore[return-value]

    def get_updates(self, offset: int | None, poll_seconds: int) -> list[dict]:
        """Long-poll for new messages (the bot command listener): waits up to
        `poll_seconds` for one to arrive. Passing `offset` confirms -- and so
        drops from Telegram -- every update before it."""
        payload: dict = {"timeout": poll_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=poll_seconds + _REQUEST_TIMEOUT_SECONDS)  # type: ignore[return-value]

    def recent_chats(self) -> list[dict]:
        """Chats that have recently messaged the bot -- how `telegram-setup` finds
        TELEGRAM_CHAT_ID (the user sends the bot any message first).
        """
        updates = self._call("getUpdates", {"allowed_updates": ["message"]})
        chats: dict[int, dict] = {}
        for update in updates:  # type: ignore[union-attr]
            chat = (update.get("message") or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
        return list(chats.values())
