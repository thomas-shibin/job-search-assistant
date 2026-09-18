from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


class RawJob(BaseModel):
    """Source-native job payload, before normalization/dedup (spec §6-7)."""

    external_id: str | None = None
    source: str
    source_url: str
    company: str
    title: str
    location: str | None = None
    description: str
    date_posted: datetime | None = None
    raw_payload: dict = Field(default_factory=dict)


class JobSource(Protocol):
    """Common interface for every discovery mechanism (manual import, Greenhouse/Lever
    adapters, future email ingestion). Business logic never depends on a specific source.
    """

    name: str

    def discover_jobs(self, search_profile: Any) -> list[RawJob]:
        """Implemented per source starting in Milestone 3."""
        ...
