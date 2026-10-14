"""Secrets loaded from the environment / `.env`, kept separate from
`config.yaml` (spec: API keys never live in the versioned/shared config
file). See `.env.example`.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
