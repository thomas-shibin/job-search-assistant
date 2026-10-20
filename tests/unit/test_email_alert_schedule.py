"""When alert emails go out (app/modules/alerts/email_alerts.py): after every
scan, or once a day / week from 8:00 local time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.alerts.email_alerts import is_due, parse_token, period_start

TORONTO = timezone(timedelta(hours=-4))  # EDT


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TORONTO)  # Oct 12 2026 is a Monday


def utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def test_daily_period_starts_at_eight_today_or_yesterday():
    assert period_start("daily", local(15, 9)) == local(15, 8)
    assert period_start("daily", local(15, 7)) == local(14, 8)


def test_weekly_period_starts_on_monday_at_eight():
    assert period_start("weekly", local(15, 9)) == local(12, 8)  # Thursday -> Monday
    assert period_start("weekly", local(12, 7)) == local(5, 8)  # Monday before 8 -> the Monday before


@pytest.mark.parametrize("frequency, last, now, due", [
    ("instant", local(15, 9), local(15, 9, 5), True),
    ("daily", None, local(15, 3), True),  # the first email goes out straight away
    ("daily", local(15, 8, 30), local(15, 20), False),  # already sent today
    ("daily", local(14, 8, 30), local(15, 7), False),  # before 8: still yesterday's period
    ("daily", local(14, 8, 30), local(15, 8, 5), True),
    ("weekly", local(12, 9), local(18, 23), False),
    ("weekly", local(12, 9), local(19, 8, 1), True),
    ("off", None, local(15, 9), False),
])
def test_is_due(frequency, last, now, due):
    assert is_due(frequency, utc(last) if last else None, now) is due


@pytest.mark.parametrize("token, expected", [
    ("7.abcdefghijklmnopqrstuvwxyz", (7, "abcdefghijklmnopqrstuvwxyz")),
    ("7.short", None),
    ("x.abcdefghijklmnopqrstuvwxyz", None),
    ("abcdefghijklmnopqrstuvwxyz", None),
    ("", None),
])
def test_parse_token(token, expected):
    assert parse_token(token) == expected
