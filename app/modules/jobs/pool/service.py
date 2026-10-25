"""The shared job pool: every open posting of the companies an admin lists
(Platform -> Job sources), pulled from their public careers-site feeds
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable -- free, no key) on a schedule and kept in the
database with the **full** description.

Why: the job-board search API only returns a ~500-character preview of each
description, which limits match analysis and tailoring. The pool gives:

- free search -- `search_pool` runs on the database, so it costs no listing
  requests; every job search merges its results in;
- full descriptions -- a board result whose company and title match a pool
  posting is analyzed with the pool's full text (`full_description_for`).

The pool is public job ads, shared by every user like the search catalog; a
posting becomes a user's own `Job` only when they analyze or track it.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.integrations.job_boards.ashby import AshbySource
from app.integrations.job_boards.base import RawJob
from app.integrations.job_boards.greenhouse import GreenhouseSource
from app.integrations.job_boards.lever import LeverSource
from app.integrations.job_boards.smartrecruiters import SmartRecruitersSource
from app.integrations.job_boards.workable import WorkableSource
from app.modules.jobs import dedupe, places, postings, relevance
from app.modules.jobs.pool.models import PoolCompany, PoolJob
from app.modules.usage import quota

logger = logging.getLogger(__name__)

PROVIDERS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable")
PROVIDER_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
                   "smartrecruiters": "SmartRecruiters", "workable": "Workable"}
_PAUSE_BETWEEN_COMPANIES_SECONDS = 1.0
_sync_lock = threading.Lock()
_status = {"running": False, "done": 0, "total": 0, "started_at": None}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def fetch(provider: str, board: str, known: set[str] | None = None) -> list[RawJob]:
    """Every open posting on one company's feed. `known`: postings whose full
    description the pool already has (SmartRecruiters fetches each one's
    description separately, so those aren't fetched again)."""
    if provider == "smartrecruiters":
        return SmartRecruitersSource(company=board, known=known or set()).discover_jobs(None)
    if provider == "workable":
        return WorkableSource(account=board).discover_jobs(None)
    if provider == "greenhouse":
        return GreenhouseSource(board_token=board).discover_jobs(None)
    if provider == "lever":
        return LeverSource(company=board).discover_jobs(None)
    if provider == "ashby":
        return AshbySource(board=board).discover_jobs(None)
    raise ValueError(f"unknown provider {provider!r}")


# ---- where a posting is -------------------------------------------------------------

_CANADA = re.compile(
    r"\bcanad|\(can\)|toronto|vancouver|montr[eé]al|ottawa|calgary|edmonton|waterloo|kitchener|winnipeg|"
    r"halifax|mississauga|markham|burnaby|saskatoon|regina|qu[eé]bec|ontario|british columbia|alberta|manitoba|"
    r"saskatchewan|nova scotia|new brunswick|newfoundland|prince edward island|,\s*(?:on|bc|ab|qc|mb|sk|ns|nb|nl)\b",
    re.IGNORECASE,
)
_USA = re.compile(r"united states|\busa?\b|\bu\.s\.|new york|san francisco|seattle|chicago|austin|boston|"
                  r"los angeles|denver|atlanta", re.IGNORECASE)
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
# A posting whose own location names another country is there, whatever the
# company's office list says (Elastic lists "Portugal" jobs under a Canadian office).
_ABROAD = re.compile(
    r"\b(?:united kingdom|\buk\b|england|scotland|ireland|dublin|london|germany|berlin|munich|france|paris|spain|"
    r"madrid|barcelona|portugal|lisbon|italy|netherlands|amsterdam|belgium|switzerland|zurich|austria|poland|"
    r"warsaw|czech|romania|bulgaria|greece|athens|hungary|sweden|stockholm|norway|denmark|copenhagen|finland|"
    r"estonia|latvia|lithuania|ukraine|serbia|croatia|turkey|israel|tel aviv|india|bangalore|bengaluru|"
    r"hyderabad|mumbai|pune|delhi|chennai|pakistan|philippines|manila|singapore|japan|tokyo|korea|seoul|china|"
    r"shanghai|beijing|hong kong|taiwan|vietnam|thailand|indonesia|malaysia|australia|sydney|melbourne|"
    r"new zealand|mexico|brazil|s[aã]o paulo|argentina|buenos aires|colombia|bogot[aá]|chile|peru|costa rica|"
    r"south africa|nigeria|kenya|egypt|uae|dubai|saudi|emea|apac|latam)\b",
    re.IGNORECASE,
)


def _place_text(raw: RawJob) -> tuple[str, str | None]:
    """Every place a posting mentions, plus an explicit country code if the feed has one."""
    p = raw.raw_payload or {}
    places = [raw.location or ""]
    code = None
    if raw.source == "lever":
        code = (p.get("country") or "").lower() or None
        places += list((p.get("categories") or {}).get("allLocations") or [])
    elif raw.source == "greenhouse":
        for office in p.get("offices") or []:
            places += [office.get("name") or "", office.get("location") or ""]
    elif raw.source == "smartrecruiters":
        place = p.get("location") or {}
        code = (place.get("country") or "").lower() or None
        places.append(place.get("fullLocation") or "")
    elif raw.source == "workable":
        codes = [(loc.get("countryCode") or "").lower() for loc in p.get("locations") or []]
        code = next((c for c in codes if c), None)
        places += [p.get("country") or ""] + [", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                                     loc.get("country")) if x)
                                              for loc in p.get("locations") or []]
    elif raw.source == "ashby":
        address = ((p.get("address") or {}).get("postalAddress") or {})
        places.append(address.get("addressCountry") or "")
        for extra in p.get("secondaryLocations") or []:
            places.append(extra.get("location") or "")
            places.append(((extra.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
    return " | ".join(x for x in places if x), code


_FEED_WORKPLACES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def feed_workplace(raw: RawJob) -> str | None:
    """The workplace a feed states outright (Ashby, Lever: `workplaceType`), or None.
    It outranks guesses: Ashby marks a hybrid job that allows some remote days `isRemote: true`."""
    value = str((raw.raw_payload or {}).get("workplaceType") or "").lower().replace("-", "").replace("_", "")
    return _FEED_WORKPLACES.get(value)


def classify(raw: RawJob) -> tuple[str | None, bool]:
    """(country code or None, remote?) for one posting."""
    text, code = _place_text(raw)
    p = raw.raw_payload or {}
    stated = feed_workplace(raw)
    remote = bool(_REMOTE.search(text)) or stated == "remote" \
        or (stated is None and p.get("isRemote") is True) or p.get("telecommuting") is True \
        or (raw.source == "smartrecruiters" and (p.get("location") or {}).get("remote") is True)
    own = raw.location or ""  # the posting's own location outranks the company's other offices
    if _CANADA.search(own):
        return "ca", remote
    if _ABROAD.search(own):
        return (code[:2] if code and code != "ca" else None), remote
    if _USA.search(own) and not code:
        return "us", remote
    if _CANADA.search(text):
        return "ca", remote
    if code:
        return code[:2], remote
    if _USA.search(text):
        return "us", remote
    return None, remote


# ---- managing companies --------------------------------------------------------------

_BOARD_URL_HOSTS = {
    "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
    "boards-api.greenhouse.io": "greenhouse", "jobs.lever.co": "lever", "api.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby", "api.ashbyhq.com": "ashby",
    "jobs.smartrecruiters.com": "smartrecruiters", "careers.smartrecruiters.com": "smartrecruiters",
    "api.smartrecruiters.com": "smartrecruiters", "apply.workable.com": "workable",
}
_BOARD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def parse_board(provider: str, value: str) -> tuple[str, str]:
    """(provider, board) from a slug or a careers-page URL, e.g.
    https://jobs.lever.co/acme/123 -> ("lever", "acme")."""
    value = (value or "").strip()
    if "://" in value or "/" in value:
        url = urlparse(value if "://" in value else f"https://{value}")
        host = (url.hostname or "").lower()
        if host not in _BOARD_URL_HOSTS:
            raise ValueError("That isn't a Greenhouse, Lever, Ashby, SmartRecruiters or Workable careers-page URL.")
        provider = _BOARD_URL_HOSTS[host]
        parts = [part for part in url.path.split("/") if part]
        skip = {"v1", "boards", "v0", "postings", "posting-api", "job-board", "embed", "job_board", "api",
                "companies", "widget", "accounts"}
        parts = [part for part in parts if part not in skip]
        value = parts[0] if parts else ""
    if provider not in PROVIDERS:
        raise ValueError("Choose Greenhouse, Lever, Ashby, SmartRecruiters or Workable.")
    if provider == "workable" and value.lower() == "j":
        raise ValueError("That's a link to one Workable job. Use the company's jobs page "
                         "(https://apply.workable.com/<company>/).")
    if not _BOARD_RE.match(value):
        raise ValueError("Enter the company's board name (e.g. acme) or its careers-page URL.")
    return provider, value.lower()


def _pretty_name(raw_jobs: list[RawJob], board: str) -> str:
    for raw in raw_jobs:
        name = (raw.raw_payload or {}).get("company_name")
        if name:
            return name
    return board.replace("-", " ").replace("_", " ").title()


def add_company(session: Session, provider: str, board: str, *, fetcher=None) -> PoolCompany:
    """Add (and immediately load) a company feed. Raises ValueError with a
    message for the admin when the board doesn't exist or can't be read."""
    provider, board = parse_board(provider, board)
    if session.query(PoolCompany).filter_by(provider=provider, board=board).first():
        raise ValueError(f"{board} on {PROVIDER_LABELS[provider]} is already in the list.")
    try:
        raw_jobs = (fetcher or fetch)(provider, board)
    except Exception as exc:
        raise ValueError(f"Couldn't read {board} on {PROVIDER_LABELS[provider]} -- check the name "
                         f"({type(exc).__name__}).") from exc
    if provider == "smartrecruiters" and not raw_jobs:  # an unknown company is an empty list, not an error
        raise ValueError(f"{board} on SmartRecruiters has no jobs in Canada right now, or isn't a company "
                         "there -- check the name in its careers-page URL.")
    company = PoolCompany(provider=provider, board=board, name=_pretty_name(raw_jobs, board), enabled=True)
    session.add(company)
    session.flush()
    _store(session, company, raw_jobs)
    session.commit()
    return company


def _store(session: Session, company: PoolCompany, raw_jobs: list[RawJob]) -> None:
    """Upsert this company's postings; drop the ones it no longer lists."""
    now = _utcnow()
    existing = {job.external_id: job for job in session.query(PoolJob).filter_by(company_id=company.id)}
    seen: set[str] = set()
    canada = 0
    for raw in raw_jobs:
        external_id = (raw.external_id or raw.source_url or "")[:120]
        if not external_id or external_id in seen or not raw.source_url:
            continue
        seen.add(external_id)
        country, remote = classify(raw)
        canada += country == "ca"
        salary = _salary(raw)
        values = dict(
            workplace=feed_workplace(raw) or postings.workplace(raw.title, raw.location, raw.description or "", remote),
            salary_min=salary[0], salary_max=salary[1], currency=salary[2],
            provider=company.provider, company=(company.name or raw.company)[:200], title=raw.title[:300],
            location=(raw.location or None) and raw.location[:500], country=country, remote=remote,
            employment_type=(raw.employment_type or None) and raw.employment_type[:64],
            description=raw.description or "", url=raw.source_url[:1000], date_posted=_naive_utc(raw.date_posted),
            last_seen_at=now,
        )
        job = existing.get(external_id)
        if job is not None and not values["description"]:
            values["description"] = job.description  # not fetched again this time (SmartRecruiters)
        if job is None:
            session.add(PoolJob(company_id=company.id, external_id=external_id, first_seen_at=now, **values))
        else:
            for key, value in values.items():
                setattr(job, key, value)
    gone = [job.id for external_id, job in existing.items() if external_id not in seen]
    if gone:
        session.query(PoolJob).filter(PoolJob.id.in_(gone)).delete(synchronize_session=False)
    company.job_count = len(seen)
    company.canada_count = canada
    company.last_synced_at = now
    company.last_status = "ok"
    company.last_error = None


def sync_company(session: Session, company: PoolCompany, *, fetcher=None) -> bool:
    """Refresh one company. On failure its postings are kept and the error noted."""
    try:
        if company.provider == "smartrecruiters":  # descriptions the pool has needn't be fetched again
            known = {external_id for (external_id,) in session.query(PoolJob.external_id).filter(
                PoolJob.company_id == company.id, PoolJob.description != "")}
            raw_jobs = (fetcher or fetch)(company.provider, company.board, known)
        else:
            raw_jobs = (fetcher or fetch)(company.provider, company.board)
        _store(session, company, raw_jobs)  # a posting it can't read fails this company only
        session.commit()
    except Exception as exc:
        session.rollback()
        company.last_synced_at = _utcnow()
        company.last_status = "error"
        company.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        session.commit()
        logger.warning("Job pool: couldn't refresh %s/%s: %s", company.provider, company.board, exc)
        return False
    return True


def sync_all(session_factory, *, max_age_hours: float | None = None, fetcher=None,
             pause: float = _PAUSE_BETWEEN_COMPANIES_SECONDS) -> dict:
    """Refresh every enabled company (or only those older than `max_age_hours`).
    One run at a time; returns counts."""
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        with session_factory() as session:
            query = session.query(PoolCompany.id).filter(PoolCompany.enabled.is_(True))
            if max_age_hours is not None:
                cutoff = _utcnow() - timedelta(hours=max_age_hours)
                query = query.filter(or_(PoolCompany.last_synced_at.is_(None), PoolCompany.last_synced_at < cutoff))
            ids = [cid for (cid,) in query.order_by(PoolCompany.last_synced_at.is_not(None), PoolCompany.last_synced_at)]
        _status.update(running=True, done=0, total=len(ids), started_at=_utcnow())
        ok = failed = 0
        for i, company_id in enumerate(ids):
            with session_factory() as session:
                company = session.get(PoolCompany, company_id)
                if company is None:
                    continue
                if sync_company(session, company, fetcher=fetcher):
                    ok += 1
                else:
                    failed += 1
            _status["done"] = i + 1
            if pause and i + 1 < len(ids):
                time.sleep(pause)
        if ids:
            log_event(logger, "JOB_POOL_REFRESHED", companies=len(ids), ok=ok, failed=failed)
        return {"ok": ok, "failed": failed}
    finally:
        _status["running"] = False
        _sync_lock.release()


def start_background_sync(session_factory) -> bool:
    if _status["running"]:
        return False
    threading.Thread(target=sync_all, args=(session_factory,), daemon=True, name="job-pool-sync").start()
    return True


def sync_status() -> dict:
    return dict(_status)


def suggestions(session: Session) -> list[tuple[str, str, str, int]]:
    """The suggested employers (pool/suggested.py) not in the list yet."""
    from app.modules.jobs.pool.suggested import SUGGESTED

    have = set(session.query(PoolCompany.provider, PoolCompany.board).all())
    return [s for s in SUGGESTED if (s[0], s[1]) not in have]


def add_suggested(session_factory, *, fetcher=None, pause: float = _PAUSE_BETWEEN_COMPANIES_SECONDS) -> dict:
    """Add every suggested employer not in the list yet, one at a time. One
    run at a time (shares the refresh lock); returns counts."""
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        with session_factory() as session:
            todo = suggestions(session)
        _status.update(running=True, done=0, total=len(todo), started_at=_utcnow())
        added = failed = 0
        for i, (provider, board, _name, _count) in enumerate(todo):
            with session_factory() as session:
                try:
                    add_company(session, provider, board, fetcher=fetcher)
                    added += 1
                except ValueError as exc:
                    session.rollback()
                    failed += 1
                    logger.warning("Job pool: couldn't add suggested %s/%s: %s", provider, board, exc)
            _status["done"] = i + 1
            if pause and i + 1 < len(todo):
                time.sleep(pause)
        log_event(logger, "JOB_POOL_SUGGESTED_ADDED", added=added, failed=failed)
        return {"added": added, "failed": failed}
    finally:
        _status["running"] = False
        _sync_lock.release()


def start_adding_suggested(session_factory) -> bool:
    if _status["running"]:
        return False
    threading.Thread(target=add_suggested, args=(session_factory,), daemon=True, name="job-pool-suggested").start()
    return True


# ---- what's current ----------------------------------------------------------------

_MIN_UNCHECKED_HOURS = 24


def fresh_conditions(session: Session) -> list:
    """SQL conditions for pool postings fit to show a job seeker:
    - the company's feed listed it at its last successful refresh within the
      last two refreshes (and at least a day): a feed that keeps failing can't
      say whether the job is still open, so its jobs are hidden until it's read
      again (a job the feed stops listing is deleted at once, in `_store`);
    - posted within `platform.pool_max_job_age_days` (an undated posting
      counts as current while its feed lists it)."""
    platform = quota.platform_limits(session)
    now = _utcnow()
    conditions = [PoolCompany.enabled.is_(True),
                  or_(PoolJob.date_posted.is_(None), PoolJob.date_posted >= dedupe.cutoff(platform.pool_max_job_age_days))]
    if platform.pool_refresh_hours:  # refreshing off: nothing to measure against
        hours = max(2 * platform.pool_refresh_hours, _MIN_UNCHECKED_HOURS)
        conditions.append(PoolJob.last_seen_at >= now - timedelta(hours=hours))
    return conditions


def fresh_jobs(session: Session):
    """Pool postings (joined to their company) fit to show: `fresh_conditions`."""
    return session.query(PoolJob).join(PoolCompany, PoolJob.company_id == PoolCompany.id).filter(
        *fresh_conditions(session))


def is_fresh(session: Session, job_id: int) -> bool:
    return fresh_jobs(session).filter(PoolJob.id == job_id).first() is not None


# ---- searching ---------------------------------------------------------------------

def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _place(location: str) -> tuple[str | None, bool]:
    """'Remote Canada' -> (None, remote); 'Toronto, ON' -> ('toronto', False)."""
    remote = bool(_REMOTE.search(location))
    rest = _REMOTE.sub(" ", location)
    rest = re.sub(r"\b(?:in|only|anywhere|canada)\b", " ", rest, flags=re.IGNORECASE)
    rest = rest.split(",")[0].strip(" -()/")
    return (rest.lower() or None), remote


def _row_key(row: PoolJob) -> tuple[str, str, str]:
    return dedupe.key(row.company, row.title, row.location)


@dataclass
class PoolResults:
    jobs: list[RawJob]
    has_next: bool
    total: int


def _salary(raw: RawJob) -> tuple[int | None, int | None, str | None]:
    p = raw.raw_payload or {}
    if raw.source == "lever" and isinstance(p.get("salaryRange"), dict):
        r = p["salaryRange"]
        if str(r.get("interval", "per-year-salary")).startswith("per-year") and (r.get("min") or r.get("max")):
            return r.get("min"), r.get("max"), r.get("currency")
    if raw.source == "ashby":
        summary = (p.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
        return postings.parse_salary(summary)
    return None, None, None


def to_raw_job(job: PoolJob) -> RawJob:
    payload = {"pool_id": job.id, "workplace": job.workplace}
    if job.salary_min or job.salary_max:
        payload["salary"] = (job.salary_min, job.salary_max, job.currency)
    return RawJob(
        external_id=job.external_id, source=job.provider, source_url=job.url, company=job.company, title=job.title,
        location=job.location, description=job.description, date_posted=job.date_posted,
        employment_type=job.employment_type, raw_payload=payload,
    )


def search_pool(session: Session, titles: list[str], locations: list[str], keywords: list[str], country: str, *,
                page: int = 1, per_page: int = 20, radius_km: int | None = None) -> PoolResults:
    """Postings matching the titles (best match first), keywords and places.

    With `radius_km`, a place that is a town or city (app/modules/jobs/places.py)
    matches jobs within that distance of it, measured from the places named in
    each job's location; other places (a province, "Remote") match by name.
    """
    query = fresh_jobs(session)
    country = (country or "").strip().lower()
    if country:
        query = query.filter(PoolJob.country == country)
    titles = [t.strip() for t in titles if t.strip()]
    keywords = [k.strip() for k in keywords if k.strip()]
    if titles:  # a job title, keyword or company name -- or a synonym (relevance.py)
        query = query.filter(or_(*(or_(and_(*(PoolJob.title.ilike(_like(w), escape="\\") for w in words)),
                                       PoolJob.company.ilike(_like(" ".join(words)), escape="\\"))
                                   for t in titles
                                   for words in {*relevance.variants(t), *relevance.related(t), tuple(t.split())})))
    place_filters = []
    origins = []  # places searched by distance: checked below, row by row
    named: set[int] = set()  # rows matching a place searched by name, when others are by distance
    for location in locations:
        origin = places.center(location) if radius_km and not _REMOTE.search(location) else None
        if origin is not None:
            origins.append(origin)
            continue
        place, remote = _place(location)
        conditions = []
        if remote:
            conditions.append(PoolJob.remote.is_(True))
        if place:
            conditions.append(PoolJob.location.ilike(_like(place), escape="\\"))
        if conditions:
            place_filters.append(and_(*conditions))
    if place_filters and not origins:
        query = query.filter(or_(*place_filters))
    elif place_filters:  # some places by name, others by distance: a row may match either
        named = {row_id for (row_id,) in query.filter(or_(*place_filters)).with_entities(PoolJob.id)}
    if keywords:
        query = query.filter(or_(*(or_(PoolJob.title.ilike(_like(w), escape="\\"),
                                       PoolJob.description.ilike(_like(w), escape="\\")) for w in keywords)))
    query = query.order_by(PoolJob.date_posted.is_(None), PoolJob.date_posted.desc(), PoolJob.id.desc())
    page = max(page, 1)
    if not (titles or keywords or origins):
        total = query.count()
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
        rows = dedupe.unique_by(rows, _row_key)  # the same job in one city, listed twice
        return PoolResults(jobs=[to_raw_job(r) for r in rows], has_next=page * per_page < total, total=total)

    # The database narrows by substring; whole words and synonyms are checked
    # here. Best title match first, newest first within it (the sort is stable).
    rows = query.all()
    if origins:
        rows = [r for r in rows if r.id in named
                or any(places.within(r.location, r.remote, origin, radius_km) for origin in origins)]
    if keywords:
        rows = [r for r in rows if relevance.mentions(keywords, r.title, r.description)]
    if titles:
        ranked = [(relevance.tier(titles, r.title, r.company), r) for r in rows]
        rows = [r for t, r in sorted(ranked, key=lambda pair: -pair[0]) if t >= relevance.RELEVANT]
    rows = dedupe.unique_by(rows, _row_key)  # best match and newest first, so that copy is kept
    total = len(rows)
    shown = rows[(page - 1) * per_page: page * per_page]
    return PoolResults(jobs=[to_raw_job(r) for r in shown], has_next=page * per_page < total, total=total)


# ---- full descriptions for job-board results ------------------------------------------------

_COMPANY_SUFFIX = re.compile(r"\b(inc|ltd|llc|corp|corporation|co|company|limited|technologies|canada)\b\.?",
                             re.IGNORECASE)


def normalize(value: str, *, company: bool = False) -> str:
    value = (value or "").lower()
    if company:
        value = _COMPANY_SUFFIX.sub(" ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def find_match(session: Session, company: str, title: str) -> PoolJob | None:
    """The pool posting for the same company and job title, if the pool has it."""
    norm_company, norm_title = normalize(company, company=True), normalize(title)
    if not norm_company or not norm_title:
        return None
    candidates = session.query(PoolJob).join(PoolCompany, PoolJob.company_id == PoolCompany.id).filter(
        PoolCompany.enabled.is_(True), func.lower(PoolJob.title) == title.strip().lower()).limit(50).all()
    if not candidates:  # titles differ in punctuation/case only
        first_word = norm_title.split()[0]
        candidates = session.query(PoolJob).filter(PoolJob.title.ilike(_like(first_word), escape="\\")).limit(500).all()
    for job in candidates:
        if normalize(job.title) == norm_title and normalize(job.company, company=True) == norm_company:
            return job
    return None


def full_description_for(session: Session, company: str, title: str) -> str | None:
    match = find_match(session, company, title)
    return match.description if match is not None and match.description else None


def totals(session: Session) -> dict:
    count = lambda q: q.scalar() or 0  # noqa: E731
    return {
        "jobs": count(session.query(func.count(PoolJob.id))),
        "canada": count(session.query(func.count(PoolJob.id)).filter(PoolJob.country == "ca")),
        "companies": count(session.query(func.count(PoolCompany.id))),
        "enabled": count(session.query(func.count(PoolCompany.id)).filter(PoolCompany.enabled.is_(True))),
    }
