"""Search-term catalog: idempotent upserts, source precedence, seeding, and the
market scan (Adzuna faked)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database.base import Base
import app.models  # noqa: F401  register all models on Base.metadata
from app.modules.jobs.catalog import seed_data
from app.modules.jobs.catalog.models import SearchTerm, TermKind, TermSource
from app.modules.jobs.catalog.scan import clean_company, clean_title, scan_market
from app.modules.jobs.catalog.store import Term, location_paths, seed_catalog, suggestions, upsert_terms


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


def test_upsert_is_idempotent_and_dedupes_by_normalized_label(session):
    assert upsert_terms(session, TermKind.LOCATION, ["Québec", "quebec", "Victoria, BC"], TermSource.SCAN) == 2
    assert upsert_terms(session, TermKind.LOCATION, ["QUEBEC", "victoria bc"], TermSource.SCAN) == 0
    assert session.query(SearchTerm).count() == 2


def test_same_label_can_exist_under_different_kinds(session):
    upsert_terms(session, TermKind.KEYWORD, ["Remote"], TermSource.SEED)
    upsert_terms(session, TermKind.LOCATION, ["Remote"], TermSource.SEED)
    assert session.query(SearchTerm).count() == 2


def test_source_only_moves_up_and_scans_refresh_counts_and_paths(session):
    upsert_terms(session, TermKind.LOCATION, ["victoria, bc"], TermSource.USER)
    upsert_terms(session, TermKind.LOCATION, [
        Term("Victoria, BC", job_count=42, location_type="city", geo_path=["Canada", "British Columbia", "Capital", "Victoria"]),
    ], TermSource.SCAN)

    row = session.query(SearchTerm).one()
    assert (row.source, row.label) == ("user", "victoria, bc")  # the user's entry isn't taken over
    assert row.job_count == 42 and row.location_type == "city"
    assert location_paths(session) == {"victoria bc": ["Canada", "British Columbia", "Capital", "Victoria"]}

    upsert_terms(session, TermKind.LOCATION, ["Victoria, BC"], TermSource.SCAN)
    assert session.query(SearchTerm).one().job_count == 42  # no count given = leave it


def test_suggestions_are_ordered_by_popularity(session):
    upsert_terms(session, TermKind.TITLE, [Term("Rare", 1), Term("Common", 90), Term("Unseen")], TermSource.SCAN)
    assert [t["label"] for t in suggestions(session, TermKind.TITLE)] == ["Common", "Rare", "Unseen"]
    assert len(suggestions(session, TermKind.TITLE, limit=1)) == 1


def test_seed_catalog_is_idempotent(session):
    first = seed_catalog(session)
    assert first == (1 + len(seed_data.CANADIAN_PROVINCES) + len(seed_data.CANADIAN_CITIES)
                     + len(seed_data.REMOTE_LOCATIONS) + len(set(seed_data.JOB_TITLES)) + len(set(seed_data.KEYWORDS)))
    assert seed_catalog(session) == 0
    assert location_paths(session)["british columbia"] == ["Canada", "British Columbia"]


@pytest.mark.parametrize("raw, expected", [
    ("Senior Java Developer - Remote (Contract)", "Senior Java Developer"),
    ("SENIOR SOFTWARE ENGINEER", "Senior Software Engineer"),
    ("Senior Java Developer with GCP- Canada", "Senior Java Developer"),
    ("Software Engineer, Backend", "Software Engineer"),
    ("Software Engineer in Test", "Software Engineer in Test"),
    ("Front-End Developer", "Front-End Developer"),
    ("A very long posting title that keeps going on and on", None),
    ("Babysitter wanted", None),
    ("Future Leaders Program", None),
    ("Vans", None),
])
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


class _FakeAdzuna:
    def __init__(self):
        self.calls = 0

    def geodata(self, area):
        self.calls += 1
        if area == ["Canada"]:
            return [(["Canada", "British Columbia"], 900), (["Canada", "Ontario"], 2000)]
        if area == ["Canada", "British Columbia"]:
            return [(["Canada", "British Columbia", "Capital"], 120)]
        return []

    def categories(self):
        self.calls += 1
        return [("it-jobs", "IT Jobs"), ("unknown", "Unknown")]

    def search_postings(self, *, page, per_page, **filters):
        self.calls += 1
        assert filters["category"] == "it-jobs"
        if page > 1:
            return []
        posting = lambda title, company, area, description="": {  # noqa: E731
            "title": title, "company": {"display_name": company}, "location": {"area": area},
            "description": description,
        }
        victoria = ["Canada", "British Columbia", "Capital", "Victoria"]
        return [
            posting("Senior Java Developer - Remote", "Acme", victoria, "Java, Kafka and Spring Boot"),
            posting("SENIOR JAVA DEVELOPER", "Acme", victoria, "Kafka streams"),
            posting("One-off Unicorn Wrangler", "Beta Co", ["Canada", "Ontario", "Toronto", "Toronto"], "Go and gophers"),
        ]


def test_scan_market_fills_every_kind(session):
    seed_catalog(session)
    fake = _FakeAdzuna()

    report = scan_market(session, fake, country="ca", pause_seconds=0, extra_pages={"it-jobs": 1})

    assert report.postings == 3 and not report.errors
    assert report.requests == fake.calls == 1 + 2 + 1 + 2  # tree, 2 provinces, categories, 2 pages of it-jobs
    titles = {t["label"]: t["count"] for t in suggestions(session, TermKind.TITLE)}
    assert titles["Senior Java Developer"] == 2  # seeded title, now with its market count
    assert "One-off Unicorn Wrangler" not in titles  # one-offs aren't offered
    locations = {t["label"]: t for t in suggestions(session, TermKind.LOCATION)}
    assert locations["Victoria, BC"]["count"] == 2 and locations["Victoria, BC"]["type"] == "city"
    assert locations["Capital, BC"]["type"] == "region"
    assert locations["Ontario"]["count"] == 2000
    assert location_paths(session)["victoria bc"] == ["Canada", "British Columbia", "Capital", "Victoria"]
    assert {t["label"] for t in suggestions(session, TermKind.COMPANY)} == {"Acme", "Beta Co"}
    keywords = {t["label"]: t["count"] for t in suggestions(session, TermKind.KEYWORD)}
    assert keywords["Kafka"] == 2 and keywords["Java"] == 2  # per posting, title or description ("JAVA" counts)
    assert keywords["Go"] == 1  # short keywords only match as written ("gophers" doesn't count)


def test_clean_company_drops_ad_copy():
    assert clean_company("  Acme   Corp ") == "Acme Corp"
    assert clean_company("GREAT PAY $17-42 p/h") is None
    assert clean_company("Hiring Now") is None


def test_rescan_drops_scanned_titles_no_longer_seen_but_keeps_seeded_and_user_ones(session):
    upsert_terms(session, TermKind.TITLE, [Term("Stale Engineer", 5)], TermSource.SCAN)
    upsert_terms(session, TermKind.TITLE, ["My Custom Developer"], TermSource.USER)
    seed_catalog(session)

    report = scan_market(session, _FakeAdzuna(), country="ca", pause_seconds=0, extra_pages={})

    titles = {t["label"] for t in suggestions(session, TermKind.TITLE)}
    assert "Stale Engineer" not in titles and report.removed == 1
    assert {"My Custom Developer", "Software Engineer", "Senior Java Developer"} <= titles
