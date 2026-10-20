"""Job search pages: results (/jobs), one posting (/jobs/posting/{ref}),
saving and unsaving results, the Saved jobs page, and "save as default search".
Business logic is in app/modules/jobs/service.py."""
from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.config.store import save_section
from app.modules.access import rbac
from app.modules.auth.deps import member_session, require_member
from app.modules.auth.models import User
from app.modules.jobs import interpret, places, postings
from app.modules.jobs import service as jobs
from app.modules.jobs.catalog.store import remember_user_terms
from app.modules.jobs.models import Job, SavedJob
from app.modules.matching.fit import active_candidate
from app.shared.terms import split_places, split_terms
from app.web.templating import safe_next, templates

router = APIRouter()


_NOT_A_SEARCH = ("We couldn't tell what job you're looking for. Try a job title, like \u201cData analyst\u201d, "
                 "or describe it, like \u201cremote HR jobs in Toronto\u201d.")
_SALARIES = [40000, 60000, 80000, 100000, 120000, 150000]


@router.get("/jobs")
def jobs_page(
    request: Request,
    q: str | None = None,
    l: str | None = None,
    titles: list[str] | None = Query(None),
    locations: list[str] | None = Query(None),
    keywords: list[str] | None = Query(None),
    country: str | None = None,
    page: int = 1,
    view: str | None = None,
    workplace: list[str] | None = Query(None),
    job_type: list[str] | None = Query(None, alias="type"),
    level: list[str] | None = Query(None),
    salary: str | None = None,
    days: str | None = None,
    radius: str | None = None,
    exact: bool = False,
    typed: str | None = None,
    fixed: list[str] | None = Query(None),
    msg: str | None = None,
    error: str | None = None,
    session: Session = Depends(member_session),
    user: User = Depends(require_member),
):
    """Job search results: filters on the left, results in the middle, the
    selected job's details on the right (full screen on phones).

    Only the search terms (what / where / country) make a new search -- which
    counts toward the hourly limit. Filters narrow what's already been found,
    so changing them is free, and they live in the URL.

    Smart search (app/modules/jobs/interpret.py): a search typed in the box is
    first read into titles, places and filters ("remote HR jobs in Toronto over
    80k"); if that changes anything, the browser is sent to the results for
    what was read, with `typed` (and any spelling `fixed`) to say so. Results
    links carry `exact=1`, so paging and filters never read the search again;
    "search for the exact words" uses it too.

    `radius` (km) widens each place that is a town or city to everything within
    that distance (app/modules/jobs/places.py); it's part of the search, so a
    new distance is a new search.
    """
    filters = postings.Filters.parse(workplace, job_type, level, salary, days)
    radius_km = places.parse_radius(radius)
    unknown_words = False
    if q and not exact and rbac.feature_on(session, "smart_search"):
        understood = interpret.interpret(session, user, q)
        if understood.not_a_search:
            terms = SearchTerms(q=q, l=l or "", country=(country or "ca").strip().lower(), radius_km=radius_km)
            return _results(request, session, terms, filters=filters, typed=None, fixed=[],
                            search={"cards": [], "page": 1, "has_next": False, "failed": [], "queries": 0,
                                    "error": _NOT_A_SEARCH})
        reading = understood.reading
        if not reading.same_as(q):
            return RedirectResponse(
                url="/jobs?" + _read_search_query(reading, q, l, keywords, country, radius_km, filters),
                status_code=303)
        unknown_words = bool(reading.unknown)

    title_list, location_list, keyword_list, country, config_error = jobs.search_terms(
        session, q, l, titles, locations, keywords, country)
    terms = SearchTerms(q=", ".join(title_list), l=", ".join(location_list), country=country,
                        keywords=tuple(keyword_list), radius_km=radius_km if location_list else None)
    query_string = terms.url({}).split("?", 1)[1]
    # Words no job title uses (a typo nothing could fix, or nonsense): an empty
    # result isn't counted, and the words aren't kept as suggestions.
    search = jobs.limited_search(session, user, title_list, location_list, keyword_list, country, page, query_string,
                                 free_if_empty=unknown_words, radius_km=terms.radius_km)
    if search["cards"] or not unknown_words:
        remember_user_terms(session, title=title_list, location=location_list, keyword=keyword_list)
    return _results(request, session, terms, filters=filters, search=search, typed=typed, fixed=fixed or [],
                    view=view, error=config_error or error, message=msg)


def _filter_params(filters: postings.Filters) -> dict:
    return {"workplace": list(filters.workplaces), "type": list(filters.employment_types),
            "level": list(filters.levels), "salary": filters.min_salary or "", "days": filters.days or ""}


