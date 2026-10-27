"""Being found in search engines: robots.txt, sitemap.xml, the "browse jobs"
pages (/job-search/<role>-jobs-in-<city>) and the structured data Google
reads for its job search (JobPosting).

Only public pages are indexed: the landing page, company and resource pages,
the browse pages and job pages from company career sites. Search results with
filters, accounts and job-board listings are `noindex` -- the browse pages are
the indexable version of a search.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.database.session import get_session
from app.modules.auth.deps import current_user
from app.modules.auth.models import User
from app.modules.jobs import places, postings
from app.modules.jobs.pool import service as pool
from app.modules.jobs.pool.models import PoolJob
from app.web.templating import templates

router = APIRouter()

# (slug, label, what the search looks for). Roles the job pool has the most of.
ROLES = [
    ("software-engineer", "Software Engineer", "Software Engineer"),
    ("data-analyst", "Data Analyst", "Data Analyst"),
    ("data-scientist", "Data Scientist", "Data Scientist"),
    ("machine-learning", "Machine Learning", "Machine Learning"),
    ("devops", "DevOps", "DevOps"),
    ("product-manager", "Product Manager", "Product Manager"),
    ("designer", "Designer", "Designer"),
    ("marketing", "Marketing", "Marketing"),
    ("sales", "Sales", "Sales"),
    ("account-executive", "Account Executive", "Account Executive"),
    ("customer-success", "Customer Success", "Customer Success"),
    ("finance", "Finance", "Finance"),
    ("engineering-manager", "Engineering Manager", "Engineering Manager"),
    ("security", "Security", "Security"),
    ("operations", "Operations", "Operations"),
    ("recruiter", "Recruiter", "Recruiter"),
]
# (slug, label, what the search looks for)
CITIES = [
    ("toronto", "Toronto", "Toronto"), ("vancouver", "Vancouver", "Vancouver"), ("montreal", "Montreal", "Montreal"),
    ("calgary", "Calgary", "Calgary"), ("ottawa", "Ottawa", "Ottawa"), ("edmonton", "Edmonton", "Edmonton"),
    ("waterloo", "Waterloo", "Waterloo"), ("mississauga", "Mississauga", "Mississauga"),
    ("winnipeg", "Winnipeg", "Winnipeg"), ("halifax", "Halifax", "Halifax"), ("victoria", "Victoria", "Victoria"),
    ("kitchener", "Kitchener", "Kitchener"),
]
_ROLES = {slug: (label, query) for slug, label, query in ROLES}
_CITIES = {slug: (label, query) for slug, label, query in CITIES}
_SLUG = re.compile(r"^(?:(remote)-)?(?:([a-z-]+?)-)?jobs(?:-in-([a-z-]+))?$")

PER_PAGE = 30
_CACHE_SECONDS = 3600
_cache: dict[str, tuple[float, object]] = {}

# Paths a search engine may index (exact, or a prefix ending in "/").
_INDEXABLE = ("/", "/jobs", "/companies", "/resources", "/terms", "/privacy", "/data-deletion", "/signup", "/canadian-resume-check", "/plans", "/contact",
              "/job-search", "/help",
              "/job-search/", "/help/")
# Kept out of search engines: accounts, tools and job-board listings.
_DISALLOW = ("/admin", "/api/", "/account", "/auth/", "/profile", "/settings", "/saved", "/applications",
             "/watchlist", "/resume", "/quick", "/costs", "/welcome", "/login", "/logout", "/forgot",
             "/reset", "/verify-email", "/alerts/", "/access-denied", "/jobs/posting/a", "/help/ask")


def _help_articles(session: Session):
    from app.modules.help import knowledge
    from app.modules.usage.quota import platform_limits

    return knowledge.articles(knowledge.values(platform_limits(session)))


def site_url(request: Request | None = None) -> str:
    """The site's public address for canonical links and the sitemap:
    PUBLIC_URL, or the address this request came in on while that is still a
    local one."""
    from app.modules.auth.settings import AuthSettings

    settings = AuthSettings()
    if settings.public_url_is_local and request is not None:
        return str(request.base_url).rstrip("/")
    return settings.base_url.rstrip("/")


def indexable(request: Request) -> bool:
    path = request.url.path
    if path == "/jobs" or path == "/companies":
        return not request.url.query  # a search is a results page
    if path.startswith("/jobs/posting/p"):
        return True
    return path in _INDEXABLE or any(path.startswith(p) for p in _INDEXABLE if p.endswith("/") and p != "/")


def _cached(key: str, build):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = build()
    _cache[key] = (time.monotonic(), value)
    return value


# ---- browse pages ------------------------------------------------------------------------

def parse_slug(slug: str) -> tuple[str | None, str | None, bool] | None:
    """'data-analyst-jobs-in-toronto' -> ('data-analyst', 'toronto', False);
    'remote-devops-jobs' -> ('devops', None, True). None if it names nothing known."""
    match = _SLUG.match(slug or "")
    if not match:
        return None
    remote, role, city = match.group(1) is not None, match.group(2), match.group(3)
    if (role and role not in _ROLES) or (city and city not in _CITIES) or (remote and city):
        return None
    if not (role or city or remote):
        return None
    return role, city, remote


def make_slug(role: str | None = None, city: str | None = None, remote: bool = False) -> str:
    return ("remote-" if remote else "") + (f"{role}-" if role else "") + "jobs" + (f"-in-{city}" if city else "")


def _search(session: Session, role: str | None, city: str | None, remote: bool, per_page: int = PER_PAGE):
    titles = [_ROLES[role][1]] if role else []
    locations = ["Remote"] if remote else ([_CITIES[city][1]] if city else [])
    return pool.search_pool(session, titles, locations, [], "ca", per_page=per_page)


def _heading(role: str | None, city: str | None, remote: bool) -> str:
    """'Data Analyst jobs in Toronto', 'Remote DevOps jobs in Canada', 'Jobs in Ottawa'."""
    what = f"{_ROLES[role][0]} jobs" if role else ("jobs" if remote else "Jobs")
    return ("Remote " if remote else "") + what + f" in {_CITIES[city][0] if city else 'Canada'}"


def counts(session: Session) -> dict[str, int]:
    """Open roles for every browse page, worked out once an hour."""
    def build():
        found = {}
        combos = [(r, None, False) for r in _ROLES] + [(None, c, False) for c in _CITIES] + [(None, None, True)]
        combos += [(r, c, False) for r in _ROLES for c in _CITIES] + [(r, None, True) for r in _ROLES]
        for role, city, remote in combos:
            found[make_slug(role, city, remote)] = _search(session, role, city, remote, per_page=1).total
        return found
    return _cached("counts", build)


@router.get("/job-search")
def browse_index(request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_session)):
    """Every browse page, by role and by city: the way into them for people
    and for search engines."""
    found = counts(session)
    return templates.TemplateResponse(request, "browse_index.html", {
        "roles": [(slug, label, found.get(make_slug(slug), 0)) for slug, label, _ in ROLES],
        "cities": [(slug, label, found.get(make_slug(city=slug), 0)) for slug, label, _ in CITIES],
        "remote": found.get(make_slug(remote=True), 0), "make_slug": make_slug})


@router.get("/job-search/{slug}")
def browse_page(request: Request, slug: str, user: User | None = Depends(current_user),
                session: Session = Depends(get_session)):
    parsed = parse_slug(slug.lower())
    if parsed is None:
        return RedirectResponse(url="/job-search", status_code=301 if slug.lower() != slug else 302)
    if slug.lower() != slug:
        return RedirectResponse(url=f"/job-search/{slug.lower()}", status_code=301)
    role, city, remote = parsed
    results = _search(session, role, city, remote)
    shown = [postings.from_raw(raw) for raw in results.jobs]
    companies = sorted({p.company for p in shown})[:6]
    found = counts(session)
    related_cities = [(make_slug(role, c), _heading(role, c, False), found.get(make_slug(role, c), 0))
                      for c, *_ in CITIES if c != city] if role else []
    related_roles = [(make_slug(r, city, remote), _heading(r, city, remote), found.get(make_slug(r, city, remote), 0))
                     for r, *_ in ROLES if r != role] if (city or remote) or not role else []
    search = {"q": _ROLES[role][1] if role else "", "l": "Remote" if remote else (_CITIES[city][0] if city else "")}
    return templates.TemplateResponse(request, "browse.html", {
        "heading": _heading(role, city, remote), "role": _ROLES[role][0] if role else None,
        "city": _CITIES[city][0] if city else None, "remote": remote, "jobs": shown, "total": results.total,
        "companies": companies, "related_cities": [r for r in related_cities if r[2]][:12],
        "related_roles": [r for r in related_roles if r[2]][:16],
        "search_url": "/jobs?" + urlencode({k: v for k, v in search.items() if v}),
        "updated": datetime.now(timezone.utc).date()},
        # A page with nothing to show is still useful to a visitor, but not to an index.
        headers={"X-Robots-Tag": "noindex"} if not results.total else None)


# ---- robots.txt and sitemap.xml ----------------------------------------------------------

@router.get("/robots.txt")
def robots(request: Request) -> PlainTextResponse:
    lines = ["User-agent: *", *(f"Disallow: {path}" for path in _DISALLOW), "Allow: /", "",
             f"Sitemap: {site_url(request)}/sitemap.xml", ""]
    return PlainTextResponse("\n".join(lines))


@router.get("/sitemap.xml")
def sitemap(request: Request, session: Session = Depends(get_session)) -> Response:
    """Public pages, every browse page with jobs in it, and every open job from
    a company career site in Canada (the newest 40,000; a sitemap holds 50,000)."""
    base = site_url(request)

    def build():
        found = counts(session)
        urls = [("/", None, "daily"), ("/canadian-resume-check", None, "weekly"), ("/jobs", None, "hourly"),
                ("/job-search", None, "daily"),
                ("/companies", None, "daily"), ("/resources", None, "monthly"), ("/signup", None, "monthly"),
                ("/plans", None, "monthly"), ("/contact", None, "yearly"),
                ("/privacy", None, "yearly"), ("/terms", None, "yearly"),
                ("/data-deletion", None, "yearly")]
        urls += [(f"/job-search/{slug}", None, "daily") for slug, total in found.items() if total]
        urls += [("/help", None, "monthly")] + [(f"/help/{a.slug}", None, "monthly") for a in _help_articles(session)]
        rows = (pool.fresh_jobs(session).with_entities(PoolJob.id, PoolJob.last_seen_at)
                .filter(PoolJob.country == "ca")
                .order_by(PoolJob.date_posted.is_(None), PoolJob.date_posted.desc()).limit(40_000).all())
        urls += [(f"/jobs/posting/p{job_id}", seen, "weekly") for job_id, seen in rows]
        return urls

    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, modified, freq in _cached("sitemap", build):
        body.append(f"<url><loc>{escape(base + path)}</loc>"
                    + (f"<lastmod>{modified.date().isoformat()}</lastmod>" if modified else "")
                    + f"<changefreq>{freq}</changefreq></url>")
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml")


# ---- structured data ---------------------------------------------------------------------

_EMPLOYMENT = {"full_time": "FULL_TIME", "part_time": "PART_TIME", "contract": "CONTRACTOR",
               "temporary": "TEMPORARY", "internship": "INTERN"}


def _description_html(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def job_posting(posting, request: Request | None = None) -> dict | None:
    """schema.org JobPosting for a job from a company career site -- what
    Google's job search reads. None for anything else (job-board listings are
    members-only and not ours to republish)."""
    if not posting.company_site:
        return None
    base = site_url(request)
    data = {
        "@context": "https://schema.org", "@type": "JobPosting", "title": posting.title,
        "description": _description_html(posting.description) or html.escape(posting.title),
        "hiringOrganization": {"@type": "Organization", "name": posting.company},
        "directApply": False, "url": f"{base}/jobs/posting/{posting.ref}",
        "identifier": {"@type": "PropertyValue", "name": "Hirlumo", "value": posting.ref},
    }
    if posting.date_posted:
        data["datePosted"] = posting.date_posted.date().isoformat()
    if posting.employment_type in _EMPLOYMENT:
        data["employmentType"] = _EMPLOYMENT[posting.employment_type]
    found = places.points(posting.location)
    if found:
        data["jobLocation"] = [{"@type": "Place", "address": {
            "@type": "PostalAddress", "addressLocality": p.name, "addressRegion": p.province, "addressCountry": "CA"}}
            for p in found[:5]]
    if posting.workplace == "remote":
        data["jobLocationType"] = "TELECOMMUTE"
        data["applicantLocationRequirements"] = {"@type": "Country", "name": "Canada"}
    elif not found:
        data["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "CA"}}
    if posting.salary_min or posting.salary_max:
        value = {"@type": "QuantitativeValue", "unitText": "YEAR"}
        if posting.salary_min:
            value["minValue"] = posting.salary_min
        if posting.salary_max:
            value["maxValue"] = posting.salary_max
        data["baseSalary"] = {"@type": "MonetaryAmount", "currency": posting.currency or "CAD", "value": value}
    return data


# Shown on the landing page and given to search engines as FAQPage data. Plain
# answers to what people search before signing up, resume questions first.
FAQ = [
    ("What is a Canadian resume format?",
     "Canadian employers expect one or two pages, no photo, no personal details such as date of birth, marital "
     "status or nationality, no signed declaration, and references given only when asked. Dates are written as "
     "month and year, spelling is Canadian, and the layout is simple enough for applicant tracking software to "
     "read. Hirlumo's free Canadian resume check scores your resume against these rules in seconds, with no "
     "sign-up."),
    ("Can Hirlumo convert my resume to the Canadian format?",
     "Yes. With a free account, Hirlumo lists every change your resume needs, you choose the ones you want, and "
     "it makes them inside your own Word file, keeping its fonts and design. A PDF becomes a Word resume in a "
     "standard one-column layout. You can compare versions and undo a conversion at any time."),
    ("What is an ATS, and can it read my resume?",
     "An applicant tracking system (ATS) is the software many employers use to collect and search resumes. It "
     "can misread tables, text boxes, columns and contact details placed in the page header. Hirlumo's check "
     "gives your resume an ATS readability score and shows what to change."),
    ("How does the AI resume tailoring work?",
     "Hirlumo compares a job posting with your resume, shows your match score, strengths and gaps, then rewrites "
     "your resume so the experience the employer asks for comes first. It only rewords what is already true; it "
     "never invents experience. Paste any job description, or pick a job from Hirlumo's search."),
    ("Is Hirlumo free?",
     "Yes. The Canadian resume check is free with no account. A free account adds the fixes, resume versions, "
     "job search, saved jobs and alerts, and 10 AI resume actions such as match analyses and tailored resumes."),
    ("Does Hirlumo also help me find jobs?",
     "Yes. One search covers job boards and companies' own career sites across Canada, and you can see your "
     "match score for each job and apply with a tailored resume on the employer's site."),
    ("Can I import my LinkedIn profile?",
     "Yes. Sign up with LinkedIn, then upload your LinkedIn profile PDF or data export to fill in your jobs, "
     "education and skills in one step."),
    ("Is my resume private?",
     "Yes. Your resume, profile and applications are visible only to you. Hirlumo doesn't share them with "
     "employers; you apply on the employer's own site, with the resume you choose."),
]


def site_data(request: Request) -> list[dict]:
    """For the landing page: the site's name (what Google shows for it), its
    logo, what it is (a free web app for Canadian resumes), and the questions
    people ask before signing up."""
    base = site_url(request)
    return [
        {"@context": "https://schema.org", "@type": "WebSite", "name": "Hirlumo", "alternateName": "hirlumo.com",
         "url": base + "/", "potentialAction": {
             "@type": "SearchAction", "target": {"@type": "EntryPoint", "urlTemplate": base + "/jobs?q={q}"},
             "query-input": "required name=q"}},
        {"@context": "https://schema.org", "@type": "Organization", "name": "Hirlumo", "url": base + "/",
         "logo": base + "/static/brand/icon-512.png"},
        {"@context": "https://schema.org", "@type": "WebApplication", "name": "Hirlumo", "url": base + "/",
         "applicationCategory": "BusinessApplication", "operatingSystem": "Any (web browser)",
         "description": "Free Canadian resume checker and converter: checks a resume against Canadian conventions "
                        "and ATS readability, fixes it in the member's own Word design and tailors it to each job.",
         "featureList": ["Canadian resume check", "ATS readability check", "Convert a resume to the Canadian format",
                         "Canadian job titles", "Resume tailoring for a job description", "Match score for a job",
                         "Resume versions and compare", "Job search across Canada"],
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "CAD"}},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]},
    ]
