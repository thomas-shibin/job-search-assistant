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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.config.schema import GlobalSearchConfig
from app.core.tenancy import system_session
from app.integrations.job_boards.adzuna import AdzunaSource
from app.integrations.job_boards.base import RawJob
from app.integrations.job_boards.jooble import JoobleSource
from app.integrations.job_boards.settings import AdzunaSettings, JoobleSettings
from app.modules.applications.service import latest_match_analysis
from app.modules.auth.models import User
from app.modules.jobs import dedupe, places, postings, relevance
from app.modules.jobs.catalog.store import location_paths
from app.modules.jobs.ingestion.pipeline import ingest_raw_job
from app.modules.jobs.models import Job, SavedJob
from app.modules.jobs.pool import service as pool
from app.modules.jobs.pool.models import PoolJob
from app.modules.jobs.ranking import best_match_first
from app.modules.jobs.search import SCHEDULED_SHARE_OF_DAILY_LIMIT, query_pairs, search_jobs
from app.modules.matching.fit import latest_assessment
from app.modules.usage import free_plan, quota
from app.modules.usage.models import ActivityEvent, ActivityKind
from app.shared.terms import split_places, split_terms

logger = logging.getLogger(__name__)

CARDS_PER_PAGE = 40
POOL_PER_PAGE = 20
# The longest the Jobs page waits on the job board; queries still running then
# count as failed, and the page shows what came back (plus the job pool).
PAGE_SEARCH_DEADLINE_SECONDS = 10.0
# When every Adzuna query fails (its rate or daily limit, or an outage), searches
# skip it for this long and ask the second board (Jooble) instead.
BOARD_PAUSE_SECONDS = 10 * 60
JOOBLE_MAX_QUERIES = 3  # title x location pairs asked of the second board, per page
_COUNTRY_NAMES = {"ca": "Canada", "us": "USA", "gb": "UK"}
_board_paused_until = 0.0
_board_lock = threading.Lock()
_BUDGET_CHECK_SECONDS = 60  # how long one count of today's listing requests is reused
_budget_cache: dict[str, float] = {"at": -1e9, "used": 0.0}


def _board_paused() -> bool:
    return time.monotonic() < _board_paused_until