@dataclass(frozen=True)
class SearchTerms:
    """What a results page searched for: the part of its address that makes a search."""

    q: str
    l: str
    country: str
    keywords: tuple[str, ...] = ()
    radius_km: int | None = None  # only meaningful with a place

    def url(self, filters: postings.Filters | dict, **changes) -> str:
        """This search's results address (with `exact=1`: never read again by
        smart search), with `filters` and any terms in `changes` replaced."""
        terms = replace(self, **changes)
        filter_params = filters if isinstance(filters, dict) else _filter_params(filters)
        params = {"q": terms.q, "l": terms.l, "radius": terms.radius_km if terms.l else None,
                  "country": terms.country, "keywords": list(terms.keywords), "exact": 1, **filter_params}
        # q stays even when empty: no title means every title, not the saved default search.
        return "/jobs?" + urlencode({k: v for k, v in params.items() if k == "q" or v not in ("", [], None)},
                                    doseq=True)


def _read_search_query(reading, q: str, l: str | None, keywords: list[str] | None, country: str | None,
                       radius_km: int | None, filters: postings.Filters) -> str:
    """The results address for a search read by smart search, keeping the place
    box, the distance and any filters already chosen."""
    place_list = list(dict.fromkeys([*split_places(l or ""), *reading.locations]))
    params = {
        "q": ", ".join(reading.titles), "l": ", ".join(place_list),
        "radius": (reading.radius_km or radius_km) if place_list else None,
        "country": country or "", "keywords": keywords or [], "exact": 1,
        "workplace": list(dict.fromkeys([*filters.workplaces, *reading.workplaces])),
        "type": list(dict.fromkeys([*filters.employment_types, *reading.employment_types])),
        "level": list(dict.fromkeys([*filters.levels, *reading.levels])),
        "salary": reading.min_salary or filters.min_salary or "",
        "days": reading.days if str(reading.days) in postings.DATE_RANGES else (filters.days or ""),
        "typed": q, "fixed": [f"{typed}>{corrected}" for typed, corrected in reading.corrections],
    }
    return urlencode({k: v for k, v in params.items() if k == "q" or v not in ("", [], None)}, doseq=True)


def _chips(terms: SearchTerms, filters: postings.Filters) -> list[dict]:
    """What the search is, one removable chip per title, place, distance and filter."""
    titles, place_list, filter_params = split_terms(terms.q), split_places(terms.l), _filter_params(filters)

    def without_filter(kind: str, value) -> str:
        changed = dict(filter_params)
        changed[kind] = [v for v in changed[kind] if v != value] if isinstance(changed[kind], list) else ""
        return terms.url(changed)

    chips = [{"label": t, "kind": "title", "url": terms.url(filters, q=", ".join(x for x in titles if x != t))}
             for t in titles]
    chips += [{"label": p, "kind": "place", "url": terms.url(filters, l=", ".join(x for x in place_list if x != p))}
              for p in place_list]
    if terms.radius_km and place_list:
        chips.append({"label": f"Within {terms.radius_km} km", "kind": "place",
                      "url": terms.url(filters, radius_km=None)})
    chips += [{"label": postings.WORKPLACES[w], "url": without_filter("workplace", w)} for w in filters.workplaces]
    chips += [{"label": postings.EMPLOYMENT_TYPES[t], "url": without_filter("type", t)}
              for t in filters.employment_types]
    chips += [{"label": postings.EXPERIENCE_LEVELS[v], "url": without_filter("level", v)} for v in filters.levels]
    if filters.min_salary:
        chips.append({"label": f"${filters.min_salary // 1000}K+", "url": without_filter("salary", None)})
    if filters.days:
        chips.append({"label": postings.DATE_RANGES[str(filters.days)], "url": without_filter("days", None)})
    return chips


