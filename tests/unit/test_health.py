"""Tests for the health report (HTTP /health and the Telegram /health command).
Telegram itself is faked -- no real HTTP."""
from __future__ import annotations

import json
import time

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  register all models on Base.metadata
from app.database.base import Base
from app.models.job import Job, JobStatus
from app.notifications.telegram import TelegramClient
from app.notifications.telegram_commands import HELP_TEXT, poll_once, reply_for
from app.services.health import FAIL, check_health, format_health_message


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _message(text: str, chat_id: int = 42, age: float = 0) -> dict:
    return {"update_id": 7, "message": {"chat": {"id": chat_id}, "date": int(time.time() - age), "text": text}}


def test_health_report_covers_the_app(tmp_path):
    session = _session(tmp_path)
    session.add(Job(source="manual", source_url="u", company="Acme", title="Dev", description="d",
                    status=JobStatus.DISCOVERED, fingerprint="f", description_hash="h"))
    session.commit()

    report = check_health(session, tmp_path / "missing.yaml")

    names = [c.name for c in report.checks]
    assert names[0] == "Database" and report.checks[0].detail.endswith("1 jobs tracked")
    assert {"Scheduler", "Resume", "API keys", "PDF export", "Last 24h"} <= set(names)
    assert report.status in ("ok", "degraded")  # never "down" with a working database
    assert "1 new jobs" in next(c.detail for c in report.checks if c.name == "Last 24h")


def test_health_report_is_down_without_a_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nope' / 'missing.db'}")  # directory doesn't exist
    report = check_health(sessionmaker(bind=engine)())

    assert report.status == "down"
    assert report.checks[0].status == FAIL
    assert "❌ App is down" in format_health_message(report)


def test_health_message_escapes_html(tmp_path):
    report = check_health(_session(tmp_path), tmp_path / "missing.yaml")
    report.checks[0].detail = "<b>oops</b>"
    assert "&lt;b&gt;oops&lt;/b&gt;" in format_health_message(report)


def test_reply_for_answers_only_the_configured_chat():
    health = lambda: "HEALTHY"  # noqa: E731
    assert reply_for(_message("/health"), "42", health) == "HEALTHY"
    assert reply_for(_message("/health@MyJobBot"), "42", health) == "HEALTHY"
    assert reply_for(_message("/help"), "42", health) == HELP_TEXT
    assert "Unknown command /foo" in reply_for(_message("/foo"), "42", health)
    assert reply_for(_message("/health", chat_id=99), "42", health) is None  # a stranger
    assert reply_for(_message("hello"), "42", health) is None
    assert reply_for(_message("/health", age=3600), "42", health) is None  # stale backlog


def test_poll_once_replies_and_advances_offset():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [_message("/health")]})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client = TelegramClient("t", client=httpx.Client(transport=httpx.MockTransport(handler)))
    offset = poll_once(client, None, "42", lambda: "HEALTHY", poll_seconds=0)

    assert offset == 8
    assert calls[1] == ("sendMessage", {"chat_id": "42", "text": "HEALTHY", "parse_mode": "HTML",
                                        "disable_web_page_preview": True})
