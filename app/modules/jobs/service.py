"""Job search as the user sees it: one page of results merged from the live
job board and the company-careers job pool, behind the hourly search limit,
plus saving a search result as the user's own job.

Depends on the job-board integration (app/integrations/job_boards), the job
pool (app/modules/jobs/pool), the hourly limits (app/modules/usage/quota) and,
to badge results the user already tracks, the applications and matching
modules. Nothing here knows about HTTP.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.config.schema import GlobalSearchConfig
from app.integrations.job_boards.adzuna import AdzunaSource
from app.integrations.job_boards.base import RawJob
from app.integrations.job_boards.settings import AdzunaSettings
from app.modules.applications.service import latest_match_analysis
from app.modules.auth.models import User
from app.modules.jobs import postings
from app.modules.jobs.catalog.store import location_paths
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.jobs.models import Job, SavedJob
from app.modules.jobs.pool import service as pool
from app.modules.jobs.pool.models import PoolJob
from app.modules.jobs.ranking import best_match_first
from app.modules.jobs.search import query_pairs, search_jobs
from app.modules.matching.fit import latest_assessment
from app.modules.usage import quota
from app.modules.usage.models import ActivityEvent, ActivityKind
from app.shared.terms import split_places, split_terms

logger = logging.getLogger(__name__)

CARDS_PER_PAGE = 40
POOL_PER_PAGE = 20


def tracked_by_external_id(session: Session, external_ids: list[str]) -> dict[str, dict]:
    """For search results already saved as jobs: their id, score and AI fit."""
    if not external_ids:
        return {}
    rows = (
        session.query(Job.external_id, Job.id)
        .filter(Job.source.in_(("adzuna",) + pool.PROVIDERS), Job.external_id.in_(external_ids))
        .all()
    )
    tracked = {}
    for external_id, job_id in rows:
        analysis = latest_match_analysis(session, job_id)
        tracked[external_id] = {
            "job_id": job_id,
            "score": analysis.overall_score if analysis else None,
            "fit": latest_assessment(session, job_id),
        }
    return tracked


def search_for_page(
    session: Session, *, titles: list[str], locations: list[str], keywords: list[str], country: str, page: int
) -> dict:
    """One page of results: the live job-board search across every title x
    location (app/modules/jobs/search.py) merged with the free job pool of
    companies' careers sites (app/modules/jobs/pool), which carries full
    descriptions -- a board result the pool also has is shown from the pool.
    Nothing here saves anything: a posting only becomes a tracked `Job` when
    the user analyzes or tracks it. An empty page past the last real results
    is the "no more results" signal.

    Returns:
        ``{"error", "cards", "page", "has_next", "failed", "queries", "requests"}``;
        ``error`` is set only when neither source produced anything.
    """
    page = max(page, 1)
    result = {"error": None, "cards": [], "page": page, "has_next": False, "failed": [],
              "queries": len(query_pairs(titles, locations)), "requests": 0}

    board_jobs: list[RawJob] = []
    board_error = None
    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        logger.error("Job search is not configured (listings provider keys missing)")
        board_error = "Job search is temporarily unavailable. Please try again later."
    else:
        source = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
        per_query = max(10, min(50, CARDS_PER_PAGE // result["queries"]))
        try:
            found = search_jobs(source, titles, locations, page=page, per_page=per_query,
                                keywords=keywords, areas=location_paths(session))
        except Exception:
            logger.exception("Job search failed")
            board_error = "Job search is temporarily unavailable. Please try again shortly."
        else:
            board_jobs = found.jobs
            result.update(has_next=found.has_next, failed=found.failed_queries, requests=found.requests)

    try:
        pooled = pool.search_pool(session, titles, locations, keywords, country, page=page, per_page=POOL_PER_PAGE)
    except Exception:
        logger.exception("Job pool search failed")
        pooled = pool.PoolResults(jobs=[], has_next=False, total=0)
    if board_error and not pooled.jobs:
        result["error"] = board_error
        return result

    jobs = _merge_results(pooled.jobs, board_jobs)
    tracked = tracked_by_external_id(session, [j.external_id for j in jobs if j.external_id])
    cards = [{"raw": j, "tracked": tracked.get(j.external_id)} for j in jobs]
    result["cards"] = best_match_first(cards, lambda card: card["tracked"])
    result["has_next"] = result["has_next"] or pooled.has_next
    return result


def _posted_at(job: RawJob) -> float:
    if job.date_posted is None:
        return 0.0
    posted = job.date_posted if job.date_posted.tzinfo else job.date_posted.replace(tzinfo=timezone.utc)
    return posted.timestamp()


def _merge_results(pool_jobs: list[RawJob], board_jobs: list[RawJob]) -> list[RawJob]:
    """Both lists, newest first; a board posting the pool also has (same company
    and title) is dropped -- the pool's copy has the full description."""
    in_pool = {(pool.normalize(j.company, company=True), pool.normalize(j.title)) for j in pool_jobs}
    merged = list(pool_jobs) + [j for j in board_jobs
                                if (pool.normalize(j.company, company=True), pool.normalize(j.title)) not in in_pool]
    return sorted(merged, key=_posted_at, reverse=True)


def _search_summary(titles: list[str], locations: list[str], keywords: list[str], country: str) -> str:
    parts = [", ".join(titles) or "any title", "in " + (", ".join(locations) or "anywhere")]
    if keywords:
        parts.append("with " + ", ".join(keywords))
    return " ".join(parts) + f" ({country.upper()})"


