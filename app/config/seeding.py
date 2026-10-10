"""Default settings delivered by versioned database migrations -- the
Liquibase-style "changeset" approach, done with Alembic.

A migration declares a frozen snapshot of default values and calls
`seed_setting_defaults(op.get_bind(), {...})`:

- a settings section that doesn't exist yet is inserted with those defaults
  (a brand-new install gets every default the moment its database is created);
- a section that exists only gets the keys it's *missing* -- values a user has
  changed are never overwritten.

To change or add a default in a later release, add a new Alembic revision that
calls `seed_setting_defaults` with just the new keys (never edit an applied
migration). User-specific values -- your name, resume, locations, job titles,
keywords, addresses, watches -- are not defaults: they start blank. Secrets
(API keys, bot token) never touch the database; they live in .env.
"""
from __future__ import annotations

import copy
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

_SETTINGS = sa.table("app_settings", sa.column("key", sa.String), sa.column("value", sa.JSON))


def merge_defaults(current: Any, defaults: Any) -> Any:
    """`current` plus any keys from `defaults` it lacks (recursively for dicts).
    Existing values -- including lists and deliberate blanks -- win."""
    if not isinstance(current, dict) or not isinstance(defaults, dict):
        return current
    merged = copy.deepcopy(current)
    for key, value in defaults.items():
        merged[key] = merge_defaults(merged[key], value) if key in merged else copy.deepcopy(value)
    return merged


def seed_setting_defaults(connection: Connection, defaults: dict[str, Any]) -> dict[str, str]:
    """Insert missing sections / fill missing keys. Returns {section: "inserted" | "updated"}."""
    changes: dict[str, str] = {}
    for key, default in defaults.items():
        row = connection.execute(sa.select(_SETTINGS.c.value).where(_SETTINGS.c.key == key)).first()
        if row is None:
            connection.execute(sa.insert(_SETTINGS).values(key=key, value=default))
            changes[key] = "inserted"
            continue
        merged = merge_defaults(row.value, default)
        if merged != row.value:
            connection.execute(sa.update(_SETTINGS).where(_SETTINGS.c.key == key).values(value=merged))
            changes[key] = "updated"
    return changes
