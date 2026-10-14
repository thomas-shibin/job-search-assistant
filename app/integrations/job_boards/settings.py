"""Adzuna API credentials, loaded from the environment / `.env` -- kept out
of config.yaml like every other secret (see `app/integrations/ai/settings.py`). Sign up
for a free app_id/app_key at https://developer.adzuna.com/.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdzunaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_id: str | None = Field(default=None, alias="ADZUNA_APP_ID")
    app_key: str | None = Field(default=None, alias="ADZUNA_APP_KEY")
