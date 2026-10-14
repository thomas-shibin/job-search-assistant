"""The real Alembic migrations: a brand-new database gets every default setting
(user-specific ones blank), and re-seeding never overwrites a user's values."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

from app.core.config.seeding import merge_defaults, seed_setting_defaults

ROOT = Path(__file__).resolve().parents[2]


def _alembic(db: Path, *args: str) -> None:
    # A separate process: alembic/env.py reads DATABASE_URL at import time.
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}"}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True, capture_output=True)


def test_fresh_database_is_created_with_default_settings(tmp_path):
    db = tmp_path / "fresh.db"
    _alembic(db, "upgrade", "head")

    connection = sqlite3.connect(db)
    settings = {k: json.loads(v) for k, v in connection.execute("select key, value from app_settings")}
    # Shared rows are platform settings only; each user's sections live in user_settings.
    assert set(settings) == {"ai", "scheduler", "retention", "platform"}
    assert settings["retention"] == {"delete_jobs_after_days": 7, "keep_jobs_you_acted_on": True}
    assert settings["platform"]["job_search_daily_limit"] == 250
    assert not any("key" in k.lower() or "token" in k.lower() for k in settings)  # secrets stay in .env
    assert connection.execute("select count(*) from users").fetchone()[0] == 0  # no owner on a fresh install
    assert connection.execute("select count(*) from user_settings").fetchone()[0] == 0

    from sqlalchemy.orm import Session

    from app.core.config.store import load_app_config

    with Session(sa.create_engine(f"sqlite:///{db}")) as session:  # usable without any config.yaml
        assert load_app_config(session, tmp_path / "missing.yaml").scheduler.interval_minutes == 60


def test_seeding_only_fills_gaps_and_never_overwrites():
    assert merge_defaults({"a": 1, "nested": {"x": 0}}, {"a": 9, "b": 2, "nested": {"x": 5, "y": 6}}) == \
        {"a": 1, "b": 2, "nested": {"x": 0, "y": 6}}
    assert merge_defaults(["mine"], ["default"]) == ["mine"]

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("create table app_settings (key varchar primary key, value json, updated_at datetime)"))
        conn.execute(sa.text("""insert into app_settings (key, value) values ('search', '{"titles": ["Java"], "country": "gb"}')"""))
        changes = seed_setting_defaults(conn, {"search": {"titles": [], "country": "ca", "keywords": []},
                                               "retention": {"delete_jobs_after_days": 7}})
        rows = {k: json.loads(v) for k, v in conn.execute(sa.text("select key, value from app_settings"))}
    assert changes == {"search": "updated", "retention": "inserted"}
    assert rows["search"] == {"titles": ["Java"], "country": "gb", "keywords": []}
    assert rows["retention"] == {"delete_jobs_after_days": 7}
