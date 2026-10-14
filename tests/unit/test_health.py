"""Tests for the admin health report and the Telegram bot: linking a chat to
an account with a single-use code, and /health for admins only. Telegram itself
is faked -- no real HTTP."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.auth.tokens import issue_token
from app.core.config.store import load_app_config
from app.core.database.base import Base
from app.modules.jobs.models import Job, JobStatus
from app.modules.auth.models import TokenPurpose, User
from app.integrations.telegram import TelegramClient
from app.modules.alerts.telegram_commands import BotActions, poll_once, reply_for
from app.modules.admin.health import FAIL, check_health, format_health_message
from app.core.tenancy import system_session, tenant_session


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, info={"tenant_strict": True})


def _user(factory, uid: int, role: str = "member") -> None:
    with system_session(factory) as s:
        s.add(User(id=uid, email=f"u{uid}@example.com", first_name=f"U{uid}", last_name="X", role=role,
                   email_verified_at=datetime(2026, 1, 1)))
        s.commit()


def _message(text: str, chat_id: int = 42, age: float = 0, chat_type: str = "private") -> dict:
    return {"update_id": 7, "message": {"chat": {"id": chat_id, "type": chat_type},
                                        "date": int(time.time() - age), "text": text}}


def _link_code(factory, uid: int) -> str:
    with tenant_session(factory, uid) as s:
        code = issue_token(s, TokenPurpose.TELEGRAM_LINK, user_id=uid, ttl=timedelta(minutes=15))
        s.commit()
    return code


def test_health_report_covers_the_platform(factory, tmp_path):
    _user(factory, 1)
    with tenant_session(factory, 1) as s:
        s.add(Job(source="manual", source_url="u", company="Acme", title="Dev", description="d",
                  status=JobStatus.DISCOVERED, fingerprint="f", description_hash="h"))
        s.commit()

    with system_session(factory) as s:
        report = check_health(s, tmp_path / "missing.yaml")

    names = [c.name for c in report.checks]
    assert names[0] == "Database" and report.checks[0].detail == "reachable, 1 accounts, 1 jobs tracked"
    assert {"Scheduler", "Accounts", "Integrations", "PDF export", "Last 24h"} <= set(names)
    assert report.status in ("ok", "degraded")
    assert "1 new jobs" in next(c.detail for c in report.checks if c.name == "Last 24h")
    text = format_health_message(report).lower()
    assert "openai" not in text and "adzuna" not in text  # vendor-neutral even for admins


def test_health_report_is_down_without_a_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nope' / 'missing.db'}")  # directory doesn't exist
    with system_session(sessionmaker(bind=engine)) as s:
        report = check_health(s)
    assert report.status == "down"
    assert report.checks[0].status == FAIL
    assert "❌ App is down" in format_health_message(report)


def test_start_code_links_the_chat_to_that_account_only(factory, tmp_path):
    _user(factory, 1)
    _user(factory, 2)
    actions = BotActions(factory, tmp_path / "missing.yaml")
    code = _link_code(factory, 2)

    chat, reply = reply_for(_message(f"/start {code}", chat_id=555), actions)
    assert chat == "555" and "Connected, U2" in reply
    with tenant_session(factory, 2) as s:
        assert load_app_config(s, tmp_path / "missing.yaml").watchlist.telegram_chat_id == "555"
    with tenant_session(factory, 1) as s:
        assert load_app_config(s, tmp_path / "missing.yaml").watchlist.telegram_chat_id is None

    # Single use: replaying the code (e.g. from another chat) does nothing.
    chat, reply = reply_for(_message(f"/start {code}", chat_id=666), actions)
    assert "expired" in reply
    with tenant_session(factory, 2) as s:
        assert load_app_config(s, tmp_path / "missing.yaml").watchlist.telegram_chat_id == "555"


def test_stop_unlinks_and_group_chats_are_ignored(factory, tmp_path):
    _user(factory, 1)
    actions = BotActions(factory, tmp_path / "missing.yaml")
    reply_for(_message(f"/start {_link_code(factory, 1)}"), actions)

    assert reply_for(_message("/health", chat_type="group"), actions) is None
    assert "off" in reply_for(_message("/stop"), actions)[1]
    with tenant_session(factory, 1) as s:
        assert load_app_config(s, tmp_path / "missing.yaml").watchlist.telegram_chat_id is None


def test_health_command_answers_admin_chats_only(factory, tmp_path):
    _user(factory, 1, role="member")
    _user(factory, 2, role="admin")
    actions = BotActions(factory, tmp_path / "missing.yaml")
    reply_for(_message(f"/start {_link_code(factory, 1)}", chat_id=100), actions)
    reply_for(_message(f"/start {_link_code(factory, 2)}", chat_id=200), actions)

    assert "only available to administrators" in reply_for(_message("/health", chat_id=100), actions)[1]
    assert "only available to administrators" in reply_for(_message("/health", chat_id=999), actions)[1]
    assert "Job Search Assistant" in reply_for(_message("/health", chat_id=200), actions)[1]
    assert reply_for(_message("/health", chat_id=200, age=3600), actions) is None  # stale backlog
    assert reply_for(_message("hello", chat_id=200), actions) is None


def test_poll_once_replies_and_advances_offset(factory, tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [_message("/help")]})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = TelegramClient("t", client=httpx.Client(transport=httpx.MockTransport(handler)))
    offset = poll_once(client, None, BotActions(factory, tmp_path / "missing.yaml"), poll_seconds=0)

    assert offset == 8
    assert calls[1][0] == "sendMessage" and calls[1][1]["chat_id"] == "42" and "/stop" in calls[1][1]["text"]
