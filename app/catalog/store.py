"""Reads and writes the search-term catalog (`app.models.catalog.SearchTerm`).

Everything goes through `upsert_terms`, which is idempotent and keyed on
(kind, normalized label): re-seeding, re-scanning or re-saving the same
settings never creates duplicates. A term's `source` only ever moves up
(scan -> seed -> user), so a scan refreshes popularity without taking over
labels the user typed or the built-in list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.catalog import seed_data
from app.models.catalog import LocationType, SearchTerm, TermKind, TermSource, UserSearchTerm
from app.tenancy import sees_user_data
from app.utils.terms import normalize_term

_TRUST = {TermSource.SCAN.value: 0, TermSource.SEED.value: 1, TermSource.USER.value: 2}
_IN_CHUNK = 500  # stay under SQLite's bound-parameter limit


@dataclass
class Term:
    label: str
    job_count: int | None = None  # None = leave the stored count alone
    location_type: str | None = None
    geo_path: list[str] | None = None


def upsert_terms(session: Session, kind: TermKind, terms: Iterable[Term | str], source: TermSource) -> int:
    """Insert new terms / enrich existing ones; returns how many were new. Commits."""
    incoming: dict[str, Term] = {}
    for term in terms:
        term = Term(term) if isinstance(term, str) else term
        term.label = " ".join(term.label.split())
        norm = normalize_term(term.label)
        if norm and norm not in incoming:
            incoming[norm] = term
    if not incoming:
        return 0

    existing: dict[str, SearchTerm] = {}
    norms = list(incoming)
    for i in range(0, len(norms), _IN_CHUNK):
        rows = session.query(SearchTerm).filter(
            SearchTerm.kind == kind.value, SearchTerm.norm.in_(norms[i:i + _IN_CHUNK])
        )
        existing.update({row.norm: row for row in rows})

    created = 0
    for norm, term in incoming.items():
        row = existing.get(norm)
        if row is None:
            session.add(SearchTerm(
                kind=kind.value, label=term.label, norm=norm, source=source.value, job_count=term.job_count or 0,
                location_type=term.location_type, geo_path=term.geo_path,
            ))
            created += 1
            continue
        if _TRUST[source.value] > _TRUST[row.source]:
            row.source = source.value
            row.label = term.label
        if term.job_count is not None:
            row.job_count = term.job_count
        if term.geo_path and not row.geo_path:
            row.geo_path = term.geo_path
        if term.location_type and not row.location_type:
            row.location_type = term.location_type
    session.commit()
    return created


def remember_user_terms(session: Session, **labels_by_kind: Iterable[str]) -> None:
    """Add anything the signed-in user typed (settings, searches, watches) to
    *their own* term list, so it is offered in their pickers from then on --
    never in anyone else's. `remember_user_terms(session, title=[...], location=[...])`.
    """
    if not sees_user_data(session):
        return
    for kind, labels in labels_by_kind.items():
        incoming = {}
        for label in labels:
            label = " ".join(label.split())
            norm = normalize_term(label)
            if norm:
                incoming.setdefault(norm, label)
        if not incoming:
            continue
        known = {norm for (norm,) in session.query(UserSearchTerm.norm).filter(
            UserSearchTerm.kind == kind, UserSearchTerm.norm.in_(list(incoming)))}
        for norm, label in incoming.items():
            if norm not in known:
                session.add(UserSearchTerm(kind=kind, label=label, norm=norm))
    session.commit()


def suggestions(session: Session, kind: TermKind, limit: int | None = None) -> list[dict]:
    """Picker options, most common in the job market first."""
    query = (
        session.query(SearchTerm.label, SearchTerm.job_count, SearchTerm.location_type)
        .filter(SearchTerm.kind == kind.value)
        .order_by(SearchTerm.job_count.desc(), SearchTerm.label)
    )
    if limit:
        query = query.limit(limit)
    shared = [{"label": label, "count": count, "type": location_type} for label, count, location_type in query]
    if not sees_user_data(session):
        return shared
    seen = {normalize_term(item["label"]) for item in shared}
    own = [{"label": label, "count": 0, "type": None} for label, norm in session.query(
        UserSearchTerm.label, UserSearchTerm.norm).filter(UserSearchTerm.kind == kind.value).order_by(
        UserSearchTerm.label) if norm not in seen]
    return own + shared


def location_paths(session: Session) -> dict[str, list[str]]:
    """normalized location label -> exact geo path, for locations a scan has pinned down."""
    rows = session.query(SearchTerm.norm, SearchTerm.geo_path).filter(
        SearchTerm.kind == TermKind.LOCATION.value, SearchTerm.geo_path.is_not(None)
    )
    return {norm: list(path) for norm, path in rows if path}


def counts(session: Session) -> dict[str, int]:
    return dict(session.query(SearchTerm.kind, func.count()).group_by(SearchTerm.kind).all())


def seed_catalog(session: Session) -> int:
    """Load the built-in vocabulary (idempotent; cheap enough to run on every start)."""
    provinces = [
        Term(name, location_type=LocationType.PROVINCE.value, geo_path=["Canada", name])
        for name, _ in seed_data.CANADIAN_PROVINCES
    ]
    locations = [
        Term("Canada", location_type=LocationType.COUNTRY.value, geo_path=["Canada"]),
        *provinces,
        *(Term(f"{city}, {code}", location_type=LocationType.CITY.value) for city, code in seed_data.CANADIAN_CITIES),
        *(Term(label, location_type=LocationType.REMOTE.value) for label in seed_data.REMOTE_LOCATIONS),
    ]
    return (
        upsert_terms(session, TermKind.LOCATION, locations, TermSource.SEED)
        + upsert_terms(session, TermKind.TITLE, seed_data.JOB_TITLES, TermSource.SEED)
        + upsert_terms(session, TermKind.KEYWORD, seed_data.KEYWORDS, TermSource.SEED)
    )
