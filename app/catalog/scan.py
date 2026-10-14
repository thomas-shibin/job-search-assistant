"""Market scan: a quick, quota-friendly sweep of Adzuna across a whole country
that fills the search-term catalog with what the market actually uses --
provinces and regions (from the location tree), cities, job titles and
companies (from a sample of recent postings in every category), and how
often each known keyword appears.

Cost: 1 + provinces (~14) location-tree requests, 1 category list, and
`pages_per_category` (plus `extra_pages`) postings requests per category --
roughly 50 requests for Canada, run one at a time with a pause so the
per-minute limit isn't hit. Meant to run occasionally (Settings -> Refresh
catalog, or `python cli.py catalog-scan`), not on every scheduled scan.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.catalog.seed_data import PROVINCE_CODES
from app.catalog.store import Term, upsert_terms
from app.models.catalog import LocationType, SearchTerm, TermKind, TermSource
from app.sources.adzuna import AdzunaSource
from app.core.logging import log_event
from app.shared.terms import normalize_term

logger = logging.getLogger(__name__)

_COUNTRY_ROOTS = {"ca": "Canada", "us": "US", "gb": "UK", "au": "Australia", "in": "India", "de": "Deutschland"}
_TITLE_CUT = re.compile(r"\s+[-–—|/]\s+|(?<=\S)[-–]\s|\s*[(\[|,:]|\s+(?:with|for|at)\s+", re.I)
_TITLE_NOISE = re.compile(r"\b(?:remote|hybrid|on-?site|contract|permanent|full[- ]time|part[- ]time|urgent|new)\b\.?$", re.I)
_MIN_TITLE_COUNT = 2  # a title must recur to be offered; one-offs are mostly employer-specific wording
# A real job title names a role; "Babysitter wanted" / "Future Leaders Program" don't.
_ROLE_WORDS = {
    "engineer", "developer", "programmer", "architect", "analyst", "scientist", "designer", "manager", "director",
    "lead", "head", "president", "officer", "administrator", "specialist", "consultant", "coordinator",
    "assistant", "associate", "advisor", "adviser", "representative", "agent", "technician", "technologist",
    "technical", "operator", "supervisor", "accountant", "bookkeeper", "auditor", "controller", "clerk",
    "nurse", "physician", "therapist", "pharmacist", "teacher", "instructor", "tutor", "educator", "driver",
    "mechanic", "electrician", "plumber", "carpenter", "welder", "machinist", "labourer", "laborer", "worker",
    "cook", "chef", "server", "cashier", "receptionist", "recruiter", "writer", "editor", "researcher",
    "lawyer", "paralegal", "counsel", "planner", "estimator", "inspector", "owner", "partner", "intern",
    "apprentice", "tester", "strategist", "producer", "buyer", "dispatcher", "caregiver", "aide", "attendant",
    "superintendent", "foreman", "captain", "pilot", "dentist", "hygienist", "veterinarian", "psychologist",
    "counsellor", "counselor", "salesperson", "merchandiser", "handler", "packer", "picker", "cleaner",
    "janitor", "custodian", "guard", "stylist", "barista", "bartender", "housekeeper", "nanny", "babysitter",
    "scrum", "devops", "sre", "cto", "cio", "cfo", "vp",
}
_NOT_A_TITLE = re.compile(r"\b(?:wanted|program|programme|opportunit\w*|participant|hiring|jobs?|career fair)\b", re.I)
_NOT_A_COMPANY = re.compile(r"[$@]|\d\s*(?:/\s*h|p/h)|\b(?:pay|hiring|jobs?|wanted|apply|urgent)\b", re.I)


@dataclass
class ScanReport:
    requests: int = 0
    postings: int = 0
    new_terms: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    removed: int = 0


def clean_title(raw: str) -> str | None:
    """"Senior Java Developer - Remote (Contract)" -> "Senior Java Developer"."""
    title = _TITLE_CUT.split(raw.strip(), maxsplit=1)[0]
    previous = None
    while previous != title:
        previous, title = title, _TITLE_NOISE.sub("", title).strip(" -–")
    title = " ".join(title.split())
    if not 2 <= len(title) <= 60 or len(title.split()) > 6:
        return None
    if title.isupper() or title.islower():
        title = title.title()
    words = {w.strip(".&/-").lower() for w in re.split(r"[\s/]+", title)}
    if _NOT_A_TITLE.search(title) or not words & _ROLE_WORDS:
        return None
    return title


def clean_company(raw: str) -> str | None:
    """Employer name, or None for ad copy posing as one ("GREAT PAY $17-42 p/h")."""
    name = " ".join(raw.split())
    if not name or len(name) > 80 or _NOT_A_COMPANY.search(name):
        return None
    return name


def _keyword_pattern(keyword: str) -> re.Pattern:
    flags = 0 if len(keyword) <= 2 else re.IGNORECASE  # "Go", "R", "C" only as written
    return re.compile(rf"(?<![\w+#.]){re.escape(keyword)}(?![\w+#])", flags)


def _place_label(area: list[str]) -> tuple[str, str] | None:
    """Adzuna area path -> (label, LocationType) for a city or region."""
    if len(area) < 3:
        return None
    code = PROVINCE_CODES.get(area[1], area[1])
    if len(area) >= 4:
        return f"{area[3]}, {code}", LocationType.CITY.value
    return f"{area[2]}, {code}", LocationType.REGION.value


def scan_market(
    session: Session, adzuna: AdzunaSource, *, country: str = "ca", pages_per_category: int = 1,
    extra_pages: dict[str, int] | None = None, pause_seconds: float = 2.5,
    progress: Callable[[str], None] = lambda message: None,
) -> ScanReport:
    report = ScanReport()
    extra_pages = {"it-jobs": 4, "engineering-jobs": 1} if extra_pages is None else extra_pages
    root = _COUNTRY_ROOTS.get(country, country.upper())

    def call(description: str, fn, *args, **kwargs):
        progress(description)
        if report.requests:
            time.sleep(pause_seconds)
        report.requests += 1
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # one failed request shouldn't sink the scan
            report.errors.append(f"{description}: {exc}")
            logger.warning("Catalog scan request failed (%s): %s", description, exc)
            return None

    # 1. Location tree: provinces, then each province's regions.
    places: dict[str, Term] = {}
    for area, count in call("Location tree", adzuna.geodata, [root]) or []:
        if len(area) == 2:
            places[area[1]] = Term(area[1], count, LocationType.PROVINCE.value, area)
            for sub_area, sub_count in call(f"Regions of {area[1]}", adzuna.geodata, area) or []:
                if (label := _place_label(sub_area)):
                    places[label[0]] = Term(label[0], sub_count, label[1], sub_area)

    # 2. A sample of recent postings from every category.
    postings: list[dict] = []
    for tag, label in call("Job categories", adzuna.categories) or []:
        if tag == "unknown":
            continue
        for page in range(1, pages_per_category + extra_pages.get(tag, 0) + 1):
            batch = call(f"{label}, page {page}", adzuna.search_postings, page=page, per_page=50,
                         category=tag, sort_by="date")
            postings.extend(batch or [])
    report.postings = len(postings)

    titles: Counter[str] = Counter()
    title_labels: dict[str, str] = {}
    companies: Counter[str] = Counter()
    cities: Counter[str] = Counter()
    for posting in postings:
        if (title := clean_title(posting.get("title") or "")):
            norm = normalize_term(title)
            titles[norm] += 1
            title_labels.setdefault(norm, title)
        if (company := clean_company((posting.get("company") or {}).get("display_name") or "")):
            companies[company] += 1
        area = (posting.get("location") or {}).get("area") or []
        if (place := _place_label(area)) and place[1] == LocationType.CITY.value:
            cities[place[0]] += 1
            places.setdefault(place[0], Term(place[0], 0, place[1], area[:4]))
    for label, count in cities.items():
        places[label].job_count = max(places[label].job_count or 0, count)

    # 3. Keyword popularity, for every keyword already in the catalog.
    texts = [f"{p.get('title') or ''} {p.get('description') or ''}" for p in postings]
    keyword_terms = []
    for (keyword,) in session.query(SearchTerm.label).filter(SearchTerm.kind == TermKind.KEYWORD.value):
        pattern = _keyword_pattern(keyword)
        keyword_terms.append(Term(keyword, sum(1 for text in texts if pattern.search(text))))

    # Popularity is relative to this scan: clear the old counts first.
    session.query(SearchTerm).filter(SearchTerm.kind.in_([k.value for k in TermKind])).update(
        {SearchTerm.job_count: 0}, synchronize_session=False
    )
    report.new_terms = {
        "location": upsert_terms(session, TermKind.LOCATION, places.values(), TermSource.SCAN),
        "title": upsert_terms(
            session, TermKind.TITLE,
            [Term(title_labels[n], c) for n, c in titles.items() if c >= _MIN_TITLE_COUNT], TermSource.SCAN,
        ),
        "company": upsert_terms(session, TermKind.COMPANY, [Term(n, c) for n, c in companies.items()], TermSource.SCAN),
        "keyword": upsert_terms(session, TermKind.KEYWORD, keyword_terms, TermSource.SCAN),
    }
    # Scanned titles/companies no longer in the market drop out (seeded and
    # user-added ones always stay). Locations stay: the tree is stable.
    report.removed = session.query(SearchTerm).filter(
        SearchTerm.kind.in_([TermKind.TITLE.value, TermKind.COMPANY.value]),
        SearchTerm.source == TermSource.SCAN.value, SearchTerm.job_count == 0,
    ).delete(synchronize_session=False)
    session.commit()
    log_event(logger, "CATALOG_SCAN_COMPLETE", removed=report.removed, requests=report.requests, postings=report.postings,
              errors=len(report.errors), **{f"new_{k}": v for k, v in report.new_terms.items()})
    return report


def plural(n: int, kind: str) -> str:
    return f"{n} {'companies' if kind == 'company' else kind + 's'}" if n != 1 else f"1 {kind}"


# ---- Background run for the dashboard --------------------------------------

_status: dict = {"running": False, "message": None, "finished_at": None}
_status_lock = threading.Lock()


def scan_status() -> dict:
    with _status_lock:
        return dict(_status)


def start_background_scan(session_factory, adzuna: AdzunaSource, country: str) -> bool:
    """Run `scan_market` on a thread; False if one is already running."""
    with _status_lock:
        if _status["running"]:
            return False
        _status.update(running=True, message="Starting…", finished_at=None)

    def set_message(message: str) -> None:
        with _status_lock:
            _status["message"] = message

    def run() -> None:
        try:
            with session_factory() as session:
                report = scan_market(session, adzuna, country=country, progress=set_message)
            added = ", ".join(plural(n, kind) for kind, n in report.new_terms.items() if n) or "no new terms"
            summary = f"Scanned {report.postings} postings with {report.requests} requests: added {added}."
            if report.errors:
                summary += f" {len(report.errors)} requests failed."
        except Exception as exc:
            logger.exception("Catalog scan failed")
            summary = f"Catalog scan failed: {exc}"
        with _status_lock:
            _status.update(running=False, message=summary, finished_at=datetime.now(timezone.utc))

    threading.Thread(target=run, name="catalog-scan", daemon=True).start()
    return True
