"""Backups (app/modules/backups): a consistent, encrypted copy of the database
and members' files, copied off the server, old ones removed, and restorable
into a new folder -- against a temporary folder and a fake bucket."""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.modules.backups import crypto
from app.modules.backups import service as backups


class _Bucket:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    def upload(self, path: Path) -> None:
        self.files[path.name] = path.read_bytes()

    def names(self):
        return [(name, len(data)) for name, data in self.files.items()]

    def delete(self, name: str) -> None:
        del self.files[name]


@pytest.fixture()
def places(tmp_path) -> backups.Places:
    data = tmp_path / "data"
    (data / "resumes" / "uploads" / "user_1").mkdir(parents=True)
    (data / "resumes" / "uploads" / "user_1" / "resume.docx").write_bytes(b"resume bytes")
    (data / "applications" / "Acme_Clerk_1").mkdir(parents=True)
    (data / "applications" / "Acme_Clerk_1" / "tailored_resume.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text("search: {}\n")
    db = data / "job_assistant.db"
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        c.execute("INSERT INTO users (email) VALUES ('mia@example.com')")
    return backups.Places(database=db, data=data, config=tmp_path / "config")


def _settings(**kw) -> backups.BackupSettings:
    return backups.BackupSettings(**{"BACKUP_ENCRYPTION_KEY": crypto.new_key(), **kw})


def test_encryption_round_trips_and_refuses_the_wrong_key_or_a_cut_file(monkeypatch):
    monkeypatch.setattr(crypto, "CHUNK", 10)  # several chunks
    key = crypto.load_key(crypto.new_key())
    plain = b"personal data " * 20
    sealed = io.BytesIO()
    crypto.encrypt(io.BytesIO(plain), sealed, key)
    assert plain not in sealed.getvalue()

    out = io.BytesIO()
    crypto.decrypt(io.BytesIO(sealed.getvalue()), out, key)
    assert out.getvalue() == plain
    with pytest.raises(crypto.BackupKeyError, match="Wrong"):
        crypto.decrypt(io.BytesIO(sealed.getvalue()), io.BytesIO(), crypto.load_key(crypto.new_key()))
    with pytest.raises(crypto.BackupKeyError, match="cut short"):
        crypto.decrypt(io.BytesIO(sealed.getvalue()[:-30]), io.BytesIO(), key)
    with pytest.raises(crypto.BackupKeyError):
        crypto.load_key("too-short")


def test_a_backup_is_encrypted_copied_off_the_server_and_restores_into_a_new_folder(places, tmp_path):
    settings, bucket = _settings(), _Bucket()

    result = backups.create(settings, places, storage=bucket)

    assert result["encrypted"] and result["uploaded"] and result["file"].endswith(".tar.gz.enc")
    local = places.backups / result["file"]
    assert local.read_bytes() == bucket.files[result["file"]]
    assert b"mia@example.com" not in local.read_bytes()
    assert not (places.backups / ".work").exists()
    status = backups.read_status(places)
    assert status["last_file"] == result["file"] and status["uploaded"] is True and status["last_error"] is None

    restored = backups.extract(local, tmp_path / "restored", settings.encryption_key)
    with sqlite3.connect(restored / "job_assistant.db") as c:
        assert c.execute("SELECT email FROM users").fetchall() == [("mia@example.com",)]
    assert (restored / "resumes" / "uploads" / "user_1" / "resume.docx").read_bytes() == b"resume bytes"
    assert (restored / "applications" / "Acme_Clerk_1" / "tailored_resume.pdf").exists()
    assert (restored / "config" / "config.yaml").exists()
    with pytest.raises(backups.BackupError, match="already exists"):
        backups.extract(local, restored, settings.encryption_key)  # never over existing files
    with pytest.raises(crypto.BackupKeyError):
        backups.extract(local, tmp_path / "wrong", crypto.new_key())
    assert not (tmp_path / "wrong").exists()


def test_without_a_key_backups_stay_on_the_server(places, monkeypatch):
    monkeypatch.setenv("BACKUP_S3_BUCKET", "b")
    monkeypatch.setenv("BACKUP_S3_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("BACKUP_S3_SECRET_ACCESS_KEY", "secret")
    bucket = _Bucket()

    result = backups.create(backups.BackupSettings(BACKUP_ENCRYPTION_KEY=""), places, storage=bucket)

    assert result["file"].endswith(".tar.gz") and not result["encrypted"]
    assert bucket.files == {} and "BACKUP_ENCRYPTION_KEY" in result["upload_error"]


def test_seven_daily_four_weekly_and_six_monthly_are_kept():
    start = datetime(2026, 10, 21, 3, 0)
    names = [f"hirlumo-backup-{(start - timedelta(days=d)).strftime('%Y%m%d-%H%M%S')}.tar.gz.enc"
             for d in range(400)]
    names.append("hirlumo-backup-20261021-150000.tar.gz.enc")  # a second one today: the newest of the day wins

    keep = backups.to_keep(names, backups.BackupSettings())

    dates = sorted({backups.taken_at(n).date() for n in keep}, reverse=True)
    assert "hirlumo-backup-20261021-150000.tar.gz.enc" in keep
    assert "hirlumo-backup-20261021-030000.tar.gz.enc" not in keep
    assert dates[:7] == [start.date() - timedelta(days=d) for d in range(7)]
    assert len(keep) <= 7 + 4 + 6 and min(dates) > (start - timedelta(days=200)).date()


def test_old_backups_go_but_files_made_by_hand_stay(places):
    places.backups.mkdir(parents=True)
    by_hand = places.backups / "job_assistant-pre-0.8-20261021.db"
    by_hand.write_bytes(b"x")
    for day in range(1, 12):
        (places.backups / f"hirlumo-backup-202609{day:02d}-030000.tar.gz.enc").write_bytes(b"x")

    bucket = _Bucket()
    backups.create(_settings(BACKUP_KEEP_WEEKLY=0, BACKUP_KEEP_MONTHLY=0), places, storage=bucket)

    left = sorted(p.name for p in places.backups.iterdir() if p.name.startswith("hirlumo-backup-"))
    assert len(left) == 7 and by_hand.exists()


def test_a_failed_backup_is_reported_and_leaves_nothing_half_made(places, monkeypatch):
    monkeypatch.setattr(backups.shutil, "disk_usage", lambda path: type("U", (), {"free": 10})())

    with pytest.raises(backups.BackupError, match="disk space"):
        backups.create(_settings(), places, storage=_Bucket())

    assert "disk space" in backups.read_status(places)["last_error"]
    assert not list(places.backups.glob("hirlumo-backup-*")) and not (places.backups / ".work").exists()


def test_a_bucket_that_fails_still_leaves_the_backup_here(places):
    class _Down(_Bucket):
        def upload(self, path):
            raise RuntimeError("network down")

    result = backups.create(_settings(), places, storage=_Down())

    assert not result["uploaded"] and "network down" in result["upload_error"]
    assert (places.backups / result["file"]).exists()


def test_the_health_page_says_whether_backups_are_current_and_off_the_server(monkeypatch):
    from app.modules.admin import health

    monkeypatch.setenv("BACKUP_ENABLED", "true")
    now = datetime.utcnow()
    monkeypatch.setattr(backups, "read_status", lambda places=None: {})
    assert health._backups().status == health.WARN

    recent = {"last_success_at": (now - timedelta(hours=2)).isoformat(), "last_size": 5_000_000, "uploaded": True}
    monkeypatch.setattr(backups, "read_status", lambda places=None: recent)
    check = health._backups()
    assert check.status == health.OK and "copied off the server" in check.detail

    local_only = {**recent, "uploaded": False}
    monkeypatch.setattr(backups, "read_status", lambda places=None: local_only)
    check = health._backups()
    assert check.status == health.WARN and "this server only" in check.detail

    stale = {**recent, "last_success_at": (now - timedelta(days=2)).isoformat()}
    monkeypatch.setattr(backups, "read_status", lambda places=None: stale)
    assert health._backups().status == health.FAIL
