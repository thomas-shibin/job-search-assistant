"""Database-backed settings: the source of truth for every `AppConfig` section
the dashboard can edit.

Multi-tenant split: *platform* sections (provider, scan schedule, retention,
limits) are shared rows in `app_settings`, edited by admins only; every other
section is the signed-in user's own row in `user_settings` (tenant-scoped, see
app/core/tenancy.py). `load_app_config` assembles one `AppConfig` from both, so the
rest of the app reads settings exactly as before.

Defaults arrive through versioned Alembic migrations (`app/core/config/seeding.py`):
a new install's database is created with every default setting, user-specific
ones blank, and later releases only add missing keys -- never overwriting what
a user changed. config.yaml is an optional import source (`python cli.py
import-config --force`), and the fallback for a database that was created
without the migrations (e.g. the test suite). Secrets stay in .env throughout.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

import os

from app.core.config.loader import ConfigError, load_config_dict
from app.core.config.models import AppSetting, UserSetting, WatchlistEntry
from app.core.config.schema import AppConfig, PlatformConfig, Watch
from app.core.logging import log_event
from app.core.tenancy import sees_user_data, tenant_of

logger = logging.getLogger(__name__)

PLATFORM_SECTIONS = ("ai", "scheduler", "retention", "platform")
USER_SECTIONS = (
    "candidate", "search", "address_profiles", "search_profiles", "matching", "resume", "notifications", "watchlist",
)
SECTIONS = USER_SECTIONS + PLATFORM_SECTIONS
WATCH_LIST_FIELDS = ("companies", "titles", "keywords", "locations", "exclude_keywords", "exclude_companies", "job_types")
WATCH_VALUE_FIELDS = ("min_score", "min_salary", "max_salary", "posted_within_days", "search")


def _validate(data: dict) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError([f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]) from exc


def has_settings(session: Session) -> bool:
    return session.query(AppSetting.key).first() is not None


def import_yaml_config(session: Session, yaml_path: Path, *, replace_watches: bool = True,
                       platform_only: bool = False) -> AppConfig:
    """Copy config.yaml into the database, overwriting every section it defines:
    the platform sections, plus -- unless `platform_only` -- the session's own
    user's sections and watches."""
    if not yaml_path.exists():
        raise ConfigError([
            f"No settings in the database yet and no config file to import at {yaml_path}. "
            "Run 'python cli.py init' first."
        ])
    raw = load_config_dict(yaml_path)
    config = _validate(raw)  # never import a half-valid file

    dumped = config.model_dump(mode="json")
    watches = dumped["watchlist"].pop("watches")
    for key in PLATFORM_SECTIONS:
        session.merge(AppSetting(key=key, value=dumped[key]))
    if not platform_only and sees_user_data(session):  # a user's own sections only ever go to that user
        for key in USER_SECTIONS:
            _put_user_section(session, key, dumped[key])
        if replace_watches:
            session.query(WatchlistEntry).delete()
            for watch in watches:
                session.add(WatchlistEntry(enabled=True, **watch))
    session.commit()
    log_event(logger, "CONFIG_IMPORTED", path=str(yaml_path), watches=len(watches))
    return config


def _put_user_section(session: Session, key: str, value: Any) -> None:
    row = session.query(UserSetting).filter(UserSetting.key == key).one_or_none()
    if row is None:
        session.add(UserSetting(key=key, value=value))
    else:
        row.value = value


def _user_defaults(session: Session) -> dict[str, Any]:
    """A new user's settings: the schema defaults, with their own name."""
    from app.modules.auth.models import User  # core doesn't depend on modules at import time

    user_id = tenant_of(session)
    user = session.get(User, user_id) if user_id is not None else None
    return {"candidate": {"name": user.full_name if user else "", "base_resume": ""}}


