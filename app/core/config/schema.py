"""The settings schema (pydantic): every section a user or the platform can
set, with defaults and validation. Stored values are validated against it."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class CandidateConfig(BaseModel):
    name: str = ""
    base_resume: str = ""
    # By default the name parsed off the resume's own header (app/modules/resumes/parser.py)
    # wins, since config.yaml's `name` commonly gets left at its placeholder value.
    # Set true to force `name` above to always win instead (e.g. you want a
    # different spelling/nickname on the resume than what's printed on the file).
    override_parsed_name: bool = False


class AddressProfile(BaseModel):
    """An address to show on tailored resumes. Street, unit and postal code are
    optional: with a street the resume shows the complete address, without one
    just the city/province (in the resume's own format).
    """

    street: str | None = None
    unit: str | None = None
    city: str
    province: str
    postal_code: str | None = None
    country: str

    @property
    def has_street(self) -> bool:
        return bool(self.street and self.street.strip())

    def one_line(self) -> str:
        """"1234 Main St, Unit 5, Toronto, ON M5V 1A1, Canada" (blank parts left out)."""
        region = " ".join(part for part in (self.province, self.postal_code) if part and part.strip())
        parts = (self.street, self.unit, self.city, region, self.country)
        return ", ".join(part.strip() for part in parts if part and part.strip())


_ADZUNA_COUNTRIES = {
    "at", "au", "br", "ca", "de", "fr", "gb", "in", "it", "mx", "nl", "nz", "pl", "ru", "sg", "us", "za",
}


class SearchProfile(BaseModel):
    name: str
    enabled: bool = True
    locations: list[str] = Field(default_factory=list)
    address_profile: str
    titles: list[str] = Field(default_factory=list)
    # Automated discovery (Milestone 12): board tokens/company slugs for the public,
    # documented Greenhouse/Lever job-board APIs -- see docs/ARCHITECTURE.md, "Job
    # Source Risk Assessment". Empty by default; manual `discover` always works
    # regardless of these being set.
    greenhouse_boards: list[str] = Field(default_factory=list)
    lever_companies: list[str] = Field(default_factory=list)
    # Adzuna (many employers per query) is configured once, globally, under
    # `search` below rather than per profile.


def _picker(catalog: str) -> dict:
    """Marks a list field the dashboard edits with a searchable multi-select
    whose options come from the search-term catalog of this kind (app/catalog),
    accepting custom values too (see app/web/forms.py, _tag_picker.html).
    """
    return {"input": "tags", "catalog": catalog}


class GlobalSearchConfig(BaseModel):
    """The job-search conditions applied everywhere: the dashboard's job search
    (prefilled and run on landing) and the scheduler's Adzuna scans. One Adzuna
    query runs per title x location; leaving both empty searches every job in
    `country`. A location containing "Remote" searches the whole country with
    "remote" added to the keywords (see app/modules/jobs/search.py). Needs ADZUNA_APP_ID/ADZUNA_APP_KEY in .env.
    """

    titles: list[str] = Field(default_factory=list, json_schema_extra=_picker("title"))
    locations: list[str] = Field(default_factory=list, json_schema_extra=_picker("location"))
    # Narrow every search: a job must mention at least one of these. Costs no
    # extra Adzuna requests (they ride along on each title x location query).
    keywords: list[str] = Field(default_factory=list, json_schema_extra=_picker("keyword"))
    country: str = "ca"
    # Also ingest these searches' newest results on every scheduled scan (feeds
    # the watchlist alerts). Off = the dashboard search only.
    scan_in_background: bool = True
    # Requests/day your Adzuna plan allows (free tier: ~250). Scheduled scans use
    # at most 60% of it, rotating through the title x location combinations when
    # there are more than fit in one scan; the rest is left for dashboard searches.
    adzuna_daily_limit: int = Field(default=250, ge=1)

    @model_validator(mode="after")
    def country_is_known(self) -> "GlobalSearchConfig":
        self.country = self.country.strip().lower()
        if self.country not in _ADZUNA_COUNTRIES:
            raise ValueError(f"search.country must be one of {sorted(_ADZUNA_COUNTRIES)}, got {self.country!r}")
        return self


class MatchingWeights(BaseModel):
    technical_skills: int = 30
    relevant_experience: int = 20
    title_similarity: int = 15
    seniority: int = 10
    platform: int = 10
    location: int = 5
    domain_experience: int = 5
    education: int = 5

    @model_validator(mode="after")
    def weights_sum_to_100(self) -> "MatchingWeights":
        total = (
            self.technical_skills
            + self.relevant_experience
            + self.title_similarity
            + self.seniority
            + self.platform
            + self.location
            + self.domain_experience
            + self.education
        )
        if total != 100:
            raise ValueError(f"matching.weights must sum to 100, got {total}")
        return self


class MatchingConfig(BaseModel):
    minimum_score: int = 75
    required_or_high_value_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    weights: MatchingWeights = Field(default_factory=MatchingWeights)


class AIConfig(BaseModel):
    """Controls Milestone 5's AI-based job-requirement extraction (spec §19 cost
    control): off by default so `analyze` never makes a billed call until the
    user opts in and provides an API key.
    """

    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    generate_match_explanations: bool = False

    @model_validator(mode="after")
    def provider_is_known(self) -> "AIConfig":
        if self.provider not in ("openai", "mock"):
            raise ValueError(f"ai.provider must be 'openai' or 'mock', got '{self.provider}'")
        return self


class ResumeConfig(BaseModel):
    optimize_keywords: bool = True
    reorder_bullets: bool = True
    tailor_summary: bool = True
    tailor_skills: bool = True
    change_address: bool = True
    maximum_pages: int = 2
    never_invent_experience: bool = True


class NotificationsConfig(BaseModel):
    minimum_score: int = 80


JOB_TYPES = ("full_time", "part_time", "contract", "permanent")


class Watch(BaseModel):
    """One watchlist entry: a saved search that alerts you on Telegram.

    Each list is "any of" ("Toronto or Vancouver"); every filter you fill in
    must hold. Titles/companies match partial text, keywords whole words in the
    title or description, locations by city ("Victoria, BC" matches Adzuna's
    "Victoria, Capital") or province ("Ontario"). Unless `search` is off, the
    scheduled scan also runs the watch's own Adzuna search, so it finds jobs
    your main job search doesn't cover.
    """

    name: str
    titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    # Alert only when your keyword match score (0-100) is at least this.
    min_score: int | None = Field(default=None, ge=0, le=100)
    # Salary range (yearly, the posting's currency). Jobs without a listed salary still pass.
    min_salary: int | None = Field(default=None, ge=0)
    max_salary: int | None = Field(default=None, ge=0)
    job_types: list[str] = Field(default_factory=list)  # any of JOB_TYPES; empty = any
    posted_within_days: int | None = Field(default=None, ge=1, le=90)
    search: bool = True

    @model_validator(mode="after")
    def has_some_criteria(self) -> "Watch":
        if not (self.companies or self.titles or self.keywords or self.locations):
            raise ValueError(
                f"watch {self.name!r} needs at least one location, title, keyword or company "
                "(an empty watch would alert on every job)"
            )
        unknown = sorted(set(self.job_types) - set(JOB_TYPES))
        if unknown:
            raise ValueError(f"unknown job type(s) {unknown}; use {list(JOB_TYPES)}")
        if self.min_salary is not None and self.max_salary is not None and self.min_salary > self.max_salary:
            raise ValueError("minimum salary is higher than maximum salary")
        return self


class WatchlistConfig(BaseModel):
    """Telegram alerts for newly discovered jobs matching any watch, independent
    of the match score (spec: notifications are pluggable -- see
    app/modules/alerts/base.py). Needs TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in .env.
    """

    enabled: bool = False
    # A newly added Greenhouse board can surface dozens of "new" jobs on its first
    # scan; past this many alerts in one run, the rest are rolled into one summary.
    max_alerts_per_run: int = 15
    # Optional, e.g. "http://127.0.0.1:8001" -- adds a dashboard link to each alert.
    dashboard_url: str | None = None
    # Not a secret (useless without the bot token), so it's a regular setting the
    # dashboard can detect and save. Falls back to TELEGRAM_CHAT_ID in .env.
    telegram_chat_id: str | None = None
    watches: list[Watch] = Field(default_factory=list)


class RetentionConfig(BaseModel):
    """Automatic clean-up of stored jobs (checked after every scheduled scan).
    Settings, search criteria and watches are never deleted.
    """

    # Delete jobs found more than this many days ago. 0 = keep jobs forever.
    delete_jobs_after_days: int = Field(default=7, ge=0)
    # Keep jobs you've acted on -- approved, application opened, applied,
    # interview, offer, rejected, archived -- however old.
    keep_jobs_you_acted_on: bool = True


class SchedulerConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 15


class PlatformConfig(BaseModel):
    """Platform-wide limits, set by an admin."""

    # Requests/day the job-listings provider allows across the whole platform.
    # Scheduled scans share at most 60% of it among all members; the rest serves
    # interactive searches. Overrides every user's `search.adzuna_daily_limit`.
    job_search_daily_limit: int = Field(default=250, ge=1)
    # New job searches a member may run per hour (paging through a search, or
    # running it again within the hour, is free). Admins can override per user.
    member_searches_per_hour: int = Field(default=10, ge=0)
    # Match analyses + tailored resumes a member may run per hour.
    member_ai_actions_per_hour: int = Field(default=5, ge=0)
    # New job searches one guest may run per hour.
    guest_searches_per_hour: int = Field(default=1, ge=0)
    # Result pages one search may page through (each page is a listing request).
    max_search_pages: int = Field(default=10, ge=1, le=100)
    # How often the job pool (companies' careers-site feeds) is refreshed; 0 = never.
    pool_refresh_hours: int = Field(default=6, ge=0, le=168)


class SessionsConfig(BaseModel):
    """How long a sign-in lasts, set by an admin. A session ends at whichever
    comes first: the inactivity timeout or the maximum length. Administrators
    (anyone who can open the admin dashboard) get the stricter pair, from their
    next request after the role is granted."""

    # Administrators: signed out after this many minutes without a request...
    admin_idle_minutes: int = Field(default=30, ge=5, le=1440)
    # ...and at most this many hours after signing in.
    admin_max_hours: int = Field(default=12, ge=1, le=336)
    # Everyone else: signed out after this many days without a request...
    member_idle_days: int = Field(default=7, ge=1, le=90)
    # ...and at most this many days after signing in.
    member_max_days: int = Field(default=14, ge=1, le=90)


class AppConfig(BaseModel):
    candidate: CandidateConfig
    search: GlobalSearchConfig = Field(default_factory=GlobalSearchConfig)
    address_profiles: dict[str, AddressProfile] = Field(default_factory=dict)
    search_profiles: list[SearchProfile] = Field(default_factory=list)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
