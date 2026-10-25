"""Daily backups of everything Hirlumo keeps (docs/BACKUPS.md):

1. a consistent copy of the database, taken with SQLite's online backup while
   the site runs, and checked (`PRAGMA integrity_check`) before it's used;
2. packed with the uploaded resumes, tailored resumes and application files
   and config/ into one .tar.gz;
3. encrypted (crypto.py) when BACKUP_ENCRYPTION_KEY is set -- and only then
   copied off the server (app/integrations/object_storage.py): personal data
   never leaves unencrypted;
4. old ones removed, here and in the bucket: one a day for `keep_daily` days,
   one a week for `keep_weekly` weeks, one a month for `keep_monthly` months.

Only files named `hirlumo-backup-*` are ever removed; anything else in
data/backups (copies made by hand before an upgrade) is left alone. Restoring
(`extract`) unpacks into a new folder and never touches the running data.
.env isn't in the backup -- keep its secrets, the backup key above all, in a
password manager.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import tarfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logging import log_event
from app.core.paths import DATA_DIR, PROJECT_ROOT
from app.integrations.object_storage import ObjectStorage, StorageSettings
from app.modules.backups import crypto

logger = logging.getLogger(__name__)

PREFIX = "hirlumo-backup-"
_NAME = re.compile(r"^hirlumo-backup-(\d{8}-\d{6})\.tar\.gz(\.enc)?$")
# Folders under data/ that hold people's files; the database is copied separately.
FOLDERS = ("resumes", "applications")
_lock = threading.Lock()


class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    enabled: bool = Field(default=True, alias="BACKUP_ENABLED")
    hour: int = Field(default=3, ge=0, le=23, alias="BACKUP_HOUR")  # server's local time
    encryption_key: str | None = Field(default=None, alias="BACKUP_ENCRYPTION_KEY")
    keep_daily: int = Field(default=7, ge=1, alias="BACKUP_KEEP_DAILY")
    keep_weekly: int = Field(default=4, ge=0, alias="BACKUP_KEEP_WEEKLY")
    keep_monthly: int = Field(default=6, ge=0, alias="BACKUP_KEEP_MONTHLY")


class BackupError(RuntimeError):
    pass


@dataclass
class Places:
    """Where things are; tests point these at a temporary folder."""
    database: Path
    data: Path = DATA_DIR
    config: Path = PROJECT_ROOT / "config"

    @property
    def backups(self) -> Path:
        return self.data / "backups"


def default_places() -> Places:
    from app.core.database.session import DATABASE_URL

    if not DATABASE_URL.startswith("sqlite:///"):
        raise BackupError("Backups copy a SQLite database; DATABASE_URL isn't one.")
    return Places(database=Path(DATABASE_URL.removeprefix("sqlite:///")))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- status, for the Health page ---------------------------------------------------------

def _status_path(places: Places) -> Path:
    return places.backups / "status.json"


def read_status(places: Places | None = None) -> dict:
    try:
        return json.loads(_status_path(places or default_places()).read_text())
    except (OSError, ValueError, BackupError):
        return {}


def _write_status(places: Places, **values) -> None:
    status = {**read_status(places), **values}
    path = _status_path(places)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, default=str))
    tmp.replace(path)


# ---- making one ---------------------------------------------------------------------

def _snapshot(source: Path, target: Path) -> None:
    """A consistent copy of a live SQLite database (WAL included), then checked."""
    if not source.exists():
        raise BackupError(f"No database at {source}")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != "ok":
        raise BackupError(f"The database copy failed its integrity check: {result}")


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def create(settings: BackupSettings | None = None, places: Places | None = None,
           storage: ObjectStorage | None = None, now: datetime | None = None) -> dict:
    """Make one backup now, copy it off the server when that's set up, and
    remove old ones. Returns what happened; raises BackupError if no backup
    could be made. One at a time."""
    settings = settings or BackupSettings()
    places = places or default_places()
    if not _lock.acquire(blocking=False):
        raise BackupError("A backup is already running.")
    try:
        return _create(settings, places, storage, now or _utcnow())
    finally:
        _lock.release()


def _create(settings: BackupSettings, places: Places, storage: ObjectStorage | None, now: datetime) -> dict:
    places.backups.mkdir(parents=True, exist_ok=True)
    _write_status(places, last_attempt_at=now.isoformat())
    work = places.backups / ".work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        needed = 2 * (_size(places.database) + sum(_size(places.data / f) for f in FOLDERS))
        free = shutil.disk_usage(places.backups).free
        if free < needed:
            raise BackupError(f"Not enough disk space: {free // 1_000_000} MB free, about "
                              f"{needed // 1_000_000} MB needed.")
        key = crypto.load_key(settings.encryption_key) if settings.encryption_key else None

        snapshot = work / places.database.name
        _snapshot(places.database, snapshot)
        stamp = now.strftime("%Y%m%d-%H%M%S")
        archive = work / f"{PREFIX}{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname=places.database.name)
            for folder in FOLDERS:
                if (places.data / folder).exists():
                    tar.add(places.data / folder, arcname=folder)
            if places.config.exists():
                tar.add(places.config, arcname="config")
        if key is not None:
            final = places.backups / (archive.name + ".enc")
            with archive.open("rb") as source, final.with_suffix(".part").open("wb") as target:
                crypto.encrypt(source, target, key)
            final.with_suffix(".part").replace(final)
        else:
            final = places.backups / archive.name
            archive.replace(final)
    except Exception as exc:
        _write_status(places, last_error=f"{type(exc).__name__}: {exc}"[:500], last_error_at=now.isoformat())
        logger.error("Backup failed: %s", exc)
        raise BackupError(str(exc)) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result = {"file": final.name, "size": final.stat().st_size, "encrypted": key is not None,
              "uploaded": False, "upload_error": None}
    storage_settings = StorageSettings()
    if storage is None and storage_settings.configured and key is not None:
        storage = ObjectStorage(storage_settings)
    if storage is not None and key is not None:
        try:
            storage.upload(final)
            result["uploaded"] = True
            remove_old_remote(storage, settings)
        except Exception as exc:
            result["upload_error"] = str(exc)[:500]
            logger.error("Backup made but not copied off the server: %s", exc)
    elif storage_settings.configured and key is None:
        result["upload_error"] = "Not copied off the server: set BACKUP_ENCRYPTION_KEY first."
    remove_old_local(places, settings)
    _write_status(places, last_success_at=now.isoformat(), last_file=final.name, last_size=result["size"],
                  encrypted=result["encrypted"], uploaded=result["uploaded"], upload_error=result["upload_error"],
                  last_error=None)
    log_event(logger, "BACKUP_CREATED", file=final.name, size=result["size"], uploaded=result["uploaded"])
    return result


# ---- keeping the right ones -----------------------------------------------------------

def taken_at(name: str) -> datetime | None:
    match = _NAME.match(name)
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S") if match else None


def to_keep(names: list[str], settings: BackupSettings) -> set[str]:
    """The newest backup of each of the last `keep_daily` days, `keep_weekly`
    weeks and `keep_monthly` months (that have one)."""
    dated = sorted(((taken_at(n), n) for n in names if taken_at(n)), reverse=True)
    keep: set[str] = set()
    for count, period in ((settings.keep_daily, lambda d: d.date()),
                          (settings.keep_weekly, lambda d: d.isocalendar()[:2]),
                          (settings.keep_monthly, lambda d: (d.year, d.month))):
        seen = []
        for when, name in dated:
            p = period(when)
            if p not in seen:
                if len(seen) == count:
                    break
                seen.append(p)
                keep.add(name)
    return keep


def remove_old_local(places: Places, settings: BackupSettings) -> list[str]:
    names = [p.name for p in places.backups.glob(PREFIX + "*") if taken_at(p.name)]
    removed = [n for n in names if n not in to_keep(names, settings)]
    for name in removed:
        (places.backups / name).unlink(missing_ok=True)
    return removed


def remove_old_remote(storage: ObjectStorage, settings: BackupSettings) -> list[str]:
    names = [n for n, _ in storage.names() if taken_at(n)]
    removed = [n for n in names if n not in to_keep(names, settings)]
    for name in removed:
        storage.delete(name)
    return removed


# ---- listing and restoring ------------------------------------------------------------------

def local_backups(places: Places | None = None) -> list[tuple[str, int]]:
    places = places or default_places()
    return sorted(((p.name, p.stat().st_size) for p in places.backups.glob(PREFIX + "*") if taken_at(p.name)),
                  reverse=True)


def extract(backup: Path, target: Path, key: str | None) -> Path:
    """Unpack a backup into `target`, a folder that must not exist yet (the
    running site's data is never overwritten). Returns `target`."""
    if target.exists():
        raise BackupError(f"{target} already exists -- choose a new folder.")
    target.mkdir(parents=True)
    try:
        archive = backup
        if backup.name.endswith(".enc"):
            archive = target / ".archive.tar.gz"
            with backup.open("rb") as source, archive.open("wb") as out:
                crypto.decrypt(source, out, crypto.load_key(key))
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(target, filter="data")  # no paths outside `target`, no devices or links out
        if archive != backup:
            archive.unlink()
    except Exception:
        shutil.rmtree(target, ignore_errors=True)  # nothing half-restored left behind
        raise
    return target