def _section_values(session: Session) -> dict[str, Any]:
    platform = {row.key: row.value for row in session.query(AppSetting).filter(AppSetting.key.in_(PLATFORM_SECTIONS))}
    data: dict[str, Any] = {key: value for key, value in platform.items()}
    if sees_user_data(session):
        data.update(_user_defaults(session))
        data.update({row.key: row.value for row in session.query(UserSetting).filter(UserSetting.key.in_(USER_SECTIONS))})
    else:
        data.setdefault("candidate", {})
    # Platform-controlled values inside user sections: always the platform's.
    limits = PlatformConfig.model_validate(data.get("platform") or {})
    data["search"] = {**(data.get("search") or {}), "adzuna_daily_limit": limits.job_search_daily_limit}
    data["watchlist"] = {**(data.get("watchlist") or {}), "dashboard_url": os.environ.get("PUBLIC_URL") or None}
    return data


def _watch_dicts(session: Session, *, enabled_only: bool) -> list[dict]:
    query = session.query(WatchlistEntry).order_by(WatchlistEntry.id)
    if enabled_only:
        query = query.filter(WatchlistEntry.enabled.is_(True))
    return [
        {
            "name": entry.name,
            **{field: list(getattr(entry, field) or []) for field in WATCH_LIST_FIELDS},
            **{field: getattr(entry, field) for field in WATCH_VALUE_FIELDS if getattr(entry, field) is not None},
        }
        for entry in query.all()
    ]


def load_app_config(session: Session, yaml_path: Path) -> AppConfig:
    """The effective configuration for the session's user: platform settings,
    their own sections and enabled watches. Imports `yaml_path` first if the
    database has never been populated (a fresh, unmigrated database).
    """
    if not has_settings(session):
        # A fresh install may not have created config.yaml: start from the
        # neutral starter settings next to it instead -- or, with neither, the
        # built-in defaults.
        example = yaml_path.with_name("config.example.yaml")
        # Platform sections only: a file's personal values (names, searches,
        # addresses, watches) must never land in whoever happens to be signed in.
        source = yaml_path if yaml_path.exists() else example
        if source.exists():
            import_yaml_config(session, source, platform_only=True)
    data = _section_values(session)
    watches = _watch_dicts(session, enabled_only=True) if sees_user_data(session) else []
    data["watchlist"] = {**data.get("watchlist", {}), "watches": watches}
    return _validate(data)


def save_section(session: Session, key: str, value: Any) -> AppConfig:
    """Validate the whole config with `key` replaced by `value`, then persist it.
    Raises ConfigError (nothing saved) if the result would be invalid.
    """
    if key not in SECTIONS:
        raise ConfigError([f"Unknown settings section {key!r}"])
    if key in USER_SECTIONS and not sees_user_data(session):
        raise ConfigError([f"{key!r} settings belong to a user -- sign in first"])
    data = _section_values(session)
    data[key] = value
    watches = _watch_dicts(session, enabled_only=True) if sees_user_data(session) else []
    data["watchlist"] = {**data.get("watchlist", {}), "watches": watches}
    config = _validate(data)

    stored = config.model_dump(mode="json")[key]
    if key == "watchlist":
        stored.pop("watches")
        stored.pop("dashboard_url", None)  # platform-controlled (PUBLIC_URL)
    if key == "search":
        stored.pop("adzuna_daily_limit", None)  # platform-controlled
    if key in PLATFORM_SECTIONS:
        session.merge(AppSetting(key=key, value=stored))
    else:
        _put_user_section(session, key, stored)
    session.commit()
    log_event(logger, "SETTINGS_SAVED", section=key)
    return config


def save_watch(session: Session, entry: WatchlistEntry, *, name: str, enabled: bool, **fields) -> None:
    """Validate via `Watch` (e.g. rejects a watch with no criteria), then persist."""
    try:
        watch = Watch(name=name, **fields)
    except ValidationError as exc:
        raise ConfigError([
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"].removeprefix("Value error, ")
            for e in exc.errors()
        ]) from exc
    entry.name = watch.name
    entry.enabled = enabled
    for field in WATCH_LIST_FIELDS + WATCH_VALUE_FIELDS:
        setattr(entry, field, getattr(watch, field))
    session.add(entry)
    session.commit()
