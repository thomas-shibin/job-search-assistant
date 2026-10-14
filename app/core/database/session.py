"""The one database engine (DATABASE_URL, default data/job_assistant.db) and
session factory. SQLite runs in WAL mode so pages can read while background
jobs write. `get_session` is the per-request FastAPI dependency."""
from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.paths import PROJECT_ROOT


def _default_sqlite_url() -> str:
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(data_dir / 'job_assistant.db').as_posix()}"


DATABASE_URL = os.environ.get("DATABASE_URL", _default_sqlite_url())

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        # Many users at once: readers don't block the writer, and a writer waits
        # briefly for a lock instead of failing.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

# Strict (app/core/tenancy.py): touching user data needs a user bound to the
# session, so nothing can read across users by accident.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, info={"tenant_strict": True})


def get_session() -> Iterator[Session]:
    """A strict session with no user bound: fine for accounts and shared
    platform data; user data needs `app.modules.auth.deps.member_session` instead."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
