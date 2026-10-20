"""Runs the global job search (`AppConfig.search`): one Adzuna query per
title x location (one unfiltered query when both lists are empty, i.e.
"search all jobs"), in parallel, merged newest-first with duplicates removed.
Shared by the dashboard's job search and the scheduler's background scans.

Adzuna's free tier allows roughly 250 requests a day, so dashboard searches are
cached in memory for an hour: reloading the Jobs page or paging back doesn't
spend the quota again (and the hourly per-user search limit, app/services/
quota.py, lets people revisit a search within the hour for free).

Adzuna's `where` only understands places -- "Remote Canada" matches nothing --
so a location mentioning "remote" is sent as a "remote" keyword plus whatever
place is left (none = the whole country); and a location the catalog knows
(app/catalog) is searched by its exact path. With a radius ("within 60 km"),
a place the radius search knows (app/modules/jobs/places.py) is sent by name
with Adzuna's `distance` instead. See `adzuna_query`.
"""
from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import timezone

from app.integrations.job_boards.adzuna import AdzunaSource
from app.integrations.job_boards.base import RawJob
from app.modules.jobs import places
from app.shared.terms import normalize_term

logger = logging.getLogger(__name__)

_MAX_PARALLEL_QUERIES = 4
_CACHE_TTL_SECONDS = 3600
_CACHE_MAX_ENTRIES = 2000
SCHEDULED_SHARE_OF_DAILY_LIMIT = 0.6

_cache: dict[tuple, tuple[float, "SearchResults"]] = {}
_cache_lock = threading.Lock()


@dataclass
class SearchResults:
    jobs: list[RawJob] = field(default_factory=list)
    has_next: bool = False
    failed_queries: list[str] = field(default_factory=list)
    requests: int = 0  # listing requests this call made (0 when served from cache)


_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_PLACE_FILLER = re.compile(
    r"\b(?:in|only|anywhere|canada|usa|united states|uk|united kingdom|australia|india|germany)\b", re.IGNORECASE
)
_EDGE_PUNCTUATION = re.compile(r"^[\s,;:()/\-–]+|[\s,;:()/\-–]+$")


@dataclass(frozen=True)
class AdzunaQuery:
    what: str | None
    where: str | None = None
    area: tuple[str, ...] = ()
    what_or: str | None = None
    filters: tuple[tuple[str, str], ...] = ()  # extra Adzuna params (salary_min, contract, max_days_old...)


def adzuna_query(
    title: str | None, location: str | None, *, areas: dict[str, list[str]] | None = None,
    keywords: list[str] | tuple[str, ...] = (), filters: dict[str, str] | None = None,
    radius_km: int | None = None,
) -> AdzunaQuery:
    """What Adzuna is actually asked for one title x location:
    - a catalog location with a known path is pinned exactly (`area`), anything
      else is free text (`where`);
    - with `radius_km`, a town or city is sent by name with `distance` (Adzuna
      measures from its centre); a province or unknown place ignores the radius;
    - "Remote Canada" -> what="<title> remote", anywhere; "Remote BC" -> where="BC";
    - `keywords` narrow every query: a job must mention at least one of them.
    """
    what = title
    if location and _REMOTE.search(location):
        place = _PLACE_FILLER.sub(" ", _REMOTE.sub(" ", location))
        location = " ".join(_EDGE_PUNCTUATION.sub("", place).split()) or None
        what = f"{title} remote" if title else "remote"
    elif location and radius_km and (origin := places.center(location)) is not None:
        filters = {**(filters or {}), "distance": str(radius_km)}
        what_or = " ".join(dict.fromkeys(word for keyword in keywords for word in keyword.split())) or None
        return AdzunaQuery(what=what, where=origin.label, area=(), what_or=what_or,
                           filters=tuple(sorted(filters.items())))
    area = tuple((areas or {}).get(normalize_term(location), ())) if location else ()
    what_or = " ".join(dict.fromkeys(word for keyword in keywords for word in keyword.split())) or None
    return AdzunaQuery(what=what, where=None if area else location, area=area, what_or=what_or,
                       filters=tuple(sorted((filters or {}).items())))


def query_pairs(titles: list[str], locations: list[str]) -> list[tuple[str | None, str | None]]:
    return list(itertools.product(titles or [None], locations or [None]))


def _newest_first(job: RawJob) -> float:
    if job.date_posted is None:
        return 0.0
    posted = job.date_posted if job.date_posted.tzinfo else job.date_posted.replace(tzinfo=timezone.utc)
    return posted.timestamp()


