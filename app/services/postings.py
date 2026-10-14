"""Search results as one shape (`Posting`), whichever source they came from,
with the facts the search filters need: workplace (remote / hybrid / on-site),
employment type, salary, experience level and age.

Every posting has a `ref` so the results page, the details view and "Save"
can name it without trusting form fields:

- ``p<id>`` -- a job-pool posting (a company's careers site), read from the DB;
- ``a<external id>`` -- a job-board result. The board has no fetch-by-id, so
  results are remembered here (in memory, for an hour) when a search shows them.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.pool import PoolJob
from app.integrations.job_boards.base import RawJob

EMPLOYMENT_TYPES = {
    "full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "temporary": "Temporary",
    "internship": "Internship",
}
WORKPLACES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
EXPERIENCE_LEVELS = {"entry": "Entry level", "mid": "Mid level", "senior": "Senior", "manager": "Manager & above"}
DATE_RANGES = {"1": "Last 24 hours", "3": "Last 3 days", "7": "Last 7 days", "14": "Last 14 days", "30": "Last 30 days"}

_REMOTE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_LEVELS = (
    ("manager", re.compile(r"\b(manager|director|head of|vp|vice president|chief|cto|cfo|ceo)\b", re.IGNORECASE)),
    ("senior", re.compile(r"\b(senior|sr\.?|staff|principal|lead|architect)\b", re.IGNORECASE)),
    ("entry", re.compile(r"\b(intern|internship|co-?op|junior|jr\.?|entry|graduate|new grad|trainee|apprentice)\b",
                         re.IGNORECASE)),
)


def employment_type(value: str | None, title: str = "") -> str | None:
    text = f"{value or ''} {title}".lower().replace("-", "_").replace(" ", "_")
    if "intern" in text or "co_op" in text or "coop" in text:
        return "internship"
    for key in ("part_time", "contract", "temporary"):
        if key in text or (key == "temporary" and "temp_" in text):
            return key
    if "fulltime" in text or "full_time" in text or "permanent" in text:
        return "full_time"
    return None


def workplace(title: str, location: str | None, description: str = "", remote_flag: bool = False) -> str:
    head = f"{title} {location or ''}"
    if _HYBRID.search(head):
        return "hybrid"
    if remote_flag or _REMOTE.search(head):
        return "remote"
    if _HYBRID.search(description[:3000]):
        return "hybrid"
    return "onsite"


def experience_level(title: str) -> str:
    for level, pattern in _LEVELS:
        if pattern.search(title):
            return level
    return "mid"


_MONEY = re.compile(r"(?:(CA|US|C|A|£|€)?\$|£|€)?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*([kK])?")


def parse_salary(text: str | None) -> tuple[int | None, int | None, str | None]:
    """'CA$135K – CA$175K' -> (135000, 175000, 'CAD'). Hourly-looking amounts are ignored."""
    if not text:
        return None, None, None
    values = []
    for prefix, number, thousands in _MONEY.findall(text):
        amount = float(number.replace(",", "").replace(" ", ""))
        if thousands:
            amount *= 1000
        if amount >= 10_000:
            values.append(int(amount))
    if not values:
        return None, None, None
    currency = "USD" if "US$" in text or "USD" in text else "CAD" if ("CA$" in text or "CAD" in text) else None
    return min(values), max(values), currency


@dataclass
class Posting:
    ref: str
    source: str
    title: str
    company: str
    location: str | None
    description: str
    url: str
    date_posted: datetime | None
    employment_type: str | None
    workplace: str
    experience_level: str
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str | None = None
    external_id: str | None = None

    @property
    def company_site(self) -> bool:
        return self.ref.startswith("p")

    @property
    def snippet(self) -> str:
        text = " ".join(self.description.split())
        return text[:240] + ("…" if len(text) > 240 else "")

    @property
    def salary_text(self) -> str | None:
        if not self.salary_min and not self.salary_max:
            return None
        symbol = "US$" if self.currency == "USD" else "$"

        def k(v: int) -> str:
            return f"{symbol}{v / 1000:,.0f}K" if v >= 1000 else f"{symbol}{v}"
        if self.salary_min and self.salary_max and self.salary_min != self.salary_max:
            return f"{k(self.salary_min)} – {k(self.salary_max)} a year"
        return f"{k(self.salary_min or self.salary_max)} a year"

    @property
    def employment_label(self) -> str | None:
        return EMPLOYMENT_TYPES.get(self.employment_type or "")

    @property
    def workplace_label(self) -> str:
        return WORKPLACES[self.workplace]

    def as_raw_job(self) -> RawJob:
        return RawJob(external_id=self.external_id, source=self.source, source_url=self.url, company=self.company,
                      title=self.title, location=self.location, description=self.description,
                      date_posted=self.date_posted, salary_min=self.salary_min, salary_max=self.salary_max,
                      currency=self.currency, employment_type=self.employment_type)


def _naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def from_raw(raw: RawJob) -> Posting:
    pool_id = (raw.raw_payload or {}).get("pool_id")
    ref = f"p{pool_id}" if pool_id else f"a{raw.external_id or abs(hash(raw.source_url))}"
    salary_min, salary_max, currency = raw.salary_min, raw.salary_max, raw.currency
    if pool_id and (raw.raw_payload or {}).get("salary"):
        salary_min, salary_max, currency = raw.raw_payload["salary"]
    return Posting(
        ref=ref, source=raw.source, title=raw.title, company=raw.company, location=raw.location,
        description=raw.description or "", url=raw.source_url, date_posted=_naive(raw.date_posted),
        employment_type=employment_type(raw.employment_type, raw.title),
        workplace=(raw.raw_payload or {}).get("workplace") or workplace(raw.title, raw.location, raw.description or ""),
        experience_level=experience_level(raw.title), salary_min=int(salary_min) if salary_min else None,
        salary_max=int(salary_max) if salary_max else None, currency=currency or ("CAD" if salary_min else None),
        external_id=raw.external_id,
    )


# ---- remembering job-board results ------------------------------------------------------

_TTL_SECONDS = 3600
_MAX = 20_000
_remembered: dict[str, tuple[float, Posting]] = {}
_lock = threading.Lock()


def remember(postings: list[Posting]) -> None:
    now = time.monotonic()
    with _lock:
        if len(_remembered) > _MAX:
            for key in [k for k, (at, _) in _remembered.items() if now - at > _TTL_SECONDS]:
                del _remembered[key]
            if len(_remembered) > _MAX:
                _remembered.clear()
        for posting in postings:
            if not posting.company_site:
                _remembered[posting.ref] = (now, posting)


def get(session: Session, ref: str) -> Posting | None:
    """The posting `ref` names, or None if unknown (or a board result no longer remembered)."""
    ref = (ref or "").strip()
    if ref.startswith("p") and ref[1:].isdigit():
        from app.services.pool import to_raw_job

        job = session.get(PoolJob, int(ref[1:]))
        return from_raw(to_raw_job(job)) if job is not None else None
    if ref.startswith("a"):
        with _lock:
            hit = _remembered.get(ref)
        if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
            return hit[1]
    return None


def clear() -> None:
    with _lock:
        _remembered.clear()


# ---- filters ---------------------------------------------------------------------------

@dataclass
class Filters:
    workplaces: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()
    min_salary: int | None = None
    days: int | None = None

    @classmethod
    def parse(cls, workplace: list[str] | None, job_type: list[str] | None, level: list[str] | None,
              salary: str | None, days: str | None) -> "Filters":
        def pick(values, allowed):
            return tuple(v for v in dict.fromkeys(values or []) if v in allowed)
        try:
            min_salary = max(0, int(salary)) if salary and salary.strip() else None
        except ValueError:
            min_salary = None
        return cls(workplaces=pick(workplace, WORKPLACES), employment_types=pick(job_type, EMPLOYMENT_TYPES),
                   levels=pick(level, EXPERIENCE_LEVELS), min_salary=min_salary or None,
                   days=int(days) if days in DATE_RANGES else None)

    @property
    def active(self) -> int:
        return sum(bool(x) for x in (self.workplaces, self.employment_types, self.levels, self.min_salary, self.days))

    def matches(self, posting: Posting, now: datetime | None = None) -> bool:
        if self.workplaces and posting.workplace not in self.workplaces:
            return False
        if self.employment_types and (posting.employment_type or "full_time") not in self.employment_types:
            return False  # most postings that don't say are full-time
        if self.levels and posting.experience_level not in self.levels:
            return False
        if self.min_salary and (posting.salary_max or posting.salary_min or 0) < self.min_salary:
            return False
        if self.days:
            if posting.date_posted is None:
                return False
            now = now or datetime.now(timezone.utc).replace(tzinfo=None)
            if posting.date_posted < now - timedelta(days=self.days):
                return False
        return True