def _results(request: Request, session: Session, terms: SearchTerms, *, filters: postings.Filters, search: dict,
             typed: str | None, fixed: list[str], view: str | None = None, error: str | None = None,
             message: str | None = None):
    tracked = {}
    found = []
    for card in search["cards"]:
        posting = postings.from_raw(card["raw"])
        found.append(posting)
        tracked[posting.ref] = card["tracked"]
    postings.remember(found)
    shown = [p for p in found if filters.matches(p)]
    saved = jobs.saved_job_keys(session)
    selected = next((p for p in shown if p.ref == view), shown[0] if shown else None)

    corrections = [tuple(pair.split(">", 1)) for pair in fixed if ">" in pair][:10]
    radius_choices = sorted({*places.RADIUS_CHOICES, *([terms.radius_km] if terms.radius_km else [])})
    return templates.TemplateResponse(request, "jobs.html", {
        "search": search, "postings": shown, "found_count": len(found), "tracked": tracked,
        "saved": {p.ref: saved.get((p.source, p.external_id)) for p in shown}, "selected": selected,
        "filters": filters, "q": terms.q, "l": terms.l, "country": terms.country, "radius": terms.radius_km,
        "radius_choices": radius_choices, "keywords": list(terms.keywords), "page_base": terms.url(filters),
        "error": error, "message": message, "has_resume": active_candidate(session) is not None,
        "workplaces": postings.WORKPLACES, "employment_types": postings.EMPLOYMENT_TYPES,
        "levels": postings.EXPERIENCE_LEVELS, "date_ranges": postings.DATE_RANGES, "salaries": _SALARIES,
        "chips": _chips(terms, filters),
        "typed": typed if typed and typed != terms.q else None, "corrections": corrections,
        "exact_url": "/jobs?" + urlencode({k: v for k, v in {"q": typed, "country": terms.country, "exact": 1}.items()
                                           if v}),
        "smart_search": rbac.feature_on(session, "smart_search"),
    })


@router.get("/jobs/posting/{ref}")
def posting_page(request: Request, ref: str, session: Session = Depends(member_session)):
    """One search result on its own page (links from the landing page, and
    the no-script fallback for "View details")."""
    posting = postings.get(session, ref)
    if posting is None:
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available. Try a new search."}),
                                status_code=303)
    tracked = jobs.tracked_by_external_id(session, [posting.external_id] if posting.external_id else [])
    saved = jobs.saved_job_keys(session)
    return templates.TemplateResponse(request, "posting.html", {
        "p": posting, "tracked": tracked.get(posting.external_id), "saved_job_id": saved.get((posting.source,
                                                                                               posting.external_id)),
        "has_resume": active_candidate(session) is not None})


# ---- saved jobs ------------------------------------------------------------------------

def _save_button(request: Request, ref: str, job_id: int | None):
    return templates.TemplateResponse(request, "_save_button.html", {"ref": ref, "saved_job_id": job_id})


@router.post("/saved")
def save_job(request: Request, ref: str = Form(...), next: str = Form("/saved"),
             session: Session = Depends(member_session), user: User = Depends(require_member)):
    posting = postings.get(session, ref)
    if posting is None:
        if request.headers.get("hx-request"):
            return HTMLResponse('<span class="muted">This job is no longer available.</span>')
        return RedirectResponse(url="/jobs?" + urlencode({"error": "That job is no longer available."}), status_code=303)
    job = jobs.save_posting(session, user, posting)
    if request.headers.get("hx-request"):
        return _save_button(request, ref, job.id)
    return RedirectResponse(url=safe_next(next, "/saved"), status_code=303)


@router.post("/saved/{job_id}/remove")
def unsave_job(request: Request, job_id: int, ref: str = Form(""), next: str = Form("/saved"),
               session: Session = Depends(member_session)):
    session.query(SavedJob).filter(SavedJob.job_id == job_id).delete(synchronize_session=False)
    session.commit()
    if request.headers.get("hx-request") and ref:
        return _save_button(request, ref, None)
    return RedirectResponse(url=safe_next(next, "/saved"), status_code=303)


@router.get("/saved")
def saved_page(request: Request, session: Session = Depends(member_session)):
    rows = session.query(SavedJob, Job).join(Job, SavedJob.job_id == Job.id).order_by(SavedJob.created_at.desc()).all()
    return templates.TemplateResponse(request, "saved.html", {"rows": rows})


@router.post("/search/defaults")
def save_search_defaults(
    titles: list[str] = Form([]), locations: list[str] = Form([]), keywords: list[str] = Form([]),
    country: str = Form("ca"), session: Session = Depends(member_session),
):
    """"Save as default": the current search box values become Settings -> Job search."""
    try:
        current = user_config(session).search
        save_section(session, "search", {
            **current.model_dump(), "titles": jobs.picked(titles, split_terms),
            "locations": jobs.picked(locations, split_places), "keywords": jobs.picked(keywords, split_terms),
            "country": country,
        })
    except ConfigError as exc:
        return RedirectResponse(url=f"/jobs?{urlencode({'msg': 'Not saved: ' + '; '.join(exc.errors)})}", status_code=303)
    return RedirectResponse(url=f"/jobs?{urlencode({'msg': 'Saved as your default search.'})}", status_code=303)
