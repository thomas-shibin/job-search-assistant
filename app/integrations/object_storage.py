"""Off-server storage for backups: any S3-compatible bucket -- Cloudflare R2,
Backblaze B2, AWS S3 -- through boto3, imported only when it's set up.

Settings (.env): BACKUP_S3_ENDPOINT (e.g. https://<account>.r2.cloudflarestorage.com),
BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY and
BACKUP_S3_REGION ("auto" for R2). Give the key access to that one bucket only.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import IntegrationError


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    endpoint: str | None = Field(default=None, alias="BACKUP_S3_ENDPOINT")
    bucket: str | None = Field(default=None, alias="BACKUP_S3_BUCKET")
    access_key_id: str | None = Field(default=None, alias="BACKUP_S3_ACCESS_KEY_ID")
    secret_access_key: str | None = Field(default=None, alias="BACKUP_S3_SECRET_ACCESS_KEY")
    region: str = Field(default="auto", alias="BACKUP_S3_REGION")
    prefix: str = Field(default="backups/", alias="BACKUP_S3_PREFIX")

    @property
    def configured(self) -> bool:
        return bool(self.bucket and self.access_key_id and self.secret_access_key)


class StorageError(IntegrationError, RuntimeError):
    """A failed storage call; the message never includes the keys."""


class ObjectStorage:
    def __init__(self, settings: StorageSettings, client=None) -> None:
        self._settings = settings
        self._prefix = settings.prefix
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3", endpoint_url=settings.endpoint or None, region_name=settings.region,
                aws_access_key_id=settings.access_key_id, aws_secret_access_key=settings.secret_access_key,
                config=Config(retries={"max_attempts": 5, "mode": "standard"}),
            )
        self._client = client

    def _call(self, what: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # botocore raises many types; report the kind, never the request
            raise StorageError(f"Couldn't {what} ({type(exc).__name__}: {str(exc)[:200]})") from None

    def upload(self, path: Path) -> None:
        self._call("upload the backup", self._client.upload_file, str(path), self._settings.bucket,
                   self._prefix + path.name)

    def names(self) -> list[tuple[str, int]]:
        """(file name, size) of every backup in the bucket."""
        out: list[tuple[str, int]] = []
        token = None
        while True:
            kwargs = {"Bucket": self._settings.bucket, "Prefix": self._prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._call("list the backups", self._client.list_objects_v2, **kwargs)
            out += [(item["Key"][len(self._prefix):], int(item.get("Size") or 0)) for item in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return out
            token = page.get("NextContinuationToken")

    def delete(self, name: str) -> None:
        self._call("delete an old backup", self._client.delete_object, Bucket=self._settings.bucket,
                   Key=self._prefix + name)

    def download(self, name: str, path: Path) -> None:
        self._call("download the backup", self._client.download_file, self._settings.bucket, self._prefix + name,
                   str(path))
