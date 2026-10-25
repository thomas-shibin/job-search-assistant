"""The plans shown on /plans. Only the free plan exists today; a new plan is
one more `Plan` in `all_plans`, and the page shows it as the next card (the
arrows appear once there's more than one).

The numbers come from Admin -> Platform settings, so the page always says
what the limits really are.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config.schema import PlatformConfig

# Where members ask for higher limits and send feedback (shown on /plans and in limit messages).
CONTACT_EMAIL = "hirlumo@gmail.com"


@dataclass
class Feature:
    name: str
    detail: str
    limit: str  # e.g. "Unlimited", "10 in total"


@dataclass
class Section:
    title: str
    icon: str
    features: list[Feature]


@dataclass
class Plan:
    key: str
    name: str
    price: str
    period: str
    summary: str
    sections: list[Section] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def free_plan(p: PlatformConfig) -> Plan:
    total, hourly, searches = p.free_ai_actions_total, p.member_ai_actions_per_hour, p.free_searches_total
    return Plan(
        key="free", name="Free", price="$0", period="during the beta",
        summary="Everything Hirlumo does. The only limits are on job-board searches and the features "
                "that cost us money each time they run.",
        sections=[
            Section("Job search", "search", [
                Feature("Search jobs in Canada",
                        "By title and place, or in a sentence like \"remote HR jobs in Toronto over 80k\". "
                        "Radius search around a city. Old ads hidden, the same job shown once.", "Included"),
                Feature("Company careers-site jobs", "Jobs straight from employers' own careers sites, in "
                        "every search.", "Unlimited"),
                Feature("Job-board results",
                        f"Searches that also bring in job-board ads: {_n(searches, 'new search', 'new searches')} "
                        "in total. Paging through a search, or running it again within the hour, doesn't count "
                        f"(up to {p.max_search_pages} pages). Each job-alert check that brings in job-board ads "
                        "counts as one too. After that, searches show company careers-site jobs only, and alerts "
                        "stop checking the job boards.", f"{searches} in total"),
                Feature("Job pages, browse by role and city, company list", "Also open to visitors without "
                        f"an account ({_n(p.guest_searches_per_hour, 'search', 'searches')} an hour, company "
                        "careers-site jobs only).", "Unlimited"),
            ]),
            Section("Your resume", "file", [
                Feature("Upload, edit, versions",
                        "PDF or Word. Edit section by section, compare any two versions and go back to any "
                        "of them.", "Unlimited"),
                Feature("Canadian resume check and converter",
                        "Canada-ready and ATS scores, every suggested change listed, fixed inside your own "
                        "Word file when you choose.", "Unlimited"),
                Feature("Profile, and import from a LinkedIn PDF or export", "", "Unlimited"),
            ]),
            Section("Resume actions", "sparkles", [
                Feature("Match analysis",
                        "How your resume lines up with a job, and what's missing.", "Counts as 1"),
                Feature("Tailored resume and Quick Tailor",
                        "A resume for one job, in your own Word design, built only from your own "
                        "experience. DOCX and PDF.", "Counts as 1"),
                Feature("Canadian equivalents of job titles", "Suggested for the titles that need one.",
                        "Counts as 1"),
                Feature("Reading an unusual search",
                        "When the search box can't read a search on its own (a typo, or an unusual way of "
                        "putting it). Once your resume actions are used, the search box reads it on its own.",
                        "Counts as 1"),
                Feature("Your allowance",
                        f"{_n(total, 'resume action')} in total on the free plan, and at most "
                        f"{_n(hourly, 'action')} an hour. Answers we already worked out for you don't count.",
                        f"{total} in total"),
            ]),
            Section("Keeping track", "briefcase", [
                Feature("Saved jobs and application tracking",
                        "Saved, applied, interview, offer, with each job's files kept together.", "Unlimited"),
                Feature("Job alerts", "Saved searches checked on a schedule, by email or Telegram. "
                        "Checks that reach the job boards use your job-board results (see Job search).",
                        "Included"),
            ]),
        ],
        notes=[
            "One free plan per person. Accounts that share an email address, phone number or resume share "
            "one allowance.",
            "Limits may change during the beta. This page always shows the current ones.",
            f"Need higher limits for your job search? Email {CONTACT_EMAIL} and tell us what you're "
            "working on.",
        ],
    )


def all_plans(platform: PlatformConfig) -> list[Plan]:
    return [free_plan(platform)]
