from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.tenancy import TenantOwned


class TermKind(str, enum.Enum):
    TITLE = "title"
    KEYWORD = "keyword"
    LOCATION = "location"
    COMPANY = "company"


class TermSource(str, enum.Enum):
    """Where a term came from, in increasing order of trust: a later scan never
    demotes a seeded or user-added term (see app/catalog/store.py).
    """

    SCAN = "scan"
    SEED = "seed"
    USER = "user"


class LocationType(str, enum.Enum):
    COUNTRY = "country"
    PROVINCE = "province"
    REGION = "region"
    CITY = "city"
    REMOTE = "remote"


class SearchTerm(Base):
    """The suggestion catalog behind every search/watchlist picker: one row per
    distinct job title, keyword, location or company, whatever its origin
    (built-in seed list, a market scan, or typed in by the user).

    One table for all kinds because they share identity (kind + normalized
    label), ranking (`job_count`) and lifecycle; the two location-only columns
    are simply null for the other kinds. Settings and watches store plain
    labels (portable, YAML-importable), and look terms up here by `norm` --
    e.g. to search a location by its exact `geo_path` instead of free text.
    """

    __tablename__ = "search_terms"
    __table_args__ = (
        UniqueConstraint("kind", "norm", name="uq_search_terms_kind_norm"),
        Index("ix_search_terms_kind_popularity", "kind", "job_count"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # TermKind value
    label: Mapped[str] = mapped_column(String(255))  # shown in pickers and stored in settings, e.g. "Victoria, BC"
    norm: Mapped[str] = mapped_column(String(255))  # app.shared.terms.normalize_term(label)
    source: Mapped[str] = mapped_column(String(16))  # TermSource value
    # Postings that carried this term in the latest catalog scan: orders suggestions.
    job_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Locations only: granularity, and the place's path in the job source's
    # hierarchy, e.g. ["Canada", "British Columbia", "Capital", "Victoria"].
    location_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # LocationType value
    geo_path: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class UserSearchTerm(TenantOwned, Base):
    """A label one user typed into a search, setting or watch: offered back in
    *their* pickers only. The shared `SearchTerm` catalog holds just the
    built-in list and market scans, so nobody sees what anyone else searched."""

    __tablename__ = "user_search_terms"
    __table_args__ = (UniqueConstraint("user_id", "kind", "norm", name="uq_user_search_terms_user_kind_norm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(255))
    norm: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
