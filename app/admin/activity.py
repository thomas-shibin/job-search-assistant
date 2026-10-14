"""The data behind the admin activity dashboard (`/admin/activity` and
`/admin/users/{id}`): what members do, what it costs, and who is hitting the
hourly limits. Reads across users through the admin's system session.

What an admin can see: account details, activity (sign-ins, searches with
their terms, the jobs analyzed / tailored / tracked and status changes),
analysis usage and cost, and limit hits. Never resume content, tailored
resumes or job descriptions.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.admin.charts import Series, bar_chart, money
from app.models.activity import ActivityEvent, ActivityKind
from app.models.ai_usage import AICallLog
from app.models.candidate import CandidateProfile
from app.models.job import Job
from app.models.resume import TailoredResume
from app.models.scheduler import SchedulerRun
from app.models.settings import WatchlistEntry
from app.models.user import AuthSession, OAuthIdentity, Role, User
from app.services import quota

RANGES = (7, 30, 90)
_SEARCH_KINDS = (ActivityKind.SEARCH,)
_OTHER_KINDS = (ActivityKind.RESUME_UPLOAD, ActivityKind.RESUME_EDIT, ActivityKind.JOB_TRACKED, ActivityKind.JOB_SAVED,
                ActivityKind.JOB_STATUS)


def clamp_days(days: int | None) -> int:
    return days if days in RANGES else 30


def _days(since: date, until: date) -> list[date]:
    return [since + timedelta(days=i) for i in range((until - since).days + 1)]


def _day_label(day: date, span: int) -> str:
    return day.strftime("%b %d") if span <= 31 else day.strftime("%m/%d")


@dataclass
class UserRow:
    user: User
    searches: int = 0
    ai_actions: int = 0
    ai_calls: int = 0
    ai_cost: float = 0.0
    listing_requests: int = 0
    limit_hits: int = 0
    sign_ins: int = 0
    last_active: datetime | None = None

    @property
    def status(self) -> str:
        if not self.user.is_active:
            return "suspended"
        if self.user.is_guest:
            return "guest"
        return "active" if self.user.email_verified else "unverified"


@dataclass
class Overview:
    days: int
    totals: dict = field(default_factory=dict)
    charts: dict = field(default_factory=dict)
    rows: list[UserRow] = field(default_factory=list)
    recent: list[tuple[ActivityEvent, User | None]] = field(default_factory=list)
    limit_hits: list[tuple[ActivityEvent, User | None]] = field(default_factory=list)


def _activity_series(events, days: list[date]) -> list[Series]:
    index = {d: i for i, d in enumerate(days)}
    buckets = {key: [0] * len(days) for key in ("search", "ai", "sign_in", "other", "limit")}
    for kind, created_at, *_ in events:
        i = index.get(created_at.date())
        if i is None:
            continue
        if kind in _SEARCH_KINDS:
            buckets["search"][i] += 1
        elif kind in ActivityKind.AI_KINDS:
            buckets["ai"][i] += 1
        elif kind == ActivityKind.SIGN_IN:
            buckets["sign_in"][i] += 1
        elif kind == ActivityKind.LIMIT_HIT:
            buckets["limit"][i] += 1
        elif kind in _OTHER_KINDS:
            buckets["other"][i] += 1
    return [
        Series("Searches", buckets["search"], "series-1"),
        Series("Resume actions", buckets["ai"], "series-2"),
        Series("Sign-ins", buckets["sign_in"], "series-3"),
        Series("Resume & job updates", buckets["other"], "series-4"),
        Series("Limit hits", buckets["limit"], "series-5"),
    ]


def _cost_series(calls, days: list[date]) -> tuple[list[float], list[float]]:
    index = {d: i for i, d in enumerate(days)}
    cost, paid = [0.0] * len(days), [0.0] * len(days)
    for created_at, estimated_cost, cache_hit in calls:
        i = index.get(created_at.date())
        if i is None:
            continue
        cost[i] += float(estimated_cost or 0)
        if not cache_hit:
            paid[i] += 1
    return cost, paid


def _charts(events, calls, days: list[date], span: int, *, signups=None) -> dict:
    labels = [_day_label(d, span) for d in days]
    cost, paid = _cost_series(calls, days)
    charts = {
        "activity": bar_chart(labels, _activity_series(events, days), title="Activity per day"),
        "cost": bar_chart(labels, [Series("Estimated cost (USD)", cost, "series-2")], fmt=money,
                          title="Analysis cost per day"),
        "ai_calls": bar_chart(labels, [Series("Paid analysis calls", paid, "series-4")],
                              title="Analysis calls per day"),
    }
    listing = [0] * len(days)
    index = {d: i for i, d in enumerate(days)}
    hours = [0] * 24
    for kind, created_at, provider_calls in events:
        i = index.get(created_at.date())
        if i is not None:
            listing[i] += provider_calls or 0
        if kind != ActivityKind.LIMIT_HIT:
            hours[created_at.hour] += 1
    charts["listing"] = bar_chart(labels, [Series("Listing requests", listing, "series-1")],
                                  title="Job-listing requests per day")
    charts["hours"] = bar_chart([f"{h:02d}" for h in range(24)], [Series("Actions", hours, "series-3")],
                                label_every=3, title="Activity by hour of day (UTC)")
    if signups is not None:
        members, guests = [0] * len(days), [0] * len(days)
        for role, created_at in signups:
            i = index.get(created_at.date())
            if i is not None:
                (guests if role == Role.GUEST.value else members)[i] += 1
        charts["signups"] = bar_chart(labels, [Series("New members", members, "series-1"),
                                               Series("Guests", guests, "series-4")], title="Sign-ups per day")
    return charts


def overview(session: Session, days: int, *, q: str = "", include_guests: bool = False,
             sort: str = "last_active") -> Overview:
    now = quota.utcnow()
    today = now.date()
    since_day = today - timedelta(days=days - 1)
    since = datetime.combine(since_day, datetime.min.time())
    day_list = _days(since_day, today)
    result = Overview(days=days)

    users = {u.id: u for u in session.query(User).all()}
    events = session.query(ActivityEvent.kind, ActivityEvent.created_at, ActivityEvent.provider_calls,
                           ActivityEvent.user_id, ActivityEvent.ai_calls).filter(
        ActivityEvent.created_at >= since).all()
    calls = session.query(AICallLog.created_at, AICallLog.estimated_cost, AICallLog.cache_hit,
                          AICallLog.user_id).filter(AICallLog.created_at >= since).all()
    signups = [(u.role, u.created_at) for u in users.values() if u.created_at and u.created_at >= since]

    rows = {uid: UserRow(user=u, last_active=u.last_login_at) for uid, u in users.items()}
    for kind, created_at, provider_calls, user_id, ai_calls in events:
        row = rows.get(user_id)
        if row is None:
            continue
        row.listing_requests += provider_calls or 0
        if kind in _SEARCH_KINDS:
            row.searches += 1
        elif kind in ActivityKind.AI_KINDS:
            row.ai_actions += 1
        elif kind == ActivityKind.LIMIT_HIT:
            row.limit_hits += 1
        elif kind == ActivityKind.SIGN_IN:
            row.sign_ins += 1
        if kind != ActivityKind.LIMIT_HIT and (row.last_active is None or created_at > row.last_active):
            row.last_active = created_at
    for created_at, estimated_cost, cache_hit, user_id in calls:
        row = rows.get(user_id)
        if row is not None:
            row.ai_cost += float(estimated_cost or 0)
            row.ai_calls += 0 if cache_hit else 1

    members = [u for u in users.values() if not u.is_guest]
    active_ids = {e[3] for e in events if e[0] != ActivityKind.LIMIT_HIT}
    active_today = {e[3] for e in events if e[1].date() == today and e[0] != ActivityKind.LIMIT_HIT}
    kinds = Counter(e[0] for e in events)
    result.totals = {
        "members": len(members),
        "new_members": sum(1 for u in members if u.created_at and u.created_at >= since),
        "unverified": sum(1 for u in members if not u.email_verified),
        "suspended": sum(1 for u in users.values() if not u.is_active),
        "guests": sum(1 for u in users.values() if u.is_guest and u.created_at and u.created_at >= since),
        "active": len(active_ids),
        "active_today": len(active_today),
        "searches": kinds[ActivityKind.SEARCH],
        "ai_actions": sum(kinds[k] for k in ActivityKind.AI_KINDS),
        "ai_cost": sum(float(c[1] or 0) for c in calls),
        "ai_calls": sum(1 for c in calls if not c[2]),
        "ai_cache_hits": sum(1 for c in calls if c[2]),
        "listing_requests": sum(e[2] or 0 for e in events),
        "limit_hits": kinds[ActivityKind.LIMIT_HIT],
        "sign_ins": kinds[ActivityKind.SIGN_IN],
    }
    result.charts = _charts([(e[0], e[1], e[2]) for e in events],
                            [(c[0], c[1], c[2]) for c in calls], day_list, days, signups=signups)

    needle = q.strip().lower()
    listed = [r for r in rows.values()
              if (include_guests or not r.user.is_guest)
              and (not needle or needle in r.user.email.lower() or needle in r.user.full_name.lower())]
    sorters = {
        "last_active": lambda r: (r.last_active or datetime.min),
        "searches": lambda r: r.searches,
        "ai_actions": lambda r: r.ai_actions,
        "ai_cost": lambda r: r.ai_cost,
        "limit_hits": lambda r: r.limit_hits,
        "joined": lambda r: (r.user.created_at or datetime.min),
    }
    listed.sort(key=sorters.get(sort, sorters["last_active"]), reverse=True)
    result.rows = listed[:500]

    recent = session.query(ActivityEvent).filter(ActivityEvent.kind != ActivityKind.SEARCH_MORE).order_by(
        ActivityEvent.created_at.desc()).limit(40).all()
    result.recent = [(e, users.get(e.user_id)) for e in recent]
    hits = session.query(ActivityEvent).filter(ActivityEvent.kind == ActivityKind.LIMIT_HIT,
                                               ActivityEvent.created_at >= since).order_by(
        ActivityEvent.created_at.desc()).limit(20).all()
    result.limit_hits = [(e, users.get(e.user_id)) for e in hits]
    return result


@dataclass
class UserDetail:
    user: User
    days: int
    row: UserRow
    providers: list[str]
    active_sessions: int
    last_seen: datetime | None
    allowances: dict
    charts: dict
    ai_by_operation: list[dict]
    counts: dict
    jobs_by_status: list[tuple[str, int]]
    events: list[ActivityEvent]
    kind_filter: str | None


def user_detail(session: Session, user: User, days: int, *, kind: str | None = None) -> UserDetail:
    now = quota.utcnow()
    today = now.date()
    since_day = today - timedelta(days=days - 1)
    since = datetime.combine(since_day, datetime.min.time())
    uid = user.id

    events = session.query(ActivityEvent.kind, ActivityEvent.created_at, ActivityEvent.provider_calls).filter(
        ActivityEvent.user_id == uid, ActivityEvent.created_at >= since).all()
    calls = session.query(AICallLog.created_at, AICallLog.estimated_cost, AICallLog.cache_hit).filter(
        AICallLog.user_id == uid, AICallLog.created_at >= since).all()

    row = UserRow(user=user, last_active=user.last_login_at)
    for k, created_at, provider_calls in events:
        row.listing_requests += provider_calls or 0
        row.searches += k in _SEARCH_KINDS
        row.ai_actions += k in ActivityKind.AI_KINDS
        row.limit_hits += k == ActivityKind.LIMIT_HIT
        row.sign_ins += k == ActivityKind.SIGN_IN
        if k != ActivityKind.LIMIT_HIT and (row.last_active is None or created_at > row.last_active):
            row.last_active = created_at
    row.ai_cost = sum(float(c[1] or 0) for c in calls)
    row.ai_calls = sum(1 for c in calls if not c[2])

    by_op = defaultdict(lambda: {"calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
                                 "calls_range": 0, "cost_range": 0.0})
    for op, cache_hit, tokens_in, tokens_out, cost, created_at in session.query(
            AICallLog.operation, AICallLog.cache_hit, AICallLog.tokens_in, AICallLog.tokens_out,
            AICallLog.estimated_cost, AICallLog.created_at).filter(AICallLog.user_id == uid):
        entry = by_op[op]
        entry["cache_hits" if cache_hit else "calls"] += 1
        entry["tokens_in"] += tokens_in or 0
        entry["tokens_out"] += tokens_out or 0
        entry["cost"] += float(cost or 0)
        if created_at and created_at >= since:
            entry["calls_range"] += 0 if cache_hit else 1
            entry["cost_range"] += float(cost or 0)
    ai_by_operation = sorted(({"operation": op.replace("_", " ").capitalize(), **v} for op, v in by_op.items()),
                             key=lambda e: e["cost"], reverse=True)

    sessions = session.query(AuthSession).filter(AuthSession.user_id == uid, AuthSession.expires_at > now).all()
    last_seen = max((s.last_seen_at for s in sessions if s.last_seen_at), default=None)
    providers = sorted({p for (p,) in session.query(OAuthIdentity.provider).filter(OAuthIdentity.user_id == uid)})
    if user.password_hash:
        providers.insert(0, "password")

    platform = quota.platform_limits(session)
    allowances = {name: quota.allowance(session, user, name, platform) for name in (quota.SEARCH, quota.AI)}
    defaults = {quota.SEARCH: (platform.guest_searches_per_hour if user.is_guest else platform.member_searches_per_hour),
                quota.AI: platform.member_ai_actions_per_hour}

    jobs_by_status = [(str(getattr(s, "value", s)).replace("_", " ").title(), n) for s, n in session.query(
        Job.status, func.count(Job.id)).filter(Job.user_id == uid).group_by(Job.status).order_by(
        func.count(Job.id).desc())]
    scans = session.query(func.count(SchedulerRun.id), func.max(SchedulerRun.started_at)).filter(
        SchedulerRun.user_id == uid).one()
    counts = {
        "jobs": sum(n for _, n in jobs_by_status),
        "resume_versions": session.query(func.count(CandidateProfile.id)).filter(
            CandidateProfile.user_id == uid).scalar() or 0,
        "tailored": session.query(func.count(TailoredResume.id)).filter(TailoredResume.user_id == uid).scalar() or 0,
        "watches": session.query(func.count(WatchlistEntry.id)).filter(WatchlistEntry.user_id == uid).scalar() or 0,
        "scans": scans[0] or 0,
        "last_scan": scans[1],
        "ai_cost_all_time": float(session.query(func.coalesce(func.sum(AICallLog.estimated_cost), 0)).filter(
            AICallLog.user_id == uid).scalar() or 0),
    }

    feed = session.query(ActivityEvent).filter(ActivityEvent.user_id == uid)
    if kind in ActivityKind.LABELS:
        feed = feed.filter(ActivityEvent.kind == kind)
    feed_events = feed.order_by(ActivityEvent.created_at.desc()).limit(200).all()

    return UserDetail(
        user=user, days=days, row=row, providers=providers, active_sessions=len(sessions), last_seen=last_seen,
        allowances={"search": allowances[quota.SEARCH], "ai": allowances[quota.AI], "defaults": defaults},
        charts=_charts(events, calls, _days(since_day, today), days), ai_by_operation=ai_by_operation,
        counts=counts, jobs_by_status=jobs_by_status, events=feed_events,
        kind_filter=kind if kind in ActivityKind.LABELS else None,
    )