def limited_search(session: Session, user: User, titles: list[str], locations: list[str], keywords: list[str],
                   country: str, page: int, query_string: str) -> dict:
    """The job search behind the hourly search limit (app/modules/usage/quota.py):
    a new search counts once; its other pages, or running it again within the
    hour, are free. Over the limit, the result offers the last search instead.

    Returns:
        The `search_for_page` result plus ``allowance``; over the limit,
        ``limited=True``, an ``error`` message and ``last_search``.
    """
    platform = quota.platform_limits(session)
    fingerprint = quota.search_fingerprint(titles, locations, keywords, country)
    summary = _search_summary(titles, locations, keywords, country)
    try:
        charge = quota.charge_search(
            session, user, fingerprint=fingerprint, page=page, summary=summary, platform=platform,
            meta={"titles": titles, "locations": locations, "keywords": keywords, "country": country,
                  "query_string": query_string},
        )
    except quota.LimitReached as exc:
        current = exc.allowance or quota.allowance(session, user, quota.SEARCH, platform)
        return {"error": str(exc), "limited": True, "cards": [], "page": page, "has_next": False, "failed": [],
                "queries": 0, "allowance": current, "last_search": _last_search(session, user, fingerprint)}
    search = search_for_page(session, titles=titles, locations=locations, keywords=keywords, country=country,
                             page=page)
    quota.settle_search(session, user, charge, provider_calls=search.get("requests", 0), failed=bool(search.get("error")),
                        summary=summary, fingerprint=fingerprint, page=page)
    search["has_next"] = search["has_next"] and page < platform.max_search_pages
    search["allowance"] = charge.allowance
    return search


def _last_search(session: Session, user: User, fingerprint: str) -> dict | None:
    """The most recent search counted this hour (other than this one), to offer
    when the limit stops a new one."""
    event = session.query(ActivityEvent).filter(
        ActivityEvent.user_id == user.id, ActivityEvent.kind == ActivityKind.SEARCH,
        ActivityEvent.ref != fingerprint, ActivityEvent.created_at >= quota.utcnow() - quota.WINDOW,
    ).order_by(ActivityEvent.created_at.desc()).first()
    if event is None or not (event.meta or {}).get("query_string"):
        return None
    return {"summary": event.summary, "url": "/jobs?" + event.meta["query_string"]}


def picked(values: list[str], split) -> list[str]:
    """Picker values (one per tag) -- or a comma list typed into a URL -- as a clean list."""
    return list(dict.fromkeys(term for value in values for term in split(value)))


def search_terms(session: Session, q: str | None, l: str | None, titles, locations, keywords,
                 country: str | None) -> tuple[list[str], list[str], list[str], str, str | None]:
    """What to search: the boxes as typed (comma-separated), else the
    `titles`/`locations`/`keywords` lists of older links, else the saved
    default search (Settings -> Job search).

    Returns:
        (titles, locations, keywords, country, a settings error message or None).
    """
    config_error = None
    try:
        search_config = user_config(session).search
    except ConfigError as exc:
        config_error = f"Settings are invalid (fix them on the Settings page): {'; '.join(exc.errors)}"
        search_config = GlobalSearchConfig()
    typed = q is not None or l is not None or titles is not None or locations is not None
    title_list = split_terms(q or "") if q is not None else (
        picked(titles, split_terms) if titles is not None else ([] if typed else search_config.titles))
    location_list = split_places(l or "") if l is not None else (
        picked(locations, split_places) if locations is not None else ([] if typed else search_config.locations))
    keyword_list = picked(keywords, split_terms) if keywords is not None else ([] if typed else search_config.keywords)
    return title_list, location_list, keyword_list, (country or search_config.country).strip().lower(), config_error


def saved_job_keys(session: Session) -> dict[tuple[str, str], int]:
    """(source, external id) -> job id, for every job the user saved."""
    rows = session.query(Job.source, Job.external_id, Job.id).join(SavedJob, SavedJob.job_id == Job.id).all()
    return {(source, external_id): job_id for source, external_id, job_id in rows if external_id}


def save_posting(session: Session, user: User, posting: postings.Posting) -> Job:
    """Keep a search result as the user's own job, on their Saved jobs list."""
    job = ingest_raw_job(session, posting.as_raw_job()).job
    if session.query(SavedJob).filter(SavedJob.job_id == job.id).first() is None:
        session.add(SavedJob(job_id=job.id))
        session.commit()
        quota.record(session, user.id, ActivityKind.JOB_SAVED, quota.job_label(job), meta={"job_id": job.id})
    return job


def posted_job(
    session: Session, pool_id: str, external_id: str, source_url: str, company: str, title: str, location: str,
    description: str, date_posted: str, ref: str = "",
) -> RawJob:
    """The posting a search result's button sends: by `ref` (app/modules/jobs/
    postings.py) when it has one. A careers-site posting is re-read from the
    job pool (its full text, as published); a job-board one comes from the
    remembered search result -- or the form's fields -- with the pool's full
    description when the pool has the same job."""
    remembered = postings.get(session, ref) if ref else None
    if remembered is not None:
        raw = remembered.as_raw_job()
        if not remembered.company_site:
            raw.description = pool.full_description_for(session, raw.company, raw.title) or raw.description
        return raw
    if pool_id.isdigit():
        pooled = session.get(PoolJob, int(pool_id))
        if pooled is not None:
            return pool.to_raw_job(pooled)
    parsed_date: datetime | None = None
    if date_posted:
        try:
            parsed_date = datetime.fromisoformat(date_posted)
        except ValueError:
            parsed_date = None
    full = pool.full_description_for(session, company, title)
    # Adzuna has no fetch-by-id endpoint, so without a ref the form's own fields are the posting.
    return RawJob(
        external_id=external_id or None, source="adzuna", source_url=source_url, company=company, title=title,
        location=location or None, description=full or description, date_posted=parsed_date,
    )