def search_jobs(
    source: AdzunaSource, titles: list[str], locations: list[str], *, page: int = 1, per_page: int = 20,
    use_cache: bool = True, pairs: list[tuple[str | None, str | None]] | None = None,
    keywords: list[str] | tuple[str, ...] = (), areas: dict[str, list[str]] | None = None,
    filters: dict[str, str] | None = None, radius_km: int | None = None, deadline_seconds: float | None = None,
) -> SearchResults:
    """`deadline_seconds`: stop waiting after this long; queries still running
    then are reported in `failed_queries`, and the results aren't cached, so
    the next visit asks again."""
    pairs = pairs if pairs is not None else query_pairs(titles, locations)
    queries = {pair: adzuna_query(*pair, areas=areas, keywords=keywords, filters=filters, radius_km=radius_km)
               for pair in pairs}
    key = (id(type(source)), getattr(source, "_country", None), tuple(queries.values()), page, per_page)
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
            return replace(hit[1], requests=0)

    def run(pair: tuple[str | None, str | None]) -> list[RawJob]:
        q = queries[pair]
        extra = {"filters": dict(q.filters)} if q.filters else {}
        return source.search_page(what=q.what, where=q.where, area=q.area, what_or=q.what_or,
                                  page=page, per_page=per_page, **extra)

    results = SearchResults(requests=len(pairs))
    seen: set[str] = set()
    deadline = time.monotonic() + deadline_seconds if deadline_seconds is not None else None
    timed_out = False
    pool = ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_QUERIES, len(pairs)))
    try:
        futures = [(pair, pool.submit(run, pair)) for pair in pairs]
        for (what, where), future in futures:
            try:
                wait = None if deadline is None else max(0.0, deadline - time.monotonic())
                batch = future.result(timeout=wait)
            except FutureTimeout:
                timed_out = True
                logger.warning("Adzuna query too slow (what=%r, where=%r): gave up after %gs",
                               what, where, deadline_seconds)
                results.failed_queries.append(" in ".join(p for p in (what, where) if p) or "all jobs")
                continue
            except Exception as exc:
                logger.warning("Adzuna query failed (what=%r, where=%r): %s", what, where, exc)
                results.failed_queries.append(" in ".join(p for p in (what, where) if p) or "all jobs")
                continue
            results.has_next |= len(batch) >= per_page
            for job in batch:
                dedupe_key = job.external_id or job.source_url
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                results.jobs.append(job)
    finally:
        # Don't wait for queries past the deadline: they finish (or fail) on their own.
        pool.shutdown(wait=deadline is None, cancel_futures=True)

    results.jobs.sort(key=_newest_first, reverse=True)
    if use_cache and not timed_out and len(results.failed_queries) < len(pairs):
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                now = time.monotonic()
                for stale in [k for k, (at, _) in _cache.items() if now - at >= _CACHE_TTL_SECONDS]:
                    del _cache[stale]
                if len(_cache) >= _CACHE_MAX_ENTRIES:
                    _cache.clear()
            _cache[key] = (time.monotonic(), results)
    return results


@dataclass(frozen=True)
class QuerySpec:
    """One scheduled-scan query: a title x location, plus the keywords and Adzuna
    filters of the search it belongs to (the global job search, or a watch)."""
    title: str | None
    location: str | None
    keywords: tuple[str, ...] = ()
    filters: tuple[tuple[str, str], ...] = ()
    label: str = "job search"


def watch_filters(watch) -> dict[str, str]:
    """A watch's filters as Adzuna search parameters (it filters server-side too)."""
    params: dict[str, str] = {}
    if watch.min_salary:
        params["salary_min"] = str(watch.min_salary)
    if watch.max_salary:
        params["salary_max"] = str(watch.max_salary)
    if watch.posted_within_days:
        params["max_days_old"] = str(watch.posted_within_days)
    if watch.exclude_keywords:
        params["what_exclude"] = " ".join(dict.fromkeys(w for kw in watch.exclude_keywords for w in kw.split()))
    # Adzuna flags narrow to one value per dimension; with both picked, don't narrow.
    for dimension in (("full_time", "part_time"), ("permanent", "contract")):
        picked = [kind for kind in dimension if kind in watch.job_types]
        if len(picked) == 1:
            params[picked[0]] = "1"
    return params


def scan_specs(config) -> list[QuerySpec]:
    """Every query a scheduled scan should run: the global job search (if it
    scans in the background) and each enabled watch that searches. A watch
    with only companies has nothing to search Adzuna by -- it still checks the
    jobs the other searches find."""
    specs: list[QuerySpec] = []
    if config.search.scan_in_background:
        keywords = tuple(config.search.keywords)
        specs += [QuerySpec(t, loc, keywords) for t, loc in query_pairs(config.search.titles, config.search.locations)]
    for watch in config.watchlist.watches:
        if not watch.search or not (watch.titles or watch.keywords or watch.locations):
            continue
        filters = tuple(sorted(watch_filters(watch).items()))
        specs += [QuerySpec(t, loc, tuple(watch.keywords), filters, f"watch: {watch.name}")
                  for t, loc in query_pairs(watch.titles, watch.locations)]
    unique: dict[tuple, QuerySpec] = {}
    for spec in specs:
        unique.setdefault((spec.title, spec.location, spec.keywords, spec.filters), spec)
    return list(unique.values())


def scans_per_day(interval_minutes: int) -> int:
    return max(1, (24 * 60) // max(interval_minutes, 1))


def queries_per_scan(total_pairs: int, interval_minutes: int, daily_limit: int) -> int:
    """How many title x location queries one scheduled scan may run."""
    budget = int(daily_limit * SCHEDULED_SHARE_OF_DAILY_LIMIT) // scans_per_day(interval_minutes)
    return max(1, min(total_pairs, budget))


def scan_slice(
    pairs: list[tuple[str | None, str | None]], per_scan: int, interval_minutes: int, now: float | None = None
) -> list[tuple[str | None, str | None]]:
    """This scan's share of `pairs`, rotating by time slot so successive scans
    cover different combinations (stateless: the slot is derived from the clock).
    """
    if per_scan >= len(pairs):
        return pairs
    slot = int((now if now is not None else time.time()) // (max(interval_minutes, 1) * 60))
    start = (slot * per_scan) % len(pairs)
    return [pairs[(start + i) % len(pairs)] for i in range(per_scan)]


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