def _pause_board() -> None:
    global _board_paused_until
    with _board_lock:
        _board_paused_until = time.monotonic() + BOARD_PAUSE_SECONDS
    logger.warning("Job board unavailable: using the second board for %d minutes", BOARD_PAUSE_SECONDS // 60)


def reset_board_pause() -> None:
    global _board_paused_until
    with _board_lock:
        _board_paused_until = 0.0
        _budget_cache.update(at=-1e9, used=0.0)


def board_budget(session: Session) -> tuple[int, int]:
    """(listing requests members' searches made today, UTC; how many they may
    make): the platform's daily limit less the share scheduled scans keep
    (search.SCHEDULED_SHARE_OF_DAILY_LIMIT). Past it, searches use the second
    board and the job pool, so the board's own daily limit is never reached."""
    limit = quota.platform_limits(session).job_search_daily_limit
    allowed = limit - int(limit * SCHEDULED_SHARE_OF_DAILY_LIMIT)
    now = time.monotonic()
    with _board_lock:
        fresh = now - _budget_cache["at"] < _BUDGET_CHECK_SECONDS
        used = int(_budget_cache["used"])
    if not fresh:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        bind = session.get_bind()
        with system_session(lambda: Session(bind=bind)) as everyone:  # every member's searches
            used = int(everyone.query(func.coalesce(func.sum(ActivityEvent.provider_calls), 0)).filter(
                ActivityEvent.created_at >= midnight).scalar() or 0)
        with _board_lock:
            _budget_cache.update(at=now, used=float(used))
    return used, allowed


def board_status(session: Session) -> dict:
    """For Platform -> Job sources: the job boards members' searches use."""
    used, allowed = board_budget(session)
    with _board_lock:
        paused = max(0, int(_board_paused_until - time.monotonic()))
    first = AdzunaSettings()
    return {"configured": bool(first.app_id and first.app_key), "used": used, "allowed": allowed,
            "paused_minutes": (paused + 59) // 60, "second_configured": bool(JoobleSettings().api_key)}


def _count_board_requests(requests: int) -> None:
    """Requests this process just made, until the next count from the database."""
    with _board_lock:
        _budget_cache["used"] += requests


def _second_board_search(titles: list[str], locations: list[str], keywords: list[str], country: str,
                         page: int, max_age_days: int | None = None) -> tuple[list[RawJob], bool] | None:
    """The second job board (Jooble) for the same search, or None when it isn't
    set up or every query failed. Returns (postings, more pages?)."""
    settings = JoobleSettings()
    if not settings.api_key:
        return None
    source = JoobleSource(settings.api_key, settings.url)
    pairs = query_pairs(titles, locations)[:JOOBLE_MAX_QUERIES]

    def run(pair: tuple[str | None, str | None]) -> tuple[list[RawJob], int]:
        title, location = pair
        words = " ".join(w for w in [title or "", *keywords] if w)
        return source.search(words, location or _COUNTRY_NAMES.get(country.lower()), page=page)

    jobs: list[RawJob] = []
    more = False
    failed = 0
    with ThreadPoolExecutor(max_workers=len(pairs)) as workers:
        for future in [workers.submit(run, pair) for pair in pairs]:
            try:
                found, total = future.result()
            except Exception as exc:
                failed += 1
                logger.warning("Second job board search failed: %s", exc)
                continue
            jobs += [job for job in found if not dedupe.too_old(job, max_age_days)]
            more = more or page * max(len(found), 1) < total
    if failed == len(pairs):
        return None
    return dedupe.unique(jobs), more


def tracked_by_external_id(session: Session, external_ids: list[str]) -> dict[str, dict]:
    """For search results already saved as jobs: their id, score and AI fit."""
    if not external_ids:
        return {}
    rows = (
        session.query(Job.external_id, Job.id)
        .filter(Job.source.in_(("adzuna", "jooble") + pool.PROVIDERS), Job.external_id.in_(external_ids))
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
    session: Session, *, titles: list[str], locations: list[str], keywords: list[str], country: str, page: int,
    radius_km: int | None = None, job_board: bool = True,
) -> dict:
    """One page of results: the live job-board search across every title x
    location (app/modules/jobs/search.py) merged with the free job pool of
    companies' careers sites (app/modules/jobs/pool), which carries full
    descriptions -- a board result the pool also has is shown from the pool.
    Results are ordered by how well their title matches the search, then newest
    first. Nothing here saves anything: a posting only becomes a tracked `Job` when
    the user analyzes or tracks it. An empty page past the last real results
    is the "no more results" signal. With `radius_km`, each place that is a town
    or city covers everything within that distance (app/modules/jobs/places.py).
    `job_board=False` (signed-out visitors): the job pool only, and nothing is
    looked up in anyone's tracked jobs.

    Returns:
        ``{"error", "cards", "page", "has_next", "failed", "queries", "requests",
        "hidden", "whole_places"}``; ``error`` is set only when neither source
        produced anything; ``whole_places`` are searched places a radius can't
        apply to (a province, or a place the list doesn't know).
    """
    page = max(page, 1)
    result = {"error": None, "cards": [], "page": page, "has_next": False, "failed": [],
              "queries": len(query_pairs(titles, locations)), "requests": 0, "hidden": 0,
              "whole_places": [loc for loc in locations if radius_km and places.center(loc) is None]}

    board_jobs: list[RawJob] = []
    board_error = None
    platform = quota.platform_limits(session)
    board_down = False  # every query failed: the page says the board isn't responding
    settings = AdzunaSettings()
    if not job_board:
        result["queries"] = 0
    elif not (settings.app_id and settings.app_key):
        logger.error("Job search is not configured (listings provider keys missing)")
        board_error = "Job search is temporarily unavailable. Please try again later."
    elif _board_paused():
        board_error = "Job search is temporarily unavailable. Please try again shortly."
    elif (budget := board_budget(session))[0] >= budget[1]:
        logger.info("Job board: members' share of today's listing requests is used up; using the other sources")
        board_error = "Job search is temporarily unavailable. Please try again later."
    else:
        source = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country, interactive=True)
        per_query = max(10, min(50, CARDS_PER_PAGE // result["queries"]))
        try:
            found = search_jobs(source, titles, locations, page=page, per_page=per_query,
                                keywords=keywords, areas=location_paths(session), radius_km=radius_km,
                                deadline_seconds=PAGE_SEARCH_DEADLINE_SECONDS,
                                max_age_days=platform.board_max_job_age_days)
        except Exception:
            logger.exception("Job search failed")
            board_error = "Job search is temporarily unavailable. Please try again shortly."
        else:
            if found.failed_queries and len(found.failed_queries) >= result["queries"] and not found.jobs:
                _pause_board()
                board_down = True
            board_jobs = found.jobs
            # The board returns its best matches first: once a page has nothing
            # relevant, later pages won't either.
            has_more = found.has_next and (not titles or any(
                relevance.tier(titles, j.title, j.company) >= relevance.RELEVANT for j in board_jobs))
            result.update(has_next=has_more, failed=found.failed_queries, requests=found.requests)
            _count_board_requests(found.requests)

    if (board_error or board_down) and job_board:  # the second board stands in
        second = _second_board_search(titles, locations, keywords, country, page, platform.board_max_job_age_days)
        if second is not None:
            board_jobs, more = second
            board_error = None
            result.update(has_next=more, failed=[])  # `requests` counts the first board's only

    try:
        pooled = pool.search_pool(session, titles, locations, keywords, country, page=page, per_page=POOL_PER_PAGE,
                                  radius_km=radius_km)
    except Exception:
        logger.exception("Job pool search failed")
        pooled = pool.PoolResults(jobs=[], has_next=False, total=0)
    if board_error and not pooled.jobs:
        result["error"] = board_error
        return result

    jobs, result["hidden"] = relevant_only(_merge_results(pooled.jobs, board_jobs), titles)
    tracked = tracked_by_external_id(session, [j.external_id for j in jobs if j.external_id]) if job_board else {}
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
    """Both lists, newest first, each job once (app/modules/jobs/dedupe.py): a
    board ad the pool also has (same company, title and city) is dropped -- the
    company's own feed has the full description and still lists the job."""
    return sorted(dedupe.unique([*pool_jobs, *board_jobs]), key=_posted_at, reverse=True)


def relevant_only(jobs: list[RawJob], titles: list[str]) -> tuple[list[RawJob], int]:
    """Only postings whose title matches the typed titles (or whose company is
    the one typed), best match first and newest first within each tier
    (app/modules/jobs/relevance.py). The job board searches whole descriptions,
    so it also returns jobs that only mention the term in passing ("plumber ...
    send your resume to our HR team"); those are left out rather than shown last.

    Returns:
        (the relevant postings, how many were left out).
    """
    if not titles:
        return jobs, 0
    ranked = sorted(((relevance.tier(titles, job.title, job.company), job) for job in jobs), key=lambda p: -p[0])
    kept = [job for tier, job in ranked if tier >= relevance.RELEVANT]
    return kept, len(jobs) - len(kept)


def _search_summary(titles: list[str], locations: list[str], keywords: list[str], country: str,
                    radius_km: int | None = None) -> str:
    parts = [", ".join(titles) or "any title", "in " + (", ".join(locations) or "anywhere")]
    if radius_km and locations:
        parts.append(f"(within {radius_km} km)")
    if keywords:
        parts.append("with " + ", ".join(keywords))
    return " ".join(parts) + f" ({country.upper()})"


def limited_search(session: Session, user: User, titles: list[str], locations: list[str], keywords: list[str],
                   country: str, page: int, query_string: str, *, free_if_empty: bool = False,
                   radius_km: int | None = None) -> dict:
    """The job search behind the hourly search limit (app/modules/usage/quota.py):
    a new search counts once; its other pages, or running it again within the
    hour, are free. Over the limit, the result offers the last search instead.
    `free_if_empty`: give the search back if it finds nothing (words no job
    title uses -- usually a typo -- shouldn't use up a search).

    Returns:
        The `search_for_page` result plus ``allowance``; over the limit,
        ``limited=True``, an ``error`` message and ``last_search``.
    """
    platform = quota.platform_limits(session)
    radius_km = radius_km if locations else None
    fingerprint = quota.search_fingerprint(titles, locations, keywords, country, radius_km=radius_km)
    summary = _search_summary(titles, locations, keywords, country, radius_km)
    try:
        charge = quota.charge_search(
            session, user, fingerprint=fingerprint, page=page, summary=summary, platform=platform,
            meta={"titles": titles, "locations": locations, "keywords": keywords, "country": country,
                  "radius_km": radius_km, "query_string": query_string},
        )
    except quota.LimitReached as exc:
        current = exc.allowance or quota.allowance(session, user, quota.SEARCH, platform)
        return {"error": str(exc), "limited": True, "cards": [], "page": page, "has_next": False, "failed": [],
                "queries": 0, "allowance": current, "last_search": _last_search(session, user, fingerprint)}
    # The free plan's job-board searches: a new search past them shows company
    # careers-site jobs only. (More pages of a search already counted still reach the boards.)
    boards = quota.free_standing(session, user, platform, free_plan.SEARCH)
    boards_used_up = charge.event_id is not None and boards is not None and boards.exhausted
    search = search_for_page(session, titles=titles, locations=locations, keywords=keywords, country=country,
                             page=page, radius_km=radius_km, job_board=not boards_used_up)
    # Not counted: a search that failed, found nothing for words no title uses,
    # or couldn't reach the job board at all (only the job pool answered).
    board_down = (not boards_used_up and bool(search.get("failed"))
                  and len(search["failed"]) >= search.get("queries", 1))
    failed = bool(search.get("error")) or (free_if_empty and not search["cards"]) or board_down
    quota.settle_search(session, user, charge, provider_calls=search.get("requests", 0), failed=failed,
                        summary=summary, fingerprint=fingerprint, page=page)
    if charge.event_id is not None and not failed and search.get("requests", 0) > 0:
        try:
            free_plan.spend(session, user, free_plan.SEARCH)
            boards = quota.free_standing(session, user, platform, free_plan.SEARCH)
        except Exception:
            session.rollback()
            logger.exception("Couldn't count a free-plan search for user_id=%s", user.id)
    search["has_next"] = search["has_next"] and page < platform.max_search_pages
    search["allowance"] = charge.allowance
    search["boards"] = boards
    if boards_used_up:
        search["boards_note"] = quota.free_search_note(boards)
    return search


def guest_search(session: Session, titles: list[str], locations: list[str], keywords: list[str], country: str,
                 page: int, *, radius_km: int | None = None) -> dict:
    """A signed-out visitor's search: jobs from company career sites (the job
    pool) only -- the job board's listings, and its request budget, are for
    members. Nothing is counted or stored."""
    platform = quota.platform_limits(session)
    search = search_for_page(session, titles=titles, locations=locations, keywords=keywords, country=country,
                             page=page, radius_km=radius_km if locations else None, job_board=False)
    search["has_next"] = search["has_next"] and page < platform.max_search_pages
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


def _titles(text: str) -> list[str]:
    """Typed titles: comma-separated, and "HR and Admin" is two (relevance.split_titles)."""
    return list(dict.fromkeys(one for term in split_terms(text) for one in relevance.split_titles(term)))


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
    title_list = _titles(q or "") if q is not None else (
        picked(titles, _titles) if titles is not None else ([] if typed else search_config.titles))
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
