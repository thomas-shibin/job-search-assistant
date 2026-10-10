"""Database-backed settings: the source of truth for every `AppConfig` section
the dashboard can edit. config.yaml is now only an import source -- the first
time settings are loaded and the `app_settings` table is empty, the YAML file
is copied in (sections -> `AppSetting` rows, watches -> `WatchlistEntry` rows).
After that, edits happen in the dashboard (or via `python cli.py import-config
--force` to re-import a YAML file over them). Secrets stay in .env throughout.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config.loader import ConfigError, load_config_dict
from app.config.schema import AppConfig, Watch
from app.models.settings import AppSetting, WatchlistEntry
from app.utils.logging import log_event

logger = logging.getLogger(__name__)

SECTIONS = (
    "candidate", "search", "address_profiles", "search_profiles", "matching", "ai",
    "resume", "notifications", "watchlist", "scheduler",
)
WATCH_LIST_FIELDS = ("companies", "titles", "keywords", "locations", "exclude_keywords", "exclude_companies", "job_types")
WATCH_VALUE_FIELDS = ("min_score", "min_salary", "max_salary", "posted_within_days", "search")


def _validate(data: dict) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError([f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]) from exc


def has_settings(session: Session) -> bool:
    return session.query(AppSetting.key).first() is not None


def import_yaml_config(session: Session, yaml_path: Path, *, replace_watches: bool = True) -> AppConfig:
    """Copy config.yaml into the database, overwriting every section it defines."""
    if not yaml_path.exists():
        raise ConfigError([
            f"No settings in the database yet and no config file to import at {yaml_path}. "
            "Run 'python cli.py init' first."
        ])
    raw = load_config_dict(yaml_path)
    config = _validate(raw)  # never import a half-valid file

    dumped = config.model_dump(mode="json")
    watches = dumped["watchlist"].pop("watches")
    for key in SECTIONS:
        session.merge(AppSetting(key=key, value=dumped[key]))
    if replace_watches:
        session.query(WatchlistEntry).delete()
        for watch in watches:
            session.add(WatchlistEntry(enabled=True, **watch))
    session.commit()
    log_event(logger, "CONFIG_IMPORTED", path=str(yaml_path), watches=len(watches))
    return config


def _section_values(session: Session) -> dict[str, Any]:
    return {row.key: row.value for row in session.query(AppSetting).all()}


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
    """The effective configuration: DB settings plus enabled watches, importing
    `yaml_path` first if the database has never been populated.
    """
    if not has_settings(session):
        # A fresh install may not have created config.yaml: start from the
        # neutral starter settings next to it instead.
        example = yaml_path.with_name("config.example.yaml")
        import_yaml_config(session, yaml_path if yaml_path.exists() or not example.exists() else example)
    data = _section_values(session)
    data["watchlist"] = {**data.get("watchlist", {}), "watches": _watch_dicts(session, enabled_only=True)}
    return _validate(data)


def save_section(session: Session, key: str, value: Any) -> AppConfig:
    """Validate the whole config with `key` replaced by `value`, then persist it.
    Raises ConfigError (nothing saved) if the result would be invalid.
    """
    if key not in SECTIONS:
        raise ConfigError([f"Unknown settings section {key!r}"])
    data = _section_values(session)
    data[key] = value
    data["watchlist"] = {**data.get("watchlist", {}), "watches": _watch_dicts(session, enabled_only=True)}
    config = _validate(data)

    stored = config.model_dump(mode="json")[key]
    if key == "watchlist":
        stored.pop("watches")
    session.merge(AppSetting(key=key, value=stored))
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
